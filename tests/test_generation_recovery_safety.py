from __future__ import annotations

import asyncio
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from codex_image.generation.checkpoints import (
    OperationCheckpoint, RegenerationConfirmationRequired, TaskCheckpoints,
)
from codex_image.http import HTTPResponse
from codex_image.openai_images_client import OpenAIImagesImageClient
from codex_image.webui.app import create_app
from codex_image.webui.cancellation import finalize_task_cancellation, requeue_task_after_shutdown
from codex_image.webui.recovery import _recover_completed_outputs_from_disk, _recover_queue_state
from tests.helpers import TEST_PNG_BASE64, TEST_PNG_BYTES


class RecoveryTransport:
    def __init__(self, *, disconnect=False, download_fail=False, inline=False):
        self.requests = []
        self.disconnect = disconnect
        self.download_fail = download_fail
        self.inline = inline

    def request(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs["method"] == "POST":
            if self.disconnect:
                raise http.client.RemoteDisconnected("remote end closed connection without response")
            item = {"b64_json": TEST_PNG_BASE64} if self.inline else {"url": "https://assets.example/image.png?signature=private-signed-url"}
            return HTTPResponse(200, json.dumps({"data": [item]}).encode(), {"x-request-id": "request-1", "Set-Cookie": "private-cookie"})
        if self.download_fail:
            raise OSError("asset read failed")
        return HTTPResponse(200, TEST_PNG_BYTES, {"content-type": "image/png"})

    @property
    def posts(self):
        return sum(request["method"] == "POST" for request in self.requests)


class GenerationRecoverySafetyTests(unittest.TestCase):
    def fixture(self, root, transport, *, n=1, auto_retry=False):
        image_client = OpenAIImagesImageClient(api_key="private-api-key", transport=transport)
        app = create_app(output_root=root, client_factory=lambda: image_client,
                         auth_checker=lambda: True, auto_start_queue=False,
                         batch_delay_seconds=0, auto_retry=auto_retry)
        client = TestClient(app)
        created = client.post("/api/generate", data={"prompt": "draw a rabbit", "n": n, "codex_mode": "images"})
        self.assertEqual(created.status_code, 200, created.text)
        return app, client, created.json()["task"]["task_id"]

    def run_failed(self, app):
        with self.assertRaises(Exception):
            asyncio.run(app.state.queue_manager.run_available_once())

    def test_read_disconnect_posts_once_and_requires_cost_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecoveryTransport(disconnect=True)
            app, client, task_id = self.fixture(Path(tmp), transport, auto_retry=True)
            self.run_failed(app)
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(transport.posts, 1)
            self.assertEqual(task["result_certainty"], "submitted_unknown")
            self.assertEqual(task["safe_retry_action"], "confirm_regenerate")
            self.assertEqual(app.state.queue_storage.read_state()["waiting"], [])
            retry = client.post(f"/api/tasks/{task_id}/retry-failed")
            self.assertEqual(retry.status_code, 409, retry.text)
            self.assertEqual(retry.json()["detail"]["code"], "regeneration_confirmation_required")
            self.assertEqual(transport.posts, 1)
            transport.disconnect = False
            retry = client.post(f"/api/tasks/{task_id}/retry-failed", json={"confirm_regenerate": True})
            self.assertEqual(retry.status_code, 200, retry.text)
            asyncio.run(app.state.queue_manager.run_available_once())
            self.assertEqual(transport.posts, 2)

    def test_failed_download_manual_retry_and_restart_never_repeat_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecoveryTransport(download_fail=True)
            app, client, task_id = self.fixture(Path(tmp), transport)
            self.run_failed(app)
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(task["safe_retry_action"], "retry_download")
            receipt = TaskCheckpoints(app.state.storage, task_id).operation(1, [1])
            self.assertIn("private-signed-url", __import__('base64').b64decode(receipt.state["response"]["body"]).decode())
            self.assertNotIn("private-api-key", receipt.path.read_text())
            self.assertNotIn("private-cookie", receipt.path.read_text())
            self.assertNotIn("private-signed-url", json.dumps(task))
            self.assertEqual(client.post(f"/api/tasks/{task_id}/retry-failed").status_code, 200)
            _recover_queue_state(app.state.storage, app.state.queue_storage)
            transport.download_fail = False
            asyncio.run(app.state.queue_manager.run_available_once())
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(transport.posts, 1)

    def test_save_retry_uses_spooled_result_and_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecoveryTransport(inline=True)
            app, client, task_id = self.fixture(Path(tmp), transport)
            with patch.object(app.state.storage, "write_output", side_effect=OSError("disk full")):
                self.run_failed(app)
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(task["safe_retry_action"], "retry_save")
            requests = len(transport.requests)
            self.assertEqual(client.post(f"/api/tasks/{task_id}/retry-failed").status_code, 200)
            asyncio.run(app.state.queue_manager.run_available_once())
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(len(transport.requests), requests)
            self.assertFalse(TaskCheckpoints(app.state.storage, task_id).result_path(1).exists())

    def test_partial_atomic_image_without_metadata_preserves_existing_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecoveryTransport(inline=True)
            app, client, task_id = self.fixture(Path(tmp), transport, n=2)
            storage = app.state.storage
            metadata = storage.read_metadata(task_id)
            metadata.update(status="running", outputs=[{"index": 1, "status": "running"}, {"index": 2, "status": "queued"}])
            storage.write_metadata(task_id, metadata)
            path = storage.write_output(task_id, TEST_PNG_BYTES, "png", index=1)
            self.assertFalse(_recover_completed_outputs_from_disk(storage, metadata))
            self.assertEqual(metadata["outputs"][0]["status"], "completed")
            self.assertEqual(metadata["outputs"][1]["status"], "queued")
            _recover_queue_state(storage, app.state.queue_storage)
            retry = client.post(f"/api/tasks/{task_id}/retry-failed")
            self.assertEqual(retry.status_code, 200, retry.text)
            asyncio.run(app.state.queue_manager.run_available_once())
            task = client.get(f"/api/tasks/{task_id}").json()["task"]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(transport.posts, 1)
            self.assertEqual(path.read_bytes(), TEST_PNG_BYTES)

    def test_stop_and_shutdown_do_not_turn_unknown_into_queued_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, client, task_id = self.fixture(Path(tmp), RecoveryTransport())
            storage = app.state.storage
            operation = TaskCheckpoints(storage, task_id).operation(1, [1])
            operation.update(phase="submit", result_certainty="submitted_unknown", safe_retry_action="confirm_regenerate")
            task = requeue_task_after_shutdown(storage, task_id)
            self.assertEqual(task["status"], "failed")
            task = finalize_task_cancellation(storage, task_id)
            self.assertEqual(task["safe_retry_action"], "confirm_regenerate")
            self.assertTrue(task["cancel_requested"])
            self.assertEqual(task["result_certainty"], "submitted_unknown")

    def test_corrupt_receipt_fails_closed_and_confirmation_can_reset_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, client, task_id = self.fixture(Path(tmp), RecoveryTransport())
            storage = app.state.storage
            checkpoints = TaskCheckpoints(storage, task_id)
            path = checkpoints.operation(1, [1]).path
            path.write_text('{"interrupted":', encoding="utf-8")
            self.assertEqual(checkpoints.states()[1]["safe_retry_action"], "confirm_regenerate")
            with self.assertRaises(RegenerationConfirmationRequired):
                checkpoints.operation(1, [1])
            checkpoints.clear_ambiguous([1])
            self.assertEqual(checkpoints.operation(1, [1]).state["result_certainty"], "not_submitted")

    def test_missing_receipt_after_restore_requires_explicit_regeneration(self):
        for action, certainty in (("confirm_regenerate", "submitted_unknown"), ("resume_poll", "submitted_unknown"), ("retry_download", "generated"), ("retry_save", "generated")):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                transport = RecoveryTransport(inline=True)
                app, client, task_id = self.fixture(Path(tmp), transport)
                storage = app.state.storage
                task = storage.read_metadata(task_id)
                task.update(status="failed", safe_retry_action=action, result_certainty=certainty,
                            outputs=[{"index": 1, "status": "failed", "safe_retry_action": action, "result_certainty": certainty}])
                storage.write_metadata(task_id, task)
                app.state.queue_storage.remove_waiting(task_id)
                response = client.post(f"/api/tasks/{task_id}/retry-failed")
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(transport.posts, 0)
                response = client.post(f"/api/tasks/{task_id}/retry-failed", json={"confirm_regenerate": True})
                self.assertEqual(response.status_code, 200, response.text)
                asyncio.run(app.state.queue_manager.run_available_once())
                self.assertEqual(transport.posts, 1)

    def test_legacy_running_without_output_records_is_unknown_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, client, task_id = self.fixture(Path(tmp), RecoveryTransport())
            storage = app.state.storage
            task = storage.read_metadata(task_id)
            task.pop("recovery_schema_version", None)
            task.update(status="running", attempts=1)
            task.pop("outputs", None)
            storage.write_metadata(task_id, task)
            _recover_queue_state(storage, app.state.queue_storage)
            task = storage.read_metadata(task_id)
            task.pop("recovery_schema_version", None)
            self.assertEqual(task["safe_retry_action"], "confirm_regenerate")
            self.assertEqual(client.post(f"/api/tasks/{task_id}/retry-failed").status_code, 409)

    def test_legacy_attempted_terminal_failure_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, client, task_id = self.fixture(Path(tmp), RecoveryTransport())
            storage = app.state.storage
            task = storage.read_metadata(task_id)
            task.pop("recovery_schema_version", None)
            task.update(status="failed", attempts=1, started_at="2026-10-11T00:00:00Z",
                        outputs=[{"index": 1, "status": "failed", "started_at": "2026-10-11T00:00:00Z", "error": "Image request timed out after 600s"}])
            storage.write_metadata(task_id, task)
            app.state.queue_storage.remove_waiting(task_id)
            self.assertEqual(client.post(f"/api/tasks/{task_id}/retry-failed").status_code, 409)

    def test_legacy_shutdown_queued_started_slot_is_not_automatically_resubmitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = RecoveryTransport()
            app, _, task_id = self.fixture(Path(tmp), transport)
            storage = app.state.storage
            task = storage.read_metadata(task_id)
            task.pop("recovery_schema_version", None)
            task.update(status="queued", attempts=0,
                        outputs=[{"index": 1, "status": "queued", "started_at": "2026-10-11T00:00:00Z"}])
            storage.write_metadata(task_id, task)
            _recover_queue_state(storage, app.state.queue_storage)
            self.assertEqual(storage.read_metadata(task_id)["safe_retry_action"], "confirm_regenerate")
            self.assertEqual(app.state.queue_storage.read_state()["waiting"], [])
            asyncio.run(app.state.queue_manager.run_available_once())
            self.assertEqual(transport.posts, 0)

    def test_indexed_sidebar_exposes_public_recovery_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _, task_id = self.fixture(Path(tmp), RecoveryTransport(disconnect=True))
            self.run_failed(app)
            card = app.state.storage.task_sidebar_card(task_id)
            self.assertEqual(card["safe_retry_action"], "confirm_regenerate")
            self.assertEqual(card["result_certainty"], "submitted_unknown")
            self.assertEqual(card["generation_error"]["safe_retry_action"], "confirm_regenerate")

    def test_schema_valid_but_damaged_receipts_fail_closed(self):
        values = (None, [], {}, {"slots": ["bad"]}, {"slots": []}, {"schema_version": 1, "slots": [1], "phase": "download", "result_certainty": "generated", "safe_retry_action": "retry_download"})
        for value in values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                app, _, task_id = self.fixture(Path(tmp), RecoveryTransport())
                checkpoints = TaskCheckpoints(app.state.storage, task_id)
                path = checkpoints.operation(1, [1]).path
                path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(checkpoints.states()[1]["safe_retry_action"], "confirm_regenerate")
                with self.assertRaises(RegenerationConfirmationRequired):
                    checkpoints.operation(1, [1])
                task = app.state.storage.read_metadata(task_id)
                task["status"] = "failed"
                app.state.storage.write_metadata(task_id, task)
                checkpoints.clear_for_regeneration([1])
                self.assertFalse(path.exists())

    def test_gemini_batch_download_failure_never_collapses_output_positions(self):
        from io import BytesIO
        from PIL import Image
        from codex_image.providers.gemini import GeminiGenerateContentAdapter
        from codex_image.providers.registry import ProviderRegistry, default_registry
        from codex_image.webui.execution_plan_client import ExecutionPlanImageClient
        from codex_image.webui.executor_transport import _call_image_client
        from tests.test_gemini_image_protocols import _plan
        output = BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="PNG")
        second_png = output.getvalue()
        class Transport:
            def __init__(self):
                self.requests = []
                self.fail = True
            def request(self, **kwargs):
                self.requests.append(kwargs)
                if kwargs["method"] == "POST":
                    parts = [{"fileData": {"fileUri": f"https://assets.example/{name}.png", "mimeType": "image/png"}} for name in ("A", "B")]
                    return HTTPResponse(200, json.dumps({"candidates": [{"content": {"parts": parts}}]}).encode(), {})
                if kwargs["url"].endswith("A.png"):
                    return HTTPResponse(503 if self.fail else 200, b"unavailable" if self.fail else TEST_PNG_BYTES, {"content-type": "image/png"})
                return HTTPResponse(200, second_png, {"content-type": "image/png"})
        with tempfile.TemporaryDirectory() as tmp:
            app, _, task_id = self.fixture(Path(tmp), RecoveryTransport(), n=2)
            checkpoints = TaskCheckpoints(app.state.storage, task_id)
            transport = Transport()
            plan = _plan(profile="gemini_generate_content", codec="gemini_generate_content_image", base_url="https://provider.example")
            registry = ProviderRegistry(protocols={"gemini_generate_content": GeminiGenerateContentAdapter(transport=transport)},
                                        codecs={"gemini_generate_content_image": default_registry().codec("gemini_generate_content_image")})
            def make_client():
                client = ExecutionPlanImageClient(plan, None, registry=registry)
                client.prepare_output_count(2)
                return client
            async def output_for(client, index):
                return await client.call_output(lambda: _call_image_client(None, {}, client.generate_image),
                                                checkpoints=checkpoints, output_number=index, output_numbers=[1, 2])
            with self.assertRaises(Exception):
                asyncio.run(output_for(make_client(), 1))
            self.assertIsNone(checkpoints.read_result(1))
            self.assertIsNone(checkpoints.read_result(2))
            transport.fail = False
            client = make_client()
            self.assertEqual(asyncio.run(output_for(client, 1)).image_bytes, TEST_PNG_BYTES)
            self.assertEqual(asyncio.run(output_for(client, 2)).image_bytes, second_png)
            self.assertEqual(sum(request["method"] == "POST" for request in transport.requests), 1)

    def test_t8_resumes_get_on_same_durable_task_id(self):
        from codex_image.providers.t8 import T8ImagesAdapter
        from tests.test_gemini_image_protocols import _plan
        class Transport:
            def __init__(self):
                self.requests = []
                self.fail = True
            def request(self, **kwargs):
                self.requests.append(kwargs)
                if kwargs["method"] == "POST":
                    return HTTPResponse(200, b'{"task_id":"same-task"}', {})
                if self.fail:
                    raise TimeoutError("poll timed out")
                return HTTPResponse(200, json.dumps({"data": {"status": "SUCCESS", "data": {"data": [{"b64_json": TEST_PNG_BASE64}]}}}).encode(), {})
        with tempfile.TemporaryDirectory() as tmp:
            transport = Transport()
            adapter = T8ImagesAdapter(transport=transport, sleep=lambda _: None, poll_attempts=1)
            plan = _plan(profile="t8_images", codec="gemini_t8_images", base_url="https://provider.example/v1")
            path = Path(tmp) / "receipt.json"
            with OperationCheckpoint(path, [1]).scope(), self.assertRaises(Exception):
                adapter.execute(plan)
            receipt = OperationCheckpoint(path, [1])
            self.assertEqual(receipt.state["provider_task_id"], "same-task")
            self.assertEqual(receipt.state["safe_retry_action"], "resume_poll")
            transport.fail = False
            with receipt.scope():
                result = adapter.execute(plan)
            self.assertEqual(result.assets[0].image_bytes, TEST_PNG_BYTES)
            self.assertEqual([r["method"] for r in transport.requests], ["POST", "GET", "GET"])
            self.assertEqual(transport.requests[-1]["url"], "https://provider.example/v1/images/tasks/same-task")


if __name__ == "__main__":
    unittest.main()
