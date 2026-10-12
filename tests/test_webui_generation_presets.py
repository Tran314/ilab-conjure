from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_image.webui.generation_presets import GenerationPresetSettings
from codex_image.webui.routes.generation_presets import register_generation_preset_routes
from tests.test_generation_presets import preset_payload, preset_record


class WebUIGenerationPresetApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = GenerationPresetSettings(Path(self.temp.name) / "generation-presets.json")
        self.authenticated = True
        ctx = SimpleNamespace(generation_preset_settings=self.store, api_settings=SimpleNamespace(public_settings=lambda: {"providers": []}), route_helpers={"codex_auth_checker": lambda: self.authenticated})
        app = FastAPI()
        register_generation_preset_routes(app, ctx)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def create(self, **changes):
        response = self.client.post("/api/generation-presets", json=preset_payload(**changes))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["preset"]

    def test_old_configuration_and_crud_endpoints(self):
        self.assertEqual(self.client.get("/api/generation-presets").json(), {"version": 1, "presets": []})
        preset = self.create()
        self.assertTrue(preset["availability"]["available"])
        url = "/api/generation-presets/" + preset["id"]
        renamed = self.client.patch(url, json={"name": "改名"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["preset"]["name"], "改名")
        self.assertEqual(self.client.delete(url).status_code, 409)
        deleted = self.client.delete(url + "?confirm=true")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()["presets"], [])

    def test_create_and_rename_duplicates_are_409(self):
        self.create(name="ABC")
        self.assertEqual(self.client.post("/api/generation-presets", json=preset_payload(name="ａｂｃ")).status_code, 409)
        second = self.create(name="Second")
        self.assertEqual(self.client.patch("/api/generation-presets/" + second["id"], json={"name": "abc"}).status_code, 409)

    def test_unknown_model_provider_and_binding_creation_rejected(self):
        for changes in ({"canonical_model_id": "removed"}, {"provider_id": "removed"}, {"binding_id": "removed"}):
            with self.subTest(changes=changes):
                response = self.client.post("/api/generation-presets", json=preset_payload(**changes))
                self.assertEqual(response.status_code, 409)
                self.assertIn("code", response.json()["detail"])
        self.assertEqual(self.store.list(), [])

    def test_removed_provider_can_list_rename_delete_but_not_apply(self):
        record = preset_record(provider_id="removed")
        self.store.replace_snapshot({"version": 1, "presets": [record]})
        listed = self.client.get("/api/generation-presets").json()["presets"][0]
        self.assertFalse(listed["availability"]["available"])
        self.assertEqual(listed["availability"]["issues"][0]["code"], "provider_not_found")
        url = "/api/generation-presets/" + record["id"]
        self.assertEqual(self.client.post(url + "/apply", json={}).status_code, 409)
        self.assertEqual(self.client.patch(url, json={"name": "可保留"}).status_code, 200)
        self.assertEqual(self.client.delete(url + "?confirm=true").status_code, 200)

    def test_apply_is_read_only_and_returns_only_safe_settings(self):
        preset = self.create()
        before = self.store.path.read_bytes()
        url = "/api/generation-presets/" + preset["id"]
        result = self.client.post(url + "/apply", json={"image_count": 2, "reference_file_count": 0, "has_mask": False})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["preset"]["parameters"], preset["parameters"])
        self.assertEqual(self.store.path.read_bytes(), before)
        for field in ("prompt", "api_key", "image_inputs", "protocol_request", "reference_files"):
            self.assertNotIn(field, result.json()["preset"])

    def test_apply_checks_current_reference_counts_and_authentication(self):
        preset = self.create(reference_rules={"images": "require"})
        url = "/api/generation-presets/" + preset["id"]
        check = self.client.post(url + "/compatibility", json={"image_count": 0})
        self.assertEqual(check.status_code, 200)
        self.assertFalse(check.json()["compatible"])
        self.assertEqual(self.client.post(url + "/apply", json={"image_count": 0}).status_code, 409)
        self.assertEqual(self.client.post(url + "/apply", json={"image_count": 1}).status_code, 200)
        self.authenticated = False
        response = self.client.post(url + "/apply", json={"image_count": 1})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "provider_unavailable")

    def test_invalid_fields_are_422_and_never_echo_secret_values(self):
        response = self.client.post("/api/generation-presets", json=preset_payload(api_key="sk-SUPERSECRET"))
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("SUPERSECRET", response.text)
        preset = self.create()
        response = self.client.post("/api/generation-presets/" + preset["id"] + "/apply", json={"prompt": "SUPERSECRET"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("SUPERSECRET", response.text)

    def test_corrupt_and_future_store_returns_actionable_503_without_overwrite(self):
        for raw in ("{corrupted", json.dumps({"version": 99, "presets": []})):
            with self.subTest(raw=raw):
                self.store.path.write_text(raw)
                for method, url, kwargs in (("get", "/api/generation-presets", {}), ("post", "/api/generation-presets", {"json": preset_payload()}), ("patch", "/api/generation-presets/missing", {"json": {"name": "x"}}), ("delete", "/api/generation-presets/missing?confirm=true", {})):
                    response = getattr(self.client, method)(url, **kwargs)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(self.store.path.read_text(), raw)


if __name__ == "__main__":
    unittest.main()
