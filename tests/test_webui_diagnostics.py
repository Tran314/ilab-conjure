from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from codex_image.webui.app import create_app
from tests.webui_helpers import FakeImageClient


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app(output_root=self.root, client_factory=FakeImageClient,
                              auth_checker=lambda: True, auto_start_queue=False)
        self.ctx = self.app.state.ctx
        self.client = TestClient(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_health_exposes_verified_local_product_and_instance_identity(self):
        data = self.root / "data"
        with patch.dict(os.environ, {"ILAB_CONJURE_DATA_DIR": str(data / ".." / "data"), "ILAB_CONJURE_LAUNCHER_INSTANCE": "fixture-launch-token"}):
            response = self.client.get("/api/health")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["product"], "ilab-conjure")
        self.assertEqual(body["data_dir"], str(data.resolve()))
        self.assertEqual(body["launcher_instance"], "fixture-launch-token")
        self.assertIn("queue", body)

    def test_diagnostics_are_redacted_by_allowlist_and_do_not_write(self):
        secret = "fixture-api-secret-never-export"
        prompt = "fixture private prompt not for diagnostics"
        records = [
            {"task_id": secret, "prompt": prompt, "error": f"Authorization: Bearer {secret}",
             "request": {"api_key": secret}, "outputs": [{"url": f"https://images.test/private?token={secret}"}],
             "generation_error": {"code": "request_timeout", "message": prompt, "provider_id": secret}},
            {"generation_error": {"code": secret, "message": prompt}},
        ]
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        with patch.object(self.ctx.storage.task_index, "list_summaries", return_value=records), \
             patch.object(self.ctx.storage, "rebuild_task_index", side_effect=AssertionError("must remain read-only")), \
             patch.object(self.ctx.network_egress_settings, "read", return_value={"mode": "custom", "custom_proxy_url": f"http://user:{secret}@proxy.test:8080", "image_request_timeout_seconds": 600, "image_request_retry_count": 0}), \
             patch.dict(os.environ, {"HTTPS_PROXY": f"https://user:{secret}@proxy.test:443"}):
            response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        encoded = json.dumps(body)
        for forbidden in [secret, prompt, "proxy.test", "images.test", "Authorization", "api_key", "custom_proxy_url"]:
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(body["proxy"]["mode"], "custom")
        self.assertTrue(body["proxy"]["configured"])
        self.assertEqual({error["code"] for error in body["recent_errors"]}, {"request_timeout", "unknown_error"})
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_failed_checks_are_reported_without_raw_exception(self):
        with patch.object(self.ctx.queue_storage, "read_state", side_effect=OSError("sensitive raw failure")), \
             patch.object(self.ctx.storage.task_index, "list_summaries", side_effect=OSError("sensitive raw failure")):
            response = self.client.get("/api/diagnostics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["unavailable_checks"], ["queue", "recent_errors"])
        self.assertNotIn("sensitive raw failure", response.text)
        self.assertTrue(response.json()["next_steps"])

    def test_directory_permissions_are_observed_without_write_probe(self):
        self.root.chmod(0o500)
        try:
            body = self.client.get("/api/diagnostics").json()
            self.assertFalse(body["directories"]["output"]["writable"])
            self.assertTrue(body["directories"]["output"]["readable"])
            self.assertTrue(body["next_steps"])
        finally:
            self.root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
