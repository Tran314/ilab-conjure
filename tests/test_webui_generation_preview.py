from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from PIL import Image

from codex_image.webui.app import create_app
from codex_image.webui.generation_summary import PREVIEW_TOKEN_HEADER
from codex_image.webui.reference_files import validate_reference_file
from tests.webui_helpers import FakeImageClient


class GenerationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.make_app()
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.data = {
            "prompt": "private preview prompt",
            "canonical_model_id": "gpt-image-2", "provider_id": "codex",
            "binding_id": "codex-gpt-image-2-images",
            "parameters_json": json.dumps({"canvas.size": "1024x1024", "gpt.quality": "high", "output.count": 3}),
        }

    def make_app(self):
        settings = {name: self.root / (name + ".json") for name in (
            "auth_settings_path", "api_settings_path", "webui_settings_path",
            "network_egress_settings_path", "color_settings_path", "prompt_snippets_path", "prompt_templates_path",
        )}
        return create_app(output_root=self.root / "outputs", client_factory=FakeImageClient,
                          auth_checker=lambda: True, auto_start_queue=False, batch_delay_seconds=0, **settings)

    @staticmethod
    def png(color):
        output = BytesIO()
        Image.new("RGB", (3, 4), color).save(output, format="PNG")
        return output.getvalue()

    def preview(self, data=None, *, operation="generate", files=None):
        result = self.client.post(f"/api/{operation}/preview", data=data or self.data, files=files)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def disk_state(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def provider(self, *, model="gpt-image-2", codec="gpt_openai_images", profile="openai_images", concurrency=2):
        settings = {
            "schema_version": 2, "active_provider_id": "relay", "default_provider_by_model": {model: "relay"},
            "providers": [{"id": "relay", "name": "Relay", "base_url": "https://relay.example/v1",
                           "api_key": "unit-test-secret", "auth_scheme": "bearer", "concurrency": concurrency,
                           "bindings": [{"id": "relay-binding", "canonical_model_id": model,
                                         "remote_model_id": f"remote/{model}", "protocol_profile": profile,
                                         "parameter_codec": codec, "operations": ["generate", "edit"]}]}],
        }
        self.app.state.ctx.api_settings.replace_snapshot(settings)
        return settings

    def test_preview_has_no_auth_client_task_reference_or_identity_side_effects(self):
        ctx = self.app.state.ctx
        saved = ctx.reference_asset_storage.create_or_touch("saved.png", self.png("green"), "image/png")
        gallery = ctx.gallery_storage.create_item("Gallery", "portrait", "gallery.png", self.png("yellow"), "image/png")
        saved_file = ctx.reference_file_storage.create_or_touch(validate_reference_file("saved.txt", b"secret text", "text/plain"))
        data = {**self.data, "binding_id": "codex-gpt-image-2-responses", "reference_asset_ids": saved["id"],
                "gallery_image_ids": gallery["id"], "reference_file_ids": saved_file["id"], "client_request_id": "invalid ignored on preview"}
        files = [("reference_images", ("private-input.png", self.png("red"), "image/png")),
                 ("reference_files", ("private-file.txt", b"private file body", "text/plain"))]
        before = self.disk_state()
        with patch.dict(ctx.route_helpers, {
            "codex_auth_checker": Mock(side_effect=AssertionError("preview must not check or refresh auth")),
            "wake_queue_worker": Mock(side_effect=AssertionError("preview must not wake the queue")),
        }), patch.object(ctx, "client_factory", side_effect=AssertionError("preview must not construct a client")), \
             patch.object(ctx.storage, "create_task", side_effect=AssertionError("preview must not create tasks")), \
             patch.object(ctx.reference_asset_storage, "create_or_touch", side_effect=AssertionError("preview must not save uploads")), \
             patch.object(ctx.reference_asset_storage, "touch", side_effect=AssertionError("preview must not touch assets")), \
             patch.object(ctx.reference_file_storage, "commit_batch", side_effect=AssertionError("preview must not save files")), \
             patch("socket.create_connection", side_effect=AssertionError("preview must not use the network")):
            body = self.preview(data, files=files)
        self.assertEqual(self.disk_state(), before)
        self.assertEqual(ctx.storage.list_tasks(), [])
        self.assertFalse((ctx.source_data_root / "webui-submissions.db").exists())
        serialized = json.dumps(body)
        for private in (data["prompt"], "private-input.png", "private-file.txt", "private file body", "secret text", "data:image/", str(self.root)):
            self.assertNotIn(private, serialized)
        self.assertEqual(body["summary"]["references"]["image_count"], 3)
        self.assertEqual(body["summary"]["references"]["file_count"], 2)

    def test_summary_matches_persisted_snapshot_and_effective_wire_parameters(self):
        body = self.preview()
        response = self.client.post("/api/generate", data=self.data, headers={PREVIEW_TOKEN_HEADER: body["preview_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        metadata = self.app.state.storage.read_metadata(response.json()["task"]["task_id"])
        summary = body["summary"]
        snapshot = metadata["generation_snapshot"]
        self.assertEqual(metadata["generation_summary"], summary)
        for key in ("canonical_model_id", "provider_id", "binding_id"):
            section = {"canonical_model_id": "model", "provider_id": "provider", "binding_id": "binding"}[key]
            self.assertEqual(summary[section]["id"], snapshot[key])
        self.assertEqual(summary["parameters"], snapshot["requested_parameters"])
        wire = snapshot["mapped_request"]["json_body"]
        self.assertEqual(summary["dimensions"]["size"], wire["size"])
        self.assertEqual(summary["quality"], wire["quality"])
        self.assertEqual(summary["output_count"], 3)
        self.assertEqual(summary["task_count"], 1)
        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["queue"]["provider_concurrency"], 1)
        self.assertEqual(summary["queue"]["task_request_concurrency"], 3)

    def test_omitted_parameters_do_not_claim_manifest_defaults_or_exact_dimensions(self):
        body = self.preview({**self.data, "parameters_json": "{}"})
        self.assertEqual(body["summary"]["output_count"], 1)
        self.assertIsNone(body["summary"]["quality"])
        self.assertIsNone(body["summary"]["dimensions"]["size"])
        auto = self.preview({**self.data, "parameters_json": '{"canvas.size":"auto"}'})
        self.assertEqual(auto["summary"]["dimensions"]["size"], "auto")

    def test_changed_prompt_parameters_or_upload_invalidates_review_before_task_creation(self):
        token = self.preview()["preview_token"]
        for data in ({**self.data, "prompt": "different prompt"}, {**self.data, "parameters_json": '{"output.count":1}'}):
            response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "generation_preview_stale")
        files = {"reference_images": ("a.png", self.png("red"), "image/png")}
        token = self.preview(files=files)["preview_token"]
        changed = self.client.post("/api/generate", data=self.data,
                                   files={"reference_images": ("a.png", self.png("blue"), "image/png")},
                                   headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertFalse(self.app.state.storage.list_tasks())
        self.assertFalse(list(self.app.state.reference_asset_root.rglob("*.png")))

    def test_provider_endpoint_binding_remote_model_concurrency_or_key_change_invalidates_review(self):
        self.provider()
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding"}
        for target, field, value in (("provider", "base_url", "https://other.example/v1"),
                                     ("provider", "concurrency", 4), ("provider", "api_key", "changed-secret"),
                                     ("binding", "remote_model_id", "remote/different"),
                                     ("binding", "append_aspect_ratio_prompt", True)):
            with self.subTest(field=field):
                original = self.provider()
                token = self.preview(data)["preview_token"]
                provider = original["providers"][0]
                (provider if target == "provider" else provider["bindings"][0])[field] = value
                self.app.state.ctx.api_settings.replace_snapshot(original)
                response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["detail"]["code"], "generation_preview_stale")
        self.assertFalse(self.app.state.storage.list_tasks())

    def test_provider_change_during_preparation_is_rejected_before_reservation(self):
        from codex_image.webui.routes import generation
        settings = self.provider()
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding", "client_request_id": "preview-config-race-0001"}
        token = self.preview(data)["preview_token"]
        original = generation._prepare_generation_submission
        def change_after_resolution(*args, **kwargs):
            result = original(*args, **kwargs)
            settings["providers"][0]["base_url"] = "https://other.example/v1"
            self.app.state.ctx.api_settings.replace_snapshot(settings)
            return result
        with patch.object(generation, "_prepare_generation_submission", side_effect=change_after_resolution):
            response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "generation_preview_stale")
        self.assertFalse(self.app.state.storage.list_tasks())
        self.assertFalse(self.app.state.ctx.queue_storage.read_state()["waiting"])

    def test_reviewed_submission_and_failed_retry_with_same_stripe_do_not_deadlock(self):
        from codex_image.webui.routes import generation
        self.provider()
        ctx = self.app.state.ctx
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding"}
        failed = self.client.post("/api/generate", data=data).json()["task"]["task_id"]
        ctx.queue_storage.remove_waiting(failed)
        metadata = ctx.storage.read_metadata(failed)
        metadata.update(status="failed", outputs=[{"index": 1, "status": "failed", "error": "temporary failure"}])
        ctx.storage.write_metadata(failed, metadata)
        review = self.preview(data)
        retry_holds_stripe = threading.Event()
        submitting = threading.Event()
        original_retry = ctx.route_helpers["apply_retry_api_provider"]
        original_persist = generation._persist_generation_submission

        class BoundedLock:
            # A regression must fail promptly instead of hanging the suite.
            def __init__(self):
                self.lock = threading.RLock()
            def __enter__(self):
                if not self.lock.acquire(timeout=3):
                    raise AssertionError("provider/task-stripe lock inversion")
                return self
            def __exit__(self, *_):
                self.lock.release()

        def pause_retry(*args, **kwargs):
            retry_holds_stripe.set()
            self.assertTrue(submitting.wait(5))
            return original_retry(*args, **kwargs)

        def persist(*args, **kwargs):
            submitting.set()
            return original_persist(*args, **kwargs)

        with patch.object(ctx.storage, "_task_write_locks", (BoundedLock(),)), \
             patch.object(ctx.api_settings, "_lock", BoundedLock()), \
             patch.dict(ctx.route_helpers, {"apply_retry_api_provider": pause_retry}), \
             patch.object(generation, "_persist_generation_submission", side_effect=persist), \
             ThreadPoolExecutor(max_workers=2) as pool:
            retry = pool.submit(self.client.post, f"/api/tasks/{failed}/retry-failed")
            self.assertTrue(retry_holds_stripe.wait(5))
            submit = pool.submit(self.client.post, "/api/generate", data=data,
                                 headers={PREVIEW_TOKEN_HEADER: review["preview_token"]})
            submitted = submit.result(timeout=8)
            retried = retry.result(timeout=8)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(retried.status_code, 200, retried.text)
        stored = ctx.storage.read_metadata(submitted.json()["task"]["task_id"])
        self.assertEqual(stored["generation_summary"], review["summary"])
        self.assertEqual(len(ctx.storage.list_tasks()), 2)
        self.assertEqual(len(ctx.queue_storage.read_state()["waiting"]), 2)

    def test_settings_change_after_review_acceptance_keeps_the_accepted_snapshot(self):
        from codex_image.webui.routes import generation
        settings = self.provider()
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding"}
        review = self.preview(data)
        original = generation._persist_generation_submission
        def change_after_acceptance(*args, **kwargs):
            settings["providers"][0]["base_url"] = "https://later.example/v1"
            settings["providers"][0]["bindings"][0]["remote_model_id"] = "later-model"
            self.app.state.ctx.api_settings.replace_snapshot(settings)
            return original(*args, **kwargs)
        with patch.object(generation, "_persist_generation_submission", side_effect=change_after_acceptance):
            response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: review["preview_token"]})
        self.assertEqual(response.status_code, 200, response.text)
        metadata = self.app.state.storage.read_metadata(response.json()["task"]["task_id"])
        self.assertEqual(metadata["generation_snapshot"]["provider_base_url"], "https://relay.example/v1")
        self.assertEqual(metadata["generation_snapshot"]["remote_model_id"], "remote/gpt-image-2")
        self.assertEqual(metadata["generation_summary"], review["summary"])
        self.assertEqual(len(self.app.state.storage.list_tasks()), 1)

    def test_expired_malformed_and_restarted_tokens_are_rejected(self):
        with patch("codex_image.webui.generation_summary.time.time", return_value=1000):
            token = self.preview()["preview_token"]
        for stale in (token, "bad", "v1.not-a-time." + "0" * 64):
            result = self.client.post("/api/generate", data=self.data, headers={PREVIEW_TOKEN_HEADER: stale})
            self.assertEqual(result.status_code, 409, result.text)
        fresh = self.preview()["preview_token"]
        with TestClient(self.make_app()) as restarted:
            result = restarted.post("/api/generate", data=self.data, headers={PREVIEW_TOKEN_HEADER: fresh})
            self.assertEqual(result.status_code, 409, result.text)
        self.assertFalse(self.app.state.storage.list_tasks())

    def test_review_token_never_changes_submission_identity_or_blocks_accepted_replay(self):
        data = {**self.data, "client_request_id": "preview-submission-0001"}
        token = self.preview(data)["preview_token"]
        first = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(first.status_code, 200, first.text)
        task_id = first.json()["task"]["task_id"]
        # Invalid, expired, renewed, or process-local tokens cannot hide an
        # accepted task. The same FormData always has the same durable identity.
        for token in ("expired", self.preview(data)["preview_token"]):
            again = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertTrue(again.json()["deduplicated"])
            self.assertEqual(again.json()["task"]["task_id"], task_id)
        asyncio.run(self.app.state.queue_manager.run_available_once())
        self.assertEqual(self.app.state.storage.read_metadata(task_id)["status"], "completed")
        with TestClient(self.make_app()) as restarted:
            again = restarted.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: "pre-restart-token"})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(again.json()["task"]["task_id"], task_id)
        self.assertEqual(len(self.app.state.storage.list_tasks()), 1)
        self.assertFalse(self.app.state.ctx.queue_storage.read_state()["waiting"])

    def test_reserved_unknown_submission_remains_unknown_even_when_review_token_is_stale(self):
        data = {**self.data, "client_request_id": "preview-unknown-0001"}
        token = self.preview(data)["preview_token"]
        with patch("codex_image.webui.routes.generation._commit_reference_assets", side_effect=OSError("disk unavailable")):
            response = TestClient(self.app, raise_server_exceptions=False).post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(response.status_code, 500)
        response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: "expired"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "submission_result_unknown")
        self.assertFalse(self.app.state.ctx.queue_storage.read_state()["waiting"])

    def test_edit_reference_order_dedup_mask_and_files_are_the_actual_persisted_inputs(self):
        ctx = self.app.state.ctx
        asset = ctx.reference_asset_storage.create_or_touch("saved.png", self.png("green"), "image/png")
        gallery = ctx.gallery_storage.create_item("Gallery", "portrait", "gallery.png", self.png("yellow"), "image/png")
        saved_file = ctx.reference_file_storage.create_or_touch(validate_reference_file("saved.txt", b"saved", "text/plain"))
        data = {**self.data, "binding_id": "codex-gpt-image-2-responses", "reference_asset_ids": asset["id"],
                "gallery_image_ids": gallery["id"], "reference_file_ids": saved_file["id"],
                "reference_image_order": json.dumps([{"kind": "gallery", "id": gallery["id"]}, {"kind": "upload", "index": 0},
                                                       {"kind": "asset", "id": asset["id"]}, {"kind": "upload", "index": 1}])}
        files = [("images", ("first.png", self.png("red"), "image/png")),
                 ("images", ("same.png", self.png("red"), "image/png")),
                 ("mask", ("mask.png", self.png("white"), "image/png")),
                 ("reference_files", ("new.txt", b"new", "text/plain"))]
        review = self.preview(data, operation="edit", files=files)
        refs = review["summary"]["references"]
        self.assertEqual(refs["images"], [{"position": 1, "kind": "gallery", "source_index": 0},
                                           {"position": 2, "kind": "upload", "source_index": 0},
                                           {"position": 3, "kind": "asset", "source_index": 0}])
        self.assertEqual([item["kind"] for item in refs["files"]], ["upload", "file"])
        self.assertTrue(refs["mask_present"])
        self.assertEqual(review["summary"]["queue"]["task_request_concurrency"], 1)
        result = self.client.post("/api/edit", data=data, files=files, headers={PREVIEW_TOKEN_HEADER: review["preview_token"]})
        self.assertEqual(result.status_code, 200, result.text)
        metadata = ctx.storage.read_metadata(result.json()["task"]["task_id"])
        self.assertEqual(metadata["generation_summary"], review["summary"])
        self.assertEqual(metadata["reference_image_order"], [
            {"kind": "gallery", "id": gallery["id"]},
            {"kind": "asset", "id": metadata["reference_assets"][0]["id"]},
            {"kind": "asset", "id": asset["id"]},
        ])

    def test_api_concurrency_and_gemini_batch_count_are_not_ui_task_count(self):
        self.provider(concurrency=2)
        result = self.preview({**self.data, "provider_id": "relay", "binding_id": "relay-binding"})["summary"]
        self.assertEqual(result["queue"]["provider_concurrency"], 2)
        self.assertEqual(result["queue"]["task_request_concurrency"], 2)
        self.provider(model="nano-banana-2", codec="gemini_generate_content_image", profile="gemini_generate_content")
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding", "canonical_model_id": "nano-banana-2",
                "parameters_json": json.dumps({"output.count": 2, "canvas.aspect_ratio": "16:9", "canvas.resolution": "2K"})}
        result = self.preview(data)["summary"]
        self.assertEqual((result["task_count"], result["request_count"], result["output_count"]), (1, 1, 2))
        self.assertEqual(result["dimensions"], {"size": None, "aspect_ratio": "16:9", "resolution": "2K"})
        self.assertNotIn("unit-test-secret", json.dumps(result))
        self.assertNotIn("relay.example", json.dumps(result))

    def test_effective_summary_exposes_ignored_protocol_controls_instead_of_claiming_they_apply(self):
        data = {**self.data, "parameters_json": json.dumps({"output.count": 1, "gpt.web_search": True})}
        result = self.preview(data)["summary"]
        self.assertFalse(result["effective_parameters"]["gpt.web_search"])
        self.assertTrue(any(item.get("parameter_id") == "gpt.web_search" for item in result["warnings"]))
        self.provider(model="nano-banana-2", codec="gemini_t8_images", profile="t8_images")
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding", "canonical_model_id": "nano-banana-2",
                "parameters_json": json.dumps({"output.count": 1, "gemini.google_search": True})}
        result = self.preview(data)["summary"]
        self.assertFalse(result["effective_parameters"]["gemini.google_search"])
        self.assertIsNone(result["effective_parameters"]["output.modalities"])
        self.assertEqual({item.get("parameter_id") for item in result["warnings"]}, {"gemini.google_search"})

    def test_unknown_model_provider_and_invalid_reference_constraints_fail_without_persisting(self):
        for fields in ({"canonical_model_id": "unknown"}, {"provider_id": "unknown"}, {"binding_id": "unknown"},
                       {"parameters_json": '{"output.count":5}'}, {"reference_rules": '{"images":"require"}'},
                       {"reference_rules": '{"unexpected":true}'}):
            result = self.client.post("/api/generate/preview", data={**self.data, **fields})
            self.assertIn(result.status_code, (400, 409, 422), result.text)
        unsupported = self.client.post("/api/generate/preview", data=self.data, files={"reference_files": ("note.txt", b"note", "text/plain")})
        self.assertEqual(unsupported.status_code, 400, unsupported.text)
        self.assertEqual(unsupported.json()["detail"]["code"], "reference_files_require_responses")
        self.assertFalse(self.app.state.storage.list_tasks())

    def test_gallery_replacement_after_preview_requires_a_new_review(self):
        gallery = self.app.state.ctx.gallery_storage
        item = gallery.create_item("Gallery", "portrait", "gallery.png", self.png("yellow"), "image/png")
        data = {**self.data, "gallery_image_ids": item["id"]}
        token = self.preview(data)["preview_token"]
        gallery.replace_item_image(item["id"], filename="new.png", data=self.png("blue"), content_type="image/png")
        response = self.client.post("/api/generate", data=data, headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "generation_preview_stale")
        self.assertFalse(self.app.state.storage.list_tasks())

    def test_reference_model_limits_and_mask_incompatibility_have_safe_errors(self):
        files = [("reference_images", (f"image-{i}.png", self.png((i, 0, 0)), "image/png")) for i in range(17)]
        response = self.client.post("/api/generate/preview", data=self.data, files=files)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "image_input_limit_exceeded")
        self.provider(model="nano-banana-2", codec="gemini_generate_content_image", profile="gemini_generate_content")
        data = {**self.data, "provider_id": "relay", "binding_id": "relay-binding", "canonical_model_id": "nano-banana-2",
                "parameters_json": '{"output.count":1}'}
        response = self.client.post("/api/edit/preview", data=data, files={
            "images": ("image.png", self.png("red"), "image/png"),
            "mask": ("mask.png", self.png("white"), "image/png"),
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["detail"]["code"], "mask_input_unsupported")
        self.assertFalse(self.app.state.storage.list_tasks())

    def test_reference_rules_are_bound_to_review_and_cannot_silently_change(self):
        data = {**self.data, "reference_rules": '{"images":"preserve","files":"preserve","order":"preserve"}'}
        token = self.preview(data)["preview_token"]
        changed = {**data, "reference_rules": '{"images":"forbid","files":"preserve","order":"preserve"}'}
        result = self.client.post("/api/generate", data=changed, headers={PREVIEW_TOKEN_HEADER: token})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["detail"]["code"], "generation_preview_stale")

    def test_legacy_clients_still_submit_without_review_tokens(self):
        response = self.client.post("/api/generate", data={"prompt": "legacy", "n": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["task"]["params"]["n"], 2)


if __name__ == "__main__":
    unittest.main()
