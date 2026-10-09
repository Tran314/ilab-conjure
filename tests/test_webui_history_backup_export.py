from __future__ import annotations
from tests.file_security_helpers import assert_private_file

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch
import zipfile

from codex_image.webui.history_backup_export import BackupExportJob, HistoryBackupExportService
from codex_image.webui.history_backup_format import BackupFileEntry, BackupTaskEntry, parse_backup_manifest
from codex_image.webui.history_backup_plan import (
    BackupExportScope,
    BackupScopePlan,
    PlannedBackupFile,
    PlannedBackupTask,
)


class DirectExecutor:
    def submit(self, function, *args, **kwargs):
        function(*args, **kwargs)
        return None


class DeferredExecutor:
    def __init__(self) -> None:
        self.pending = []

    def submit(self, function, *args, **kwargs):
        self.pending.append((function, args, kwargs))
        return None

    def run(self) -> None:
        function, args, kwargs = self.pending.pop(0)
        function(*args, **kwargs)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True)
class DiskUsage:
    total: int
    used: int
    free: int


class FakePlanner:
    def __init__(
        self,
        root: Path,
        task_count: int,
        *,
        payload: bytes = b"payload",
        role: str = "output",
        missing_input_files: int = 0,
    ) -> None:
        self.root = root
        self.task_ids = [f"task-{number:04d}" for number in range(task_count)]
        self.payload = payload
        self.role = role
        self.missing_input_files = missing_input_files
        self.on_plan_task = None
        self.plan_scope_calls = 0

    def plan_scope(self, scope: BackupExportScope, plan_path: Path) -> BackupScopePlan:
        self.plan_scope_calls += 1
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with plan_path.open("w", encoding="utf-8") as destination:
            for task_id in self.task_ids:
                destination.write(json.dumps({"task_id": task_id}, separators=(",", ":")) + "\n")
        os.chmod(plan_path, 0o600)
        return BackupScopePlan(
            selected_count=len(self.task_ids) + 2,
            eligible_count=len(self.task_ids),
            excluded_nonterminal=2,
            plan_path=plan_path,
        )

    def plan_task(self, task_id: str) -> PlannedBackupTask:
        source = self.root / f"{task_id}.bin"
        if not source.exists():
            source.write_bytes(self.payload)
        payload = source.read_bytes()
        entry = BackupFileEntry(
            path=(
                f"tasks/{task_id}/source/{self.role}.json"
                if self.role in {"metadata", "request"}
                else f"tasks/{task_id}/outputs/output-0001.bin"
            ),
            role=self.role,
            required=True,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            source_index=None if self.role in {"metadata", "request"} else 1,
        )
        planned = PlannedBackupTask(
            entry=BackupTaskEntry(
                task_id=task_id,
                created_at="2026-08-01T10:00:00+00:00",
                fingerprint="sha256:" + ("1" * 64),
                files=(entry,),
            ),
            files=(PlannedBackupFile(entry=entry, source_path=source, inline_bytes=None),),
        )
        if self.on_plan_task is not None:
            self.on_plan_task(task_id, source, planned)
        if self.missing_input_files:
            return PlannedBackupTask(
                entry=planned.entry,
                files=planned.files,
                missing_input_files=self.missing_input_files,
            )
        return planned


