from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from codex_image.webui.app import create_app
from tests.webui_helpers import FakeImageClient


class SubmissionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.make_app()
        self.client = TestClient(self.app)
        self.data = {"prompt": "draw a cup", "client_request_id": "fixture-submission-0001"}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_app(self):
        return create_app(output_root=self.root, client_factory=FakeImageClient,
                          auth_checker=lambda: True, auto_start_queue=False)

    def test_same_request_replays_one_task_and_one_queue_entry(self):
        first = self.client.post("/api/generate", data=self.data)
        again = self.client.post("/api/generate", data=self.data)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(first.json()["task"]["task_id"], again.json()["task"]["task_id"])
        self.assertFalse(first.json()["deduplicated"])
        self.assertTrue(again.json()["deduplicated"])
        self.assertEqual(len(self.app.state.ctx.queue_storage.read_state()["waiting"]), 1)

    def test_same_id_changed_payload_conflicts(self):
        self.client.post("/api/generate", data=self.data)
        response = self.client.post("/api/generate", data={**self.data, "prompt": "a different cup"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "submission_id_conflict")

    def test_restart_replays_original_without_new_submission(self):
        first = self.client.post("/api/generate", data=self.data).json()
        restarted = self.make_app()
        replay = TestClient(restarted).post("/api/generate", data=self.data)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["task"]["task_id"], first["task"]["task_id"])
        self.assertEqual(len(restarted.state.ctx.queue_storage.read_state()["waiting"]), 1)

    def test_lost_response_after_enqueue_replays_without_second_task(self):
        from codex_image.webui.routes import generation
        original = generation._persist_generation_submission
        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated lost response")
        with patch.object(generation, "_persist_generation_submission", side_effect=lost_response):
            first = TestClient(self.app, raise_server_exceptions=False).post("/api/generate", data=self.data)
        self.assertEqual(first.status_code, 500)
        replay = self.client.post("/api/generate", data=self.data)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(self.app.state.ctx.queue_storage.read_state()["waiting"]), 1)

    def test_reserved_but_interrupted_submission_is_fail_closed(self):
        with patch("codex_image.webui.routes.generation._commit_reference_assets", side_effect=OSError("disk unavailable")):
            first = TestClient(self.app, raise_server_exceptions=False).post("/api/generate", data=self.data)
        self.assertEqual(first.status_code, 500)
        replay = TestClient(self.make_app()).post("/api/generate", data=self.data)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"]["code"], "submission_result_unknown")
        self.assertFalse(self.app.state.ctx.queue_storage.read_state()["waiting"])

    def test_concurrent_retries_allocate_once(self):
        barrier = threading.Barrier(8)
        def submit(_):
            barrier.wait()
            return self.client.post("/api/generate", data=self.data)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(submit, range(8)))
        successes = [response.json()["task"]["task_id"] for response in responses if response.status_code == 200]
        self.assertTrue(successes)
        self.assertEqual(len(set(successes)), 1)
        self.assertTrue(all(response.status_code in {200, 409} for response in responses))
        self.assertEqual(len(self.app.state.ctx.storage.task_index.list_summaries()), 1)
        self.assertEqual(len(self.app.state.ctx.queue_storage.read_state()["waiting"]), 1)

    def test_edit_upload_digest_and_endpoint_are_bound(self):
        def png(color):
            image = Image.new("RGB", (20, 20), color)
            output = BytesIO(); image.save(output, format="PNG"); return output.getvalue()
        first = self.client.post("/api/edit", data=self.data, files={"images": ("same.png", png("red"), "image/png")})
        again = self.client.post("/api/edit", data=self.data, files={"images": ("same.png", png("red"), "image/png")})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(first.json()["task"]["task_id"], again.json()["task"]["task_id"])
        changed = self.client.post("/api/edit", data=self.data, files={"images": ("same.png", png("blue"), "image/png")})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.client.post("/api/generate", data=self.data).status_code, 409)

    def test_identity_is_scoped_to_local_data_store_and_contains_no_prompt(self):
        first = self.client.post("/api/generate", data=self.data).json()
        other = create_app(output_root=self.root / "other", client_factory=FakeImageClient,
                           auth_checker=lambda: True, auto_start_queue=False)
        second = TestClient(other).post("/api/generate", data=self.data).json()
        self.assertNotEqual(first["task"]["task_id"], second["task"]["task_id"])
        ledger = self.app.state.ctx.source_data_root / "webui-submissions.db"
        self.assertNotIn(self.data["prompt"].encode(), ledger.read_bytes())

    def test_gallery_delete_cannot_race_reference_publication(self):
        from codex_image.webui.routes import generation
        image = Image.new("RGB", (20, 20), "red")
        payload = BytesIO(); image.save(payload, format="PNG")
        gallery = self.app.state.ctx.gallery_storage
        item = gallery.create_item("Reference", "portrait", "ref.png", payload.getvalue(), "image/png")
        inside_commit = threading.Event()
        release_commit = threading.Event()
        delete_started = threading.Event()
        original = generation._persist_generation_submission
        def paused_commit(*args, **kwargs):
            inside_commit.set()
            self.assertTrue(release_commit.wait(5))
            return original(*args, **kwargs)
        def delete():
            delete_started.set()
            return self.client.delete(f"/api/gallery/{item['id']}")
        with patch.object(generation, "_persist_generation_submission", side_effect=paused_commit), ThreadPoolExecutor(max_workers=2) as pool:
            submit = pool.submit(self.client.post, "/api/generate", data={**self.data, "gallery_image_ids": item["id"]})
            self.assertTrue(inside_commit.wait(5))
            deletion = pool.submit(delete)
            self.assertTrue(delete_started.wait(5))
            release_commit.set()
            submitted = submit.result(timeout=5)
            deleted = deletion.result(timeout=5)
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(deleted.status_code, 409, deleted.text)
        self.assertEqual(deleted.json()["detail"]["code"], "gallery_item_in_use")
        self.assertTrue(gallery.image_path(item["id"]).exists())

    def test_gallery_changed_during_prepare_is_rejected_before_reservation(self):
        from codex_image.webui.routes import generation
        def png(color):
            payload = BytesIO(); Image.new("RGB", (20, 20), color).save(payload, format="PNG"); return payload.getvalue()
        gallery = self.app.state.ctx.gallery_storage
        item = gallery.create_item("Reference", "portrait", "ref.png", png("red"), "image/png")
        original = generation._prepare_generation_submission
        def change_reference(*args, **kwargs):
            result = original(*args, **kwargs)
            gallery.replace_item_image(item["id"], filename="ref.png", data=png("blue"), content_type="image/png")
            return result
        with patch.object(generation, "_prepare_generation_submission", side_effect=change_reference):
            response = self.client.post("/api/generate", data={**self.data, "gallery_image_ids": item["id"]})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "gallery_reference_changed")
        self.assertFalse(self.app.state.ctx.queue_storage.read_state()["waiting"])
        retry = self.client.post("/api/generate", data={**self.data, "gallery_image_ids": item["id"]})
        self.assertEqual(retry.status_code, 200, retry.text)

    def test_legacy_requests_and_invalid_ids(self):
        first = self.client.post("/api/generate", data={"prompt": "legacy"})
        second = self.client.post("/api/generate", data={"prompt": "legacy"})
        self.assertNotEqual(first.json()["task"]["task_id"], second.json()["task"]["task_id"])
        invalid = self.client.post("/api/generate", data={**self.data, "client_request_id": "../invalid"})
        self.assertEqual(invalid.status_code, 400)


if __name__ == "__main__":
    unittest.main()
