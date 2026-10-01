from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from codex_image.webui.app import _history_backup_cleanup_loop, create_app
from codex_image.webui.history_backup_export import BackupExportJob
from codex_image.webui.history_backup_import import (
    BackupImportResult,
    BackupImportSession,
    BackupImportSnapshot,
)
from codex_image.webui.resource_limits import HISTORY_BACKUP_UPLOAD_CHUNK_BYTES
from codex_image.webui.routes.history_backup import _service_http_error
from tests.test_webui_history_backup_import import _full_restore_archive


def export_job(*, job_id: str = "a" * 32, status: str = "ready", filename: str | None = "backup.zip"):
    return BackupExportJob(
        job_id=job_id,
        status=status,
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
        total_tasks=1,
        eligible_tasks=1,
        excluded_nonterminal=0,
        completed_tasks=1,
        total_bytes=3,
        completed_bytes=3,
        filename=filename,
        download_url=f"/api/task-history/backup-exports/{job_id}/download" if filename else None,
        error_code=None,
        error_message=None,
    )


class FakeExportService:
    def __init__(self, archive: Path | None = None) -> None:
        self.job = export_job()
        self.archive = archive
        self.closed = False
        self.created_scope = None
        self._records = {self.job.job_id: object()}

    def create(self, scope):
        self.created_scope = scope
        return self.job

    def estimate(self, scope):
        self.created_scope = scope
        return SimpleNamespace(
            selected_count=8,
            eligible_count=6,
            excluded_nonterminal=2,
        )

    def get(self, job_id):
        return self.job if job_id == self.job.job_id else None

    def cancel(self, job_id):
        if self.get(job_id) is None or self.job.status not in {"queued", "planning", "packing"}:
            return False
        self.job = replace(self.job, status="cancelled")
        return True

    def discard(self, job_id):
        if self.get(job_id) is None or self.job.status in {"queued", "planning", "packing", "expired"}:
            return None
        discarded = replace(
            self.job,
            status="expired",
            filename=None,
            download_url=None,
        )
        self.job = None
        return discarded

    def claim_download(self, job_id):
        if self.get(job_id) is None or self.archive is None:
            raise ValueError("backup_export_not_found")
        archive = self.archive
        self.job = None
        return archive

    def close(self):
        self.closed = True

    def recover_startup(self):
        return None

    def cleanup_expired(self):
        return 0


class FakeImportService:
    def __init__(self, *, create_error: BaseException | None = None) -> None:
        self.create_error = create_error
        self.append_calls = 0
        self.session = BackupImportSession(
            session_id="b" * 32,
            filename="backup.zip",
            size_bytes=3,
            uploaded_bytes=0,
            status="uploading",
            created_at="2026-08-01T00:00:00+00:00",
            updated_at="2026-08-01T00:00:00+00:00",
        )
        self._records = {self.session.session_id: object()}

    def create(self, filename, size_bytes):
        if self.create_error is not None:
            raise self.create_error
        return replace(self.session, filename=filename, size_bytes=size_bytes)

    def get(self, session_id):
        return self.session if session_id == self.session.session_id else None

    def get_snapshot(self, session_id):
        session = self.get(session_id)
        if session is None:
            return None
        result = BackupImportResult((), (), (), (), (), (), ()) if session.status == "restored" else None
        return BackupImportSnapshot(session, result)

    def append_chunk(self, session_id, offset, chunk, sha256):
        self.append_calls += 1
        self.session = replace(self.session, uploaded_bytes=offset + len(chunk))
        return self.session

    def cancel(self, session_id):
        return self.get(session_id) is not None

    def recover_startup(self):
        return None

    def close(self):
        return None


