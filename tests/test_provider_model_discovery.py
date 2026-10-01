from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_image.http import HTTPResponse, HTTPResponseTooLarge
from codex_image.webui.provider_model_discovery import (
    MAX_MODEL_DISCOVERY_BYTES,
    MODEL_DISCOVERY_TIMEOUT_SECONDS,
    ModelDiscoveryError,
    discover_provider_models,
)
from codex_image.webui.routes.settings import register_settings_routes


class ProviderModelDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = Mock()
        self.settings = {
            "providers": [{
                "id": "relay", "base_url": "https://relay.example/v1", "api_key": "saved-test-key",
            }],
        }
        self.ctx = SimpleNamespace(
            route_helpers={},
            api_settings=Mock(read=Mock(return_value=self.settings)),
            network_egress_manager=Mock(transport=Mock(return_value=self.transport)),
        )
        self.payload = {
            "provider_id": "relay", "base_url": "https://relay.example/v1", "protocol": "openai_images",
        }

    def respond(self, payload, status=200) -> HTTPResponse:
        return HTTPResponse(status, json.dumps(payload).encode(), {})

    def test_openai_models_use_saved_key_and_return_only_sorted_unique_ids(self) -> None:
        self.transport.request_bounded.return_value = self.respond({"data": [
            {"id": "z/custom.model:2", "owned_by": "private-owner"},
            {"id": "gpt-image-2"}, {"id": "gpt-image-2"}, {"id": ""}, None,
        ]})
        self.assertEqual(discover_provider_models(self.ctx, self.payload), {"models": [
            {"id": "gpt-image-2"}, {"id": "z/custom.model:2"},
        ]})
        call = self.transport.request_bounded.call_args.kwargs
        self.assertEqual(call["url"], "https://relay.example/v1/models")
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["headers"]["Authorization"], "Bearer saved-test-key")
        self.assertEqual(call["body"], b"")
        self.assertEqual(call["max_response_bytes"], MAX_MODEL_DISCOVERY_BYTES)
        self.assertEqual(
            self.ctx.network_egress_manager.transport.call_args.kwargs["timeout_seconds"],
            MODEL_DISCOVERY_TIMEOUT_SECONDS,
        )
        self.ctx.api_settings.write.assert_not_called()

    def test_new_provider_can_fetch_before_save_and_root_url_stays_unversioned(self) -> None:
        self.transport.request_bounded.return_value = self.respond({"data": []})
        discover_provider_models(self.ctx, {
            "base_url": "https://new.example/images/generations/", "api_key": "new-test-key",
            "protocol": "openai_responses",
        })
        self.assertEqual(self.transport.request_bounded.call_args.kwargs["url"], "https://new.example/models")
        self.ctx.api_settings.write.assert_not_called()

    def test_saved_key_copy_requires_same_origin_and_can_use_an_unsaved_path(self) -> None:
        self.transport.request_bounded.return_value = self.respond({"data": []})
        payload = {**self.payload, "provider_id": "copy", "api_key_source_provider_id": "relay",
                   "base_url": "https://relay.example/custom/v2"}
        discover_provider_models(self.ctx, payload)
        self.assertEqual(self.transport.request_bounded.call_args.kwargs["url"], "https://relay.example/custom/v2/models")
        for base in ("https://other.example/v1", "http://relay.example/v1", "https://relay.example:8443/v1"):
            with self.subTest(base=base), self.assertRaisesRegex(ModelDiscoveryError, "api_key_origin_mismatch"):
                discover_provider_models(self.ctx, {**payload, "base_url": base})
        self.assertEqual(self.transport.request_bounded.call_count, 1)

    def test_cross_origin_edit_needs_explicit_new_key(self) -> None:
        self.transport.request_bounded.return_value = self.respond({"data": []})
        payload = {**self.payload, "base_url": "https://other.example/v1"}
        with self.assertRaisesRegex(ModelDiscoveryError, "api_key_origin_mismatch"):
            discover_provider_models(self.ctx, payload)
        self.transport.request_bounded.assert_not_called()
        discover_provider_models(self.ctx, {**payload, "api_key": "replacement-test-key"})
        self.assertEqual(self.transport.request_bounded.call_args.kwargs["headers"]["Authorization"], "Bearer replacement-test-key")

    def test_gemini_paginates_and_removes_only_resource_prefix(self) -> None:
        self.transport.request_bounded.side_effect = [
            self.respond({"models": [{"name": "models/gemini-custom", "description": "private"}], "nextPageToken": "a&b/+"}),
            self.respond({"models": [{"name": "models/vendor/image:2"}, {"name": "models/gemini-custom"}]}),
        ]
        result = discover_provider_models(self.ctx, {**self.payload, "protocol": "gemini"})
        self.assertEqual(result, {"models": [{"id": "gemini-custom"}, {"id": "vendor/image:2"}]})
        calls = self.transport.request_bounded.call_args_list
        self.assertEqual(calls[1].kwargs["url"], "https://relay.example/v1/models?pageToken=a%26b%2F%2B")
        self.assertEqual(calls[0].kwargs["headers"]["x-goog-api-key"], "saved-test-key")
        self.assertNotIn("Authorization", calls[0].kwargs["headers"])

    def test_empty_gemini_response_is_an_empty_list(self) -> None:
        self.transport.request_bounded.return_value = self.respond({})
        self.assertEqual(discover_provider_models(self.ctx, {**self.payload, "protocol": "gemini"}), {"models": []})

    def test_repeated_page_token_and_malformed_success_are_rejected(self) -> None:
        for data in ({"models": [], "nextPageToken": "repeat"}, {"models": "wrong"}):
            with self.subTest(data=data), self.assertRaisesRegex(ModelDiscoveryError, "model_discovery_invalid_response"):
                self.transport.request_bounded.return_value = self.respond(data)
                discover_provider_models(self.ctx, {**self.payload, "protocol": "gemini"})
        self.transport.request_bounded.return_value = HTTPResponse(200, b"<html>saved-test-key</html>", {})
        with self.assertRaisesRegex(ModelDiscoveryError, "model_discovery_invalid_response"):
            discover_provider_models(self.ctx, self.payload)

    def test_upstream_errors_never_echo_body_or_credentials(self) -> None:
        for status, code in ((401, "unauthorized"), (403, "unauthorized"), (404, "not_supported"),
                             (405, "not_supported"), (429, "rate_limited"), (500, "upstream_error")):
            with self.subTest(status=status), self.assertRaisesRegex(ModelDiscoveryError, f"model_discovery_{code}") as caught:
                self.transport.request_bounded.return_value = self.respond({"error": "saved-test-key"}, status)
                discover_provider_models(self.ctx, self.payload)
            self.assertNotIn("saved-test-key", str(caught.exception))
        for error, code in ((TimeoutError("saved-test-key"), "network_error"),
                            (HTTPResponseTooLarge("private"), "too_large")):
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(ModelDiscoveryError, f"model_discovery_{code}"):
                self.transport.request_bounded.side_effect = error
                discover_provider_models(self.ctx, self.payload)

    def test_invalid_connection_is_rejected_before_network_access(self) -> None:
        for patch in ({"protocol": "codex"}, {"protocol": []}, {"base_url": "file:///private"},
                      {"base_url": "https://user:password@relay.example/v1"},
                      {"api_key": "key\nheader"}, {"provider_id": "unknown"}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                discover_provider_models(self.ctx, {**self.payload, **patch})
        self.transport.request_bounded.assert_not_called()

    def test_route_exposes_safe_results_and_configuration_errors(self) -> None:
        app = FastAPI()
        register_settings_routes(app, self.ctx)
        self.transport.request_bounded.return_value = self.respond({"data": [{"id": "custom"}]})
        with TestClient(app) as client:
            response = client.post("/api/api-settings/models", json=self.payload)
            missing_key = client.post("/api/api-settings/models", json={**self.payload, "provider_id": "unknown"})
            self.transport.request_bounded.side_effect = TimeoutError("saved-test-key")
            failed = client.post("/api/api-settings/models", json=self.payload)
        self.assertEqual(response.json(), {"models": [{"id": "custom"}]})
        self.assertEqual(missing_key.status_code, 400)
        self.assertEqual(failed.status_code, 502)
        self.assertEqual(failed.json(), {"detail": "model_discovery_network_error"})


if __name__ == "__main__":
    unittest.main()