class WebUIHistoryBackupExportTests(unittest.TestCase):
    def _service(self, root: Path, planner, **overrides) -> HistoryBackupExportService:
        options = {
            "executor": DirectExecutor(),
            "clock": MutableClock(),
            "disk_usage": lambda _path: DiskUsage(total=10_000_000, used=0, free=10_000_000),
            "min_free_bytes": 100,
            "free_ratio": 0.10,
            "ttl_seconds": 60,
            "chunk_bytes": 3,
            "app_version": "test-version",
        }
        options.update(overrides)
        return HistoryBackupExportService(planner, root / "private-backups", **options)

    def test_deferred_recovery_keeps_service_dormant_until_owner_starts_it(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_root = root / "private-backups"
            service = self._service(root, FakePlanner(root, 0), recover_on_init=False)

            self.assertFalse(private_root.exists())
            with self.assertRaisesRegex(ValueError, "backup_export_lifecycle_conflict"):
                service.create(BackupExportScope.all())

            service.recover_startup()
            self.assertTrue(private_root.is_dir())
            self.assertEqual(service.create(BackupExportScope.all()).status, "ready")

    def test_close_atomically_rejects_create_and_waits_for_running_worker(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            entered = Event()
            release = Event()
            original_plan_scope = planner.plan_scope

            def blocking_plan_scope(scope, plan_path):
                entered.set()
                release.wait(2)
                return original_plan_scope(scope, plan_path)

            planner.plan_scope = blocking_plan_scope
            service = self._service(root, planner, executor=None)
            service.create(BackupExportScope.all())
            self.assertTrue(entered.wait(1))

            closer = Thread(target=service.close)
            closer.start()
            for _ in range(100):
                if not service._accepting:
                    break
                Event().wait(0.001)
            with self.assertRaisesRegex(ValueError, "backup_export_lifecycle_conflict"):
                service.create(BackupExportScope.all())
            self.assertTrue(closer.is_alive())

            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())

    def test_recovery_skips_status_with_untrusted_error_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private-backups"
            private.mkdir()
            job_id = "e" * 32
            status = asdict(
                BackupExportJob(
                    job_id=job_id,
                    status="failed",
                    created_at="2026-08-01T00:00:00Z",
                    updated_at="2026-08-01T00:00:00Z",
                    total_tasks=0,
                    eligible_tasks=0,
                    excluded_nonterminal=0,
                    completed_tasks=0,
                    total_bytes=0,
                    completed_bytes=0,
                    filename=None,
                    download_url=None,
                    error_code="backup_export_failed",
                    error_message="/private/secret/request.json",
                )
            )
            status_path = private / f"history-backup-{job_id}.status.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")

            service = self._service(root, FakePlanner(root, 0))

            self.assertIsNone(service.get(job_id))
            self.assertIn("/private/secret/request.json", status_path.read_text(encoding="utf-8"))

    def test_exports_350_tasks_with_monotonic_progress_and_valid_manifest_last(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 350, payload=b"abcdef")
            observed: list[tuple[int, int]] = []
            service = self._service(root, planner)
            service._progress_observer = lambda job: observed.append(  # type: ignore[attr-defined]
                (job.completed_tasks, job.completed_bytes)
            )

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "ready")
            self.assertEqual(
                job.download_url,
                f"/api/task-history/backup-exports/{job.job_id}/download",
            )
            self.assertEqual(job.total_tasks, 352)
            self.assertEqual(job.eligible_tasks, 350)
            self.assertEqual(job.excluded_nonterminal, 2)
            self.assertEqual(job.completed_tasks, 350)
            self.assertEqual(job.completed_bytes, 350 * 6)
            self.assertTrue(observed)
            self.assertEqual(observed, sorted(observed))
            archive_path = service.claim_download(job.job_id)
            assert_private_file(self, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist()[-1], "manifest.json")
                manifest = parse_backup_manifest(archive.read("manifest.json"))
                self.assertEqual(manifest.task_count, 350)
                self.assertEqual(manifest.file_count, 350)
                self.assertEqual(manifest.uncompressed_bytes, 350 * 6)
                self.assertEqual(manifest.scope["kind"], "all")

    def test_ready_job_reports_missing_input_warnings_without_failing_export(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(
                root,
                FakePlanner(root, 2, missing_input_files=3),
            )

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "ready")
            self.assertEqual(job.tasks_with_missing_inputs, 2)
            self.assertEqual(job.missing_input_files, 6)

    def test_rechecks_actual_source_size_and_digest_while_writing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1, payload=b"original")

            def mutate(_task_id, source: Path, _planned: PlannedBackupTask) -> None:
                source.write_bytes(b"changed")

            planner.on_plan_task = mutate
            service = self._service(root, planner)

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "backup_source_changed")
            self.assertEqual(list((root / "private-backups").glob("*.partial")), [])
            self.assertEqual(list((root / "private-backups").glob("*.zip")), [])

    def test_sensitive_metadata_failure_is_safe_and_never_publishes_zip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)

            def reject(_task_id, _source, _planned) -> None:
                raise ValueError("metadata_contains_sensitive_fields")

            planner.on_plan_task = reject
            service = self._service(root, planner)

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "metadata_contains_sensitive_fields")
            self.assertEqual(job.error_message, "metadata_contains_sensitive_fields")
            self.assertFalse(any((root / "private-backups").glob("*.zip")))

    def test_metadata_and_request_changes_after_planning_fail_before_zip_publish(self) -> None:
        for role in ("metadata", "request"):
            with self.subTest(role=role), TemporaryDirectory() as tmp:
                root = Path(tmp)
                planner = FakePlanner(root, 1, payload=b'{"value":"planned"}', role=role)

                def mutate(_task_id, source: Path, _planned: PlannedBackupTask) -> None:
                    source.write_bytes(b'{"value":"changed"}')

                planner.on_plan_task = mutate
                service = self._service(root, planner)

                job = service.create(BackupExportScope.all())

                self.assertEqual(job.status, "failed")
                self.assertEqual(job.error_code, "backup_source_changed")
                self.assertFalse(any((root / "private-backups").glob("*.zip")))

    def test_source_growth_stops_before_writing_beyond_planned_size(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1, payload=b"1234")

            def grow(_task_id, source: Path, _planned: PlannedBackupTask) -> None:
                source.write_bytes(b"x" * 10_000)

            planner.on_plan_task = grow
            service = self._service(root, planner, chunk_bytes=4)

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "backup_source_changed")
            self.assertLessEqual(job.completed_bytes, 4)
            self.assertEqual(list((root / "private-backups").glob("*.partial")), [])

    def test_disk_preflight_leaves_configured_reserve(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1, payload=b"123456")
            service = self._service(
                root,
                planner,
                disk_usage=lambda _path: DiskUsage(total=1_000, used=0, free=205),
                min_free_bytes=100,
                free_ratio=0.20,
            )

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "backup_export_insufficient_space")
            self.assertFalse(any((root / "private-backups").glob("*.zip")))

    def test_disk_preflight_reserves_manifest_and_zip_metadata_overhead(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1, payload=b"123456")
            service = self._service(
                root,
                planner,
                disk_usage=lambda _path: DiskUsage(total=1_000, used=794, free=206),
                min_free_bytes=100,
                free_ratio=0.20,
            )

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "backup_export_insufficient_space")

    def test_cancel_queued_job_deletes_partial_plan_and_zip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = DeferredExecutor()
            service = self._service(root, FakePlanner(root, 1), executor=executor)

            queued = service.create(BackupExportScope.all())
            self.assertEqual(queued.status, "queued")
            self.assertTrue(service.cancel(queued.job_id))
            executor.run()

            cancelled = service.get(queued.job_id)
            self.assertEqual(cancelled.status, "cancelled")
            artifacts = list((root / "private-backups").iterdir())
            self.assertEqual([path.suffix for path in artifacts], [".json"])

    def test_cancel_packing_job_deletes_partial_plan_and_zip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root, FakePlanner(root, 1, payload=b"abcdef"))

            def cancel_when_packing(job) -> None:
                if job.status == "packing":
                    self.assertTrue(service.cancel(job.job_id))

            service._progress_observer = cancel_when_packing  # type: ignore[attr-defined]
            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "cancelled")
            self.assertEqual(list((root / "private-backups").glob("*.partial")), [])
            self.assertEqual(list((root / "private-backups").glob("*.zip")), [])

    def test_queued_cancel_cannot_race_worker_into_planning_or_planner(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            executor = DeferredExecutor()
            service = self._service(root, planner, executor=executor)
            queued = service.create(BackupExportScope.all())
            worker_claimed = Event()
            release_worker = Event()
            observed_statuses: list[str] = []
            original_record_for_run = service._record_for_run  # type: ignore[attr-defined]

            def gated_record_for_run(job_id: str):
                record = original_record_for_run(job_id)
                worker_claimed.set()
                self.assertTrue(release_worker.wait(2))
                return record

            service._record_for_run = gated_record_for_run  # type: ignore[method-assign]
            service._progress_observer = lambda job: observed_statuses.append(job.status)  # type: ignore[attr-defined]
            worker = Thread(target=executor.run)
            worker.start()
            self.assertTrue(worker_claimed.wait(2))

            observations_before_cancel = len(observed_statuses)
            self.assertTrue(service.cancel(queued.job_id))
            release_worker.set()
            worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(service.get(queued.job_id).status, "cancelled")
            self.assertNotIn("planning", observed_statuses[observations_before_cancel:])
            self.assertEqual(planner.plan_scope_calls, 0)

    def test_download_claim_is_atomic_and_one_time(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())

            claimed = service.claim_download(job.job_id)
            self.assertTrue(claimed.is_file())
            self.assertIsNone(service.get(job.job_id))
            with self.assertRaisesRegex(ValueError, "backup_export_not_found"):
                service.claim_download(job.job_id)
            restarted = self._service(root, planner)
            self.assertIsNone(restarted.get(job.job_id))
            with self.assertRaisesRegex(ValueError, "backup_export_not_found"):
                restarted.claim_download(job.job_id)

    def test_download_claim_tombstone_write_failure_preserves_ready_for_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            original_write_status = service._write_status  # type: ignore[attr-defined]

            def fail_tombstone_write(updated_job) -> None:
                if updated_job.error_code == "backup_export_claimed":
                    raise OSError(f"private tombstone failure at {root}")
                original_write_status(updated_job)

            with patch.object(service, "_write_status", side_effect=fail_tombstone_write):
                with self.assertRaisesRegex(ValueError, "^backup_export_claim_persist_failed$") as caught:
                    service.claim_download(job.job_id)
            self.assertNotIn(str(root), str(caught.exception))
            self.assertEqual(service.get(job.job_id).status, "ready")
            self.assertEqual(service.claim_download(job.job_id).name, job.filename)

    def test_download_claim_fsync_failure_leaves_recoverable_tombstone_without_delivery(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"

            with patch(
                "codex_image.webui.history_backup_export._fsync_directory",
                side_effect=OSError(f"private fsync failure at {root}"),
            ) as fsync_directory:
                with self.assertRaisesRegex(ValueError, "^backup_export_claim_persist_failed$") as caught:
                    service.claim_download(job.job_id)

            self.assertNotIn(str(root), str(caught.exception))
            self.assertEqual(fsync_directory.call_count, 1)
            tombstone = service.get(job.job_id)
            self.assertEqual(tombstone.status, "expired")
            self.assertEqual(tombstone.error_code, "backup_export_claimed")
            self.assertIsNone(tombstone.filename)
            self.assertTrue(status.is_file())
            self.assertTrue(archive.is_file())
            restarted = self._service(root, planner)
            self.assertIsNone(restarted.get(job.job_id))
            self.assertFalse(status.exists())
            self.assertFalse(archive.exists())

    def test_recovery_cleans_existing_claimed_tombstone_without_restoring_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            tombstone = replace(
                job,
                status="expired",
                filename=None,
                download_url=None,
                error_code="backup_export_claimed",
                error_message="backup_export_claimed",
            )
            service._write_status(tombstone)  # type: ignore[attr-defined]

            restarted = self._service(root, planner)

            self.assertIsNone(restarted.get(job.job_id))
            self.assertFalse(archive.exists())
            self.assertFalse((root / "private-backups" / f"history-backup-{job.job_id}.status.json").exists())

    def test_tombstone_cleanup_keeps_status_when_artifact_fsync_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"
            tombstone = replace(
                job,
                status="expired",
                filename=None,
                download_url=None,
                error_code="backup_export_claimed",
                error_message="backup_export_claimed",
            )
            service._write_status(tombstone)  # type: ignore[attr-defined]

            with patch(
                "codex_image.webui.history_backup_export._fsync_directory",
                side_effect=OSError("artifact cleanup fsync failed"),
            ):
                restarted = self._service(root, planner)

            self.assertIsNone(restarted.get(job.job_id))
            self.assertFalse(archive.exists())
            self.assertTrue(status.is_file())
            persisted = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(persisted["error_code"], "backup_export_claimed")

    def test_tombstone_cleanup_never_recovers_ready_when_status_fsync_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"
            tombstone = replace(
                job,
                status="expired",
                filename=None,
                download_url=None,
                error_code="backup_export_claimed",
                error_message="backup_export_claimed",
            )
            service._write_status(tombstone)  # type: ignore[attr-defined]

            with patch(
                "codex_image.webui.history_backup_export._fsync_directory",
                side_effect=[None, OSError("status cleanup fsync failed")],
            ) as fsync_directory:
                restarted = self._service(root, planner)

            self.assertEqual(fsync_directory.call_count, 2)
            self.assertIsNone(restarted.get(job.job_id))
            self.assertFalse(archive.exists())
            self.assertFalse(status.exists())

    def test_successful_claim_never_repeats_when_recovery_cleanup_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"
            claimed = service.claim_download(job.job_id)
            self.assertEqual(claimed, archive)
            original_unlink = Path.unlink

            def fail_cleanup_unlink(path: Path, *args, **kwargs):
                if path in {archive, status}:
                    raise OSError(f"private cleanup failure at {path}")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_cleanup_unlink):
                restarted = self._service(root, planner)
                self.assertIsNone(restarted.get(job.job_id))
            self.assertTrue(archive.is_file())
            self.assertTrue(status.is_file())
            restarted_again = self._service(root, planner)
            self.assertIsNone(restarted_again.get(job.job_id))

    def test_recovery_never_leaves_ready_status_after_claimed_zip_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1)
            service = self._service(root, planner)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"
            tombstone = replace(
                job,
                status="expired",
                filename=None,
                download_url=None,
                error_code="backup_export_claimed",
                error_message="backup_export_claimed",
            )
            service._write_status(tombstone)  # type: ignore[attr-defined]
            original_unlink = Path.unlink

            def fail_status_unlink(path: Path, *args, **kwargs):
                if path == status:
                    raise OSError("status cleanup failed")
                return original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_status_unlink):
                restarted = self._service(root, planner)
                self.assertIsNone(restarted.get(job.job_id))
            self.assertFalse(archive.exists())
            persisted = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "expired")
            self.assertEqual(persisted["error_code"], "backup_export_claimed")

    def test_ready_job_expires_after_ttl_and_archive_is_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = MutableClock()
            service = self._service(root, FakePlanner(root, 1), clock=clock, ttl_seconds=60)
            job = service.create(BackupExportScope.all())
            self.assertEqual(job.status, "ready")
            archive = root / "private-backups" / job.filename
            self.assertTrue(archive.is_file())

            clock.advance(61)
            service.cleanup_expired()

            expired = service.get(job.job_id)
            self.assertEqual(expired.status, "expired")
            self.assertFalse(archive.exists())

    def test_discard_ready_job_immediately_deletes_archive_and_status(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root, FakePlanner(root, 1))
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            status = root / "private-backups" / f"history-backup-{job.job_id}.status.json"
            self.assertTrue(archive.is_file())
            self.assertTrue(status.is_file())

            discarded = service.discard(job.job_id)

            self.assertIsNotNone(discarded)
            self.assertEqual(discarded.status, "expired")
            self.assertFalse(archive.exists())
            self.assertFalse(status.exists())
            self.assertIsNone(service.get(job.job_id))

    def test_public_get_expires_ready_job_and_deletes_archive_without_manual_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = MutableClock()
            service = self._service(root, FakePlanner(root, 1), clock=clock, ttl_seconds=60)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename

            clock.advance(61)
            expired = service.get(job.job_id)

            self.assertIsNotNone(expired)
            self.assertEqual(expired.status, "expired")
            self.assertFalse(archive.exists())

    def test_expired_get_retries_private_zip_cleanup_after_transient_unlink_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clock = MutableClock()
            service = self._service(root, FakePlanner(root, 1), clock=clock, ttl_seconds=60)
            job = service.create(BackupExportScope.all())
            archive = root / "private-backups" / job.filename
            original_unlink = Path.unlink

            def fail_zip_once(path: Path, *args, **kwargs):
                if path == archive:
                    raise OSError("transient cleanup failure")
                return original_unlink(path, *args, **kwargs)

            clock.advance(61)
            with patch.object(Path, "unlink", autospec=True, side_effect=fail_zip_once):
                self.assertEqual(service.get(job.job_id).status, "expired")
            self.assertTrue(archive.exists())

            self.assertEqual(service.get(job.job_id).status, "expired")
            self.assertFalse(archive.exists())

    def test_large_chunk_count_throttles_durable_progress_writes_but_finishes_exactly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(
                root,
                FakePlanner(root, 1, payload=b"x" * 1024),
                chunk_bytes=8,
            )
            with patch.object(service, "_write_status", wraps=service._write_status) as write_status:
                job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "ready")
            self.assertEqual(job.completed_bytes, 1024)
            self.assertEqual(job.completed_tasks, 1)
            self.assertLess(write_status.call_count, 20)

    def test_planning_and_manifest_are_disk_streamed_and_manifest_budget_is_enforced(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(
                root,
                FakePlanner(root, 2),
                max_manifest_bytes=1,
            )

            job = service.create(BackupExportScope.all())

            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error_code, "backup_export_manifest_too_large")
            self.assertFalse(hasattr(service, "_plan_tasks"))
            self.assertFalse(hasattr(service, "_manifest_bytes"))
            self.assertFalse(any((root / "private-backups").glob("*.tasks.jsonl")))
            self.assertFalse(any((root / "private-backups").glob("*.manifest.json")))

    def test_cleanup_only_removes_known_private_prefix_artifacts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "private-backups"
            backup_root.mkdir()
            sentinel = backup_root / "sentinel.txt"
            sentinel.write_text("keep", encoding="utf-8")
            outside = root / "outside.zip"
            outside.write_bytes(b"keep")
            service = self._service(root, FakePlanner(root, 1))
            job = service.create(BackupExportScope.all())
            clock = service._clock  # type: ignore[attr-defined]
            clock.advance(61)

            service.cleanup_expired()

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(outside.read_bytes(), b"keep")
            self.assertEqual(service.get(job.job_id).status, "expired")

    def test_status_json_is_private_atomic_and_contains_no_sources_or_payloads(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            planner = FakePlanner(root, 1, payload=b"TOP-SECRET-PAYLOAD")
            deferred = DeferredExecutor()
            service = self._service(root, planner, executor=deferred)
            job = service.create(BackupExportScope.all())
            status_path = root / "private-backups" / f"history-backup-{job.job_id}.status.json"

            assert_private_file(self, status_path)
            status_text = status_path.read_text(encoding="utf-8")
            self.assertNotIn(str(root), status_text)
            self.assertNotIn("TOP-SECRET-PAYLOAD", status_text)
            self.assertNotIn("prompt", status_text)
            with patch(
                "codex_image.webui.history_backup_export.os.replace",
                wraps=os.replace,
            ) as replace:
                service.cancel(job.job_id)
            self.assertTrue(replace.called)

    def test_restart_marks_persisted_active_job_interrupted_and_removes_partial(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            deferred = DeferredExecutor()
            first = self._service(root, FakePlanner(root, 1), executor=deferred)
            queued = first.create(BackupExportScope.all())
            partial = root / "private-backups" / f"history-backup-{queued.job_id}.partial"
            partial.write_bytes(b"incomplete")
            task_spool = root / "private-backups" / f"history-backup-{queued.job_id}.tasks.jsonl"
            manifest_spool = root / "private-backups" / f"history-backup-{queued.job_id}.manifest.json"
            task_spool.write_bytes(b"private plan")
            manifest_spool.write_bytes(b"private manifest")

            restarted = self._service(root, FakePlanner(root, 1))

            recovered = restarted.get(queued.job_id)
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, "interrupted")
            self.assertFalse(partial.exists())
            self.assertFalse(task_spool.exists())
            self.assertFalse(manifest_spool.exists())


if __name__ == "__main__":
    unittest.main()