class WebUIHistoryBackupAPITests(unittest.TestCase):
    def test_cleanup_loop_runs_ttl_cleanup_until_stopped(self) -> None:
        calls = 0

        class CleanupService:
            def cleanup_expired(self):
                nonlocal calls
                calls += 1

        async def scenario() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(
                _history_backup_cleanup_loop(CleanupService(), stop, interval_seconds=0.001)
            )
            while calls == 0:
                await asyncio.sleep(0)
            stop.set()
            await task

        asyncio.run(asyncio.wait_for(scenario(), timeout=1))
        self.assertGreaterEqual(calls, 1)

    def test_manifest_shape_error_is_a_public_422(self) -> None:
        error = _service_http_error(ValueError("backup_manifest_invalid"))
        self.assertEqual(error.status_code, 422)
        self.assertEqual(error.detail, {"code": "backup_manifest_invalid", "message": "backup_manifest_invalid"})

    def make_app(self, root: Path):
        return create_app(
            output_root=root / "output",
            input_root=root / "input",
            source_data_root=root / "source-data",
            history_backup_temp_root=root / "private-backups",
            auth_checker=lambda: True,
            auto_start_queue=False,
        )

    def test_backup_routes_are_registered_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            surface = {
                (route.path, method)
                for route in app.routes
                if hasattr(route, "methods")
                for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
            }
            self.assertTrue(
                {
                    ("/api/task-history/backup-exports", "POST"),
                    ("/api/task-history/backup-exports/estimate", "POST"),
                    ("/api/task-history/backup-exports/{job_id}", "GET"),
                    ("/api/task-history/backup-exports/{job_id}", "DELETE"),
                    ("/api/task-history/backup-exports/{job_id}/download", "GET"),
                    ("/api/task-history/backup-imports", "POST"),
                    ("/api/task-history/backup-imports/{session_id}/chunks", "PUT"),
                    ("/api/task-history/backup-imports/{session_id}", "GET"),
                    ("/api/task-history/backup-imports/{session_id}", "DELETE"),
                    ("/api/task-history/backup-imports/{session_id}/validate", "POST"),
                    ("/api/task-history/backup-imports/{session_id}/restore", "POST"),
                }.issubset(surface)
            )

    def test_backup_estimate_reports_exact_scope_counts_without_creating_a_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeExportService()
            app.state.ctx.history_backup_export_service = fake
            app.state.ctx.history_backup_accepting_jobs = True

            response = TestClient(app).post(
                "/api/task-history/backup-exports/estimate",
                json={"scope": "filtered", "filters": {"q": "rabbit"}},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "scope": "filtered",
                "total_tasks": 8,
                "eligible_tasks": 6,
                "excluded_nonterminal": 2,
            })
            self.assertEqual(fake.created_scope.kind, "filtered")
            self.assertEqual(fake.created_scope.filters.q, "rabbit")

    def test_delete_ready_backup_discards_unclaimed_archive_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeExportService()
            app.state.ctx.history_backup_export_service = fake

            response = TestClient(app).delete(
                f"/api/task-history/backup-exports/{fake.job.job_id}"
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "expired")
            self.assertIsNone(fake.job)

    def test_import_get_includes_terminal_result_in_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeImportService()
            fake.session = replace(fake.session, status="restored", uploaded_bytes=3)
            app.state.ctx.history_backup_import_service = fake
            client = TestClient(app)

            response = client.get(f"/api/task-history/backup-imports/{fake.session.session_id}")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["status"], "restored")
            self.assertEqual(payload["result"]["restored"], [])
            self.assertEqual(payload["result"]["cleanup_warnings"], [])

    def test_export_scope_models_reject_extra_and_mutually_exclusive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(self.make_app(Path(tmp)))
            cases = (
                {"scope": "selected", "task_ids": [], "filters": None},
                {"scope": "selected", "task_ids": ["task-a"], "filters": {"q": "x"}},
                {"scope": "filtered", "task_ids": ["task-a"], "filters": {}},
                {"scope": "filtered", "task_ids": [], "filters": {"tag_ids": ["a"], "untagged": True}},
                {"scope": "all", "task_ids": [], "filters": {}},
                {"scope": "all", "task_ids": []},
                {"scope": "selected", "task_ids": ["task-a"], "filters": None},
                {"scope": "all", "task_ids": [], "filters": None, "secret": "do-not-echo"},
            )
            for payload in cases:
                with self.subTest(payload=payload):
                    response = client.post("/api/task-history/backup-exports", json=payload)
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn("do-not-echo", response.text)

    def test_chunk_endpoint_streams_with_exact_headers_and_rejects_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeImportService()
            app.state.ctx.history_backup_import_service = fake
            app.state.ctx.history_backup_accepting_jobs = True
            client = TestClient(app)
            chunk = b"abc"
            response = client.put(
                f"/api/task-history/backup-imports/{fake.session.session_id}/chunks",
                content=chunk,
                headers={
                    "X-Chunk-Offset": "0",
                    "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(fake.append_calls, 1)
            oversized = b"x" * (HISTORY_BACKUP_UPLOAD_CHUNK_BYTES + 1)
            response = client.put(
                f"/api/task-history/backup-imports/{fake.session.session_id}/chunks",
                content=oversized,
                headers={
                    "X-Chunk-Offset": "3",
                    "X-Chunk-SHA256": hashlib.sha256(oversized).hexdigest(),
                },
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(fake.append_calls, 1)

    def test_filtered_scope_reaches_service_as_normalized_domain_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeExportService()
            app.state.ctx.history_backup_export_service = fake
            app.state.ctx.history_backup_accepting_jobs = True
            response = TestClient(app).post(
                "/api/task-history/backup-exports",
                json={
                    "scope": "filtered",
                    "filters": {
                        "q": "rabbit",
                        "tag_ids": [" tag-a ", "tag-a"],
                        "sort": "oldest",
                    },
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(fake.created_scope.kind, "filtered")
            self.assertEqual(fake.created_scope.filters.q, "rabbit")
            self.assertEqual(fake.created_scope.filters.tag_ids, ("tag-a",))
            self.assertEqual(fake.created_scope.filters.sort, "oldest")

    def test_disk_error_is_507_and_does_not_echo_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            secret = "secret-prompt /private/root/request.json"
            app.state.ctx.history_backup_import_service = FakeImportService(
                create_error=OSError(secret)
            )
            app.state.ctx.history_backup_accepting_jobs = True
            response = TestClient(app).post(
                "/api/task-history/backup-imports",
                json={"filename": "backup.zip", "size_bytes": 3},
            )
            self.assertEqual(response.status_code, 507)
            self.assertEqual(response.json()["detail"]["code"], "backup_io_error")
            self.assertNotIn(secret, response.text)

    def test_export_status_preserves_safe_metadata_secret_and_manifest_budget_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            fake = FakeExportService()
            app.state.ctx.history_backup_export_service = fake
            client = TestClient(app)
            for code in (
                "metadata_contains_sensitive_fields",
                "backup_export_manifest_too_large",
            ):
                with self.subTest(code=code):
                    fake.job = replace(
                        fake.job,
                        status="failed",
                        filename=None,
                        download_url=None,
                        error_code=code,
                        error_message=code,
                    )
                    response = client.get(
                        f"/api/task-history/backup-exports/{fake.job.job_id}"
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["error_code"], code)
                    self.assertEqual(response.json()["error_message"], code)

    def test_download_is_attachment_and_deletes_only_claimed_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = self.make_app(root)
            archive = root / "private-backups" / "claimed.zip"
            sentinel = root / "private-backups" / "sentinel.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"zip")
            sentinel.write_bytes(b"keep")
            fake = FakeExportService(archive)
            app.state.ctx.history_backup_export_service = fake
            response = TestClient(app).get(
                f"/api/task-history/backup-exports/{fake.job.job_id}/download"
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(response.content, b"zip")
            self.assertFalse(archive.exists())
            self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_construction_preserves_unowned_artifacts_and_state_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private-backups"
            private.mkdir()
            stale = private / f"history-backup-import-{'c' * 32}.upload.partial"
            stale.write_bytes(b"stale")
            sentinel = private / "unrelated.upload.partial"
            sentinel.write_bytes(b"keep")
            app = self.make_app(root)
            self.assertEqual(stale.read_bytes(), b"stale")
            self.assertEqual(sentinel.read_bytes(), b"keep")
            self.assertIs(
                app.state.history_backup_export_service,
                app.state.ctx.history_backup_export_service,
            )
            self.assertIs(
                app.state.history_backup_import_service,
                app.state.ctx.history_backup_import_service,
            )
            self.assertEqual(app.state.history_backup_temp_root, private)
            self.assertFalse(app.state.ctx.history_backup_accepting_jobs)
            self.assertFalse(app.state.history_backup_accepting_jobs)

    def test_real_empty_history_export_url_download_and_external_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            # Capacity failures have separate coverage; keep this HTTP workflow
            # independent of the host filesystem's reserved-space threshold.
            with TestClient(app) as client, patch.object(
                app.state.ctx.history_backup_export_service, "_disk_usage",
                return_value=SimpleNamespace(total=100 * 1024**3, free=50 * 1024**3),
            ):
                created = client.post(
                    "/api/task-history/backup-exports", json={"scope": "all"}
                )
                self.assertEqual(created.status_code, 200)
                job = created.json()
                for _ in range(100):
                    status = client.get(
                        f"/api/task-history/backup-exports/{job['job_id']}"
                    ).json()
                    if status["status"] == "ready":
                        break
                    time.sleep(0.01)
                self.assertEqual(status["status"], "ready")
                self.assertEqual(
                    status["download_url"],
                    f"/api/task-history/backup-exports/{job['job_id']}/download",
                )
                downloaded = client.get(status["download_url"])
                self.assertEqual(downloaded.status_code, 200)
                self.assertIn(
                    "iLab-CONJURE-backup-",
                    downloaded.headers["content-disposition"],
                )
                self.assertEqual(
                    client.get(status["download_url"]).status_code,
                    404,
                )

    def test_real_import_validate_restore_returns_cleanup_warnings_without_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            payload, _ = _full_restore_archive("api-restore")
            digest = hashlib.sha256(payload).hexdigest()
            with TestClient(app) as client, patch.object(
                app.state.ctx.history_backup_import_service, "_disk_usage",
                return_value=SimpleNamespace(total=100 * 1024**3, free=50 * 1024**3),
            ):
                created = client.post(
                    "/api/task-history/backup-imports",
                    json={"filename": "backup.zip", "size_bytes": len(payload)},
                )
                self.assertEqual(created.status_code, 200)
                session_id = created.json()["session_id"]
                uploaded = client.put(
                    f"/api/task-history/backup-imports/{session_id}/chunks",
                    content=payload,
                    headers={
                        "X-Chunk-Offset": "0",
                        "X-Chunk-SHA256": digest,
                    },
                )
                self.assertEqual(uploaded.status_code, 200)
                validated = client.post(
                    f"/api/task-history/backup-imports/{session_id}/validate"
                )
                self.assertEqual(validated.status_code, 200)
                restored = client.post(
                    f"/api/task-history/backup-imports/{session_id}/restore"
                )
                self.assertEqual(restored.status_code, 200)
                body = restored.json()
                self.assertIn("cleanup_warnings", body)
                serialized = json.dumps(body)
                self.assertNotIn("journal", serialized)
                self.assertNotIn(str(Path(tmp)), serialized)

    def test_shutdown_rejects_new_jobs_and_closes_backup_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            export = FakeExportService()
            export.job = replace(export.job, status="packing")
            export._records = {export.job.job_id: object()}
            imports = FakeImportService()
            app.state.ctx.history_backup_export_service = export
            app.state.ctx.history_backup_import_service = imports
            with TestClient(app) as client:
                self.assertEqual(
                    client.post(
                        "/api/task-history/backup-imports",
                        json={"filename": "backup.zip", "size_bytes": 3},
                    ).status_code,
                    200,
                )
            self.assertFalse(app.state.ctx.history_backup_accepting_jobs)
            self.assertFalse(app.state.history_backup_accepting_jobs)
            self.assertTrue(export.closed)
            response = TestClient(app).post(
                "/api/task-history/backup-imports",
                json={"filename": "backup.zip", "size_bytes": 3},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "backup_lifecycle_conflict")

    def test_control_json_is_strict_bounded_and_error_table_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            client = TestClient(app)
            for payload in (
                {"filename": "backup.zip", "size_bytes": True},
                {"filename": "backup.zip", "size_bytes": "3"},
                {"scope": "filtered", "filters": {"archived": "false"}},
                {"scope": "selected", "task_ids": ["bad/id"]},
            ):
                endpoint = (
                    "/api/task-history/backup-imports"
                    if "filename" in payload
                    else "/api/task-history/backup-exports"
                )
                self.assertEqual(client.post(endpoint, json=payload).status_code, 422)
            oversized = json.dumps({"scope": "all", "padding": "x" * (1024 * 1024)}).encode()
            response = client.post(
                "/api/task-history/backup-exports",
                content=oversized,
                headers={"content-type": "application/json"},
            )
            self.assertEqual(response.status_code, 413)

            from codex_image.webui.routes.history_backup import _service_http_error

            expected = {
                "backup_export_file_missing": 404,
                "backup_import_upload_incomplete": 409,
                "backup_export_not_ready": 409,
                "backup_import_not_validated": 409,
                "backup_import_lifecycle_conflict": 409,
                "backup_import_offset_invalid": 409,
                "backup_import_size_invalid": 413,
                "backup_import_member_too_large": 413,
                "backup_export_capacity_unavailable": 507,
                "backup_import_insufficient_space": 507,
                "backup_export_claim_persist_failed": 507,
                "backup_import_restore_rollback_incomplete": 507,
                "backup_import_upload_unreadable": 507,
                "backup_import_restore_interrupted": 507,
                "backup_import_restore_plan_invalid": 507,
                "backup_import_restore_storage_unavailable": 507,
                "backup_plan_unreadable": 507,
                "backup_import_upload_state_invalid": 409,
                "backup_request_invalid": 422,
            }
            for code, status in expected.items():
                with self.subTest(code=code):
                    error = _service_http_error(ValueError(code))
                    self.assertEqual(error.status_code, status)
                    self.assertNotIn("/private", json.dumps(error.detail))

            unknown = _service_http_error(ValueError("secret_prompt"))
            self.assertEqual(unknown.status_code, 500)
            self.assertEqual(unknown.detail["code"], "backup_internal_error")
            self.assertNotIn("secret_prompt", json.dumps(unknown.detail))

    def test_two_apps_do_not_touch_shared_backup_root_until_lifespan_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_one = self.make_app(root)
            private = root / "private-backups"
            sentinel = private / "sentinel.bin"
            private.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"keep")
            app_two = self.make_app(root)
            self.assertEqual(sentinel.read_bytes(), b"keep")
            with TestClient(app_one):
                with self.assertRaises(RuntimeError):
                    with TestClient(app_two):
                        pass


if __name__ == "__main__":
    unittest.main()
