from __future__ import annotations
from ..file_permissions import is_private_file_descriptor, restrict_directory, restrict_file_descriptor

from concurrent.futures import Executor, ThreadPoolExecutor
import base64
import binascii
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from threading import Event, RLock
from typing import Callable, Iterator, Literal
from uuid import uuid4
import zipfile

from codex_image.version import APP_VERSION

from .history_backup_format import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    BackupFileEntry,
    BackupTaskEntry,
    safe_backup_member_path,
)
from .history_backup_plan import (
    BackupExportScope,
    BackupScopeSummary,
    PlannedBackupFile,
    PlannedBackupTask,
    TaskBackupPlanner,
)
from .resource_limits import (
    HISTORY_BACKUP_FREE_RATIO,
    HISTORY_BACKUP_MIN_FREE_BYTES,
    MAX_HISTORY_BACKUP_MANIFEST_BYTES,
)


BackupExportStatus = Literal[
    "queued",
    "planning",
    "packing",
    "ready",
    "failed",
    "cancelled",
    "expired",
    "interrupted",
]

_ACTIVE_STATUSES = frozenset({"queued", "planning", "packing"})
_ALL_STATUSES = frozenset(
    {"queued", "planning", "packing", "ready", "failed", "cancelled", "expired", "interrupted"}
)
_PREFIX = "history-backup-"
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_ERROR_CODES = frozenset({
    "backup_export_capacity_unavailable",
    "backup_export_claim_persist_failed",
    "backup_export_executor_unavailable",
    "backup_export_failed",
    "backup_export_insufficient_space",
    "backup_export_interrupted",
    "backup_export_manifest_too_large",
    "metadata_contains_sensitive_fields",
    "request_contains_sensitive_fields",
    "backup_plan_invalid",
    "backup_plan_private_mode_failed",
    "backup_plan_unreadable",
    "backup_scope_invalid",
    "backup_source_changed",
    "backup_source_missing",
    "backup_source_path_invalid",
    "backup_source_unreadable",
})
_PERSISTED_ERROR_CODES = _SAFE_ERROR_CODES | frozenset({"backup_export_claimed"})


@dataclass(frozen=True)
class BackupExportJob:
    job_id: str
    status: BackupExportStatus
    created_at: str
    updated_at: str
    total_tasks: int
    eligible_tasks: int
    excluded_nonterminal: int
    completed_tasks: int
    total_bytes: int
    completed_bytes: int
    filename: str | None
    download_url: str | None
    error_code: str | None
    error_message: str | None
    tasks_with_missing_inputs: int = 0
    missing_input_files: int = 0


@dataclass
class _JobRecord:
    job: BackupExportJob
    scope: BackupExportScope
    cancelled: Event
    ready_at: datetime | None = None
    last_persisted_bytes: int = 0
    last_persisted_tasks: int = 0
    last_persisted_at: float = 0.0


@dataclass(frozen=True)
class _PlannedSpool:
    path: Path
    manifest_path: Path
    task_count: int
    file_count: int
    total_bytes: int
    member_name_bytes: int
    manifest_size: int
    tasks_with_missing_inputs: int
    missing_input_files: int


class _Cancelled(Exception):
    pass


class HistoryBackupExportService:
    def __init__(
        self,
        planner: TaskBackupPlanner,
        root: Path,
        *,
        executor: Executor | None = None,
        clock: Callable[[], datetime] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        min_free_bytes: int = HISTORY_BACKUP_MIN_FREE_BYTES,
        free_ratio: float = HISTORY_BACKUP_FREE_RATIO,
        ttl_seconds: int = 60 * 60,
        chunk_bytes: int = 1024 * 1024,
        max_manifest_bytes: int = MAX_HISTORY_BACKUP_MANIFEST_BYTES,
        app_version: str = APP_VERSION,
        recover_on_init: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_free_bytes < 0 or not 0 <= free_ratio < 1:
            raise ValueError("backup_export_capacity_config_invalid")
        if ttl_seconds <= 0 or chunk_bytes <= 0 or max_manifest_bytes <= 0:
            raise ValueError("backup_export_runtime_config_invalid")
        self.planner = planner
        self.root = Path(root)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="history-backup-export",
        )
        self._owns_executor = executor is None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._disk_usage = disk_usage
        self._min_free_bytes = min_free_bytes
        self._free_ratio = free_ratio
        self._ttl_seconds = ttl_seconds
        self._chunk_bytes = chunk_bytes
        self._max_manifest_bytes = max_manifest_bytes
        self._app_version = app_version
        self._monotonic = monotonic
        self._lock = RLock()
        self._records: dict[str, _JobRecord] = {}
        self._progress_observer: Callable[[BackupExportJob], None] | None = None
        self._accepting = bool(recover_on_init)
        self._recovered = False
        if recover_on_init:
            self.recover_startup()

    def recover_startup(self) -> None:
        with self._lock:
            if self._recovered:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise ValueError("backup_export_root_invalid")
            restrict_directory(self.root)
            self._recover_statuses()
            self._recovered = True
            self._accepting = True
        self.cleanup_expired()

    def create(self, scope: BackupExportScope) -> BackupExportJob:
        if not isinstance(scope, BackupExportScope):
            raise ValueError("backup_scope_invalid")
        self.cleanup_expired()
        now = self._now()
        job_id = uuid4().hex
        job = BackupExportJob(
            job_id=job_id,
            status="queued",
            created_at=_timestamp(now),
            updated_at=_timestamp(now),
            total_tasks=0,
            eligible_tasks=0,
            excluded_nonterminal=0,
            completed_tasks=0,
            total_bytes=0,
            completed_bytes=0,
            filename=None,
            download_url=None,
            error_code=None,
            error_message=None,
        )
        with self._lock:
            if not self._accepting:
                raise ValueError("backup_export_lifecycle_conflict")
            self._records[job_id] = _JobRecord(
                job=job,
                scope=scope,
                cancelled=Event(),
                last_persisted_at=self._monotonic(),
            )
            self._write_status(job)
            try:
                self._executor.submit(self._run, job_id)
            except Exception:
                self._fail(job_id, "backup_export_executor_unavailable")
        return self.get(job_id) or job

    def estimate(self, scope: BackupExportScope) -> BackupScopeSummary:
        if not isinstance(scope, BackupExportScope):
            raise ValueError("backup_scope_invalid")
        return self.planner.summarize_scope(scope)

    def get(self, job_id: str) -> BackupExportJob | None:
        self.cleanup_expired()
        with self._lock:
            record = self._records.get(str(job_id))
            return record.job if record is not None else None

    def cancel(self, job_id: str) -> bool:
        cleanup = False
        with self._lock:
            record = self._records.get(str(job_id))
            if record is None or record.job.status not in _ACTIVE_STATUSES:
                return False
            record.cancelled.set()
            if record.job.status == "queued":
                record.job = self._changed(record.job, status="cancelled")
                self._write_status(record.job)
                cleanup = True
        if cleanup:
            self._delete_job_artifacts(str(job_id), keep_status=True)
        return True

    def discard(self, job_id: str) -> BackupExportJob | None:
        """Forget a retained terminal result and remove its private artifacts."""
        self.cleanup_expired()
        normalized_job_id = str(job_id)
        with self._lock:
            record = self._records.get(normalized_job_id)
            if (
                record is None
                or record.job.status in _ACTIVE_STATUSES
                or record.job.status == "expired"
            ):
                return None
            snapshot = self._changed(
                record.job,
                status="expired",
                filename=None,
                download_url=None,
            )
            self._write_status(snapshot)
            del self._records[normalized_job_id]
        self._cleanup_claimed_tombstone(normalized_job_id)
        return snapshot

    def claim_download(self, job_id: str) -> Path:
        self.cleanup_expired()
        with self._lock:
            record = self._records.get(str(job_id))
            if record is None:
                raise ValueError("backup_export_not_found")
            if record.job.status == "expired":
                raise ValueError("backup_export_not_found")
            if record.job.status != "ready" or not record.job.filename:
                raise ValueError("backup_export_not_ready")
            path = self._artifact_path(record.job.filename)
            if not path.is_file():
                raise ValueError("backup_export_file_missing")
            tombstone = self._changed(
                record.job,
                status="expired",
                filename=None,
                download_url=None,
                error_code="backup_export_claimed",
                error_message="backup_export_claimed",
            )
            try:
                self._write_status(tombstone)
            except Exception:
                raise ValueError("backup_export_claim_persist_failed") from None
            record.job = tombstone
            try:
                _fsync_directory(self.root)
            except OSError:
                raise ValueError("backup_export_claim_persist_failed") from None
            del self._records[job_id]
            return path

    def cleanup_expired(self) -> int:
        now = self._now()
        cleanup_ids: list[str] = []
        newly_expired = 0
        with self._lock:
            for job_id, record in self._records.items():
                if record.job.status == "expired" and record.job.error_code != "backup_export_claimed":
                    cleanup_ids.append(job_id)
                    continue
                if record.job.status != "ready" or record.ready_at is None:
                    continue
                if (now - record.ready_at).total_seconds() <= self._ttl_seconds:
                    continue
                record.job = self._changed(
                    record.job,
                    status="expired",
                    filename=None,
                    download_url=None,
                )
                record.ready_at = None
                self._write_status(record.job)
                cleanup_ids.append(job_id)
                newly_expired += 1
        for job_id in cleanup_ids:
            self._delete_job_artifacts(job_id, keep_status=True)
        return newly_expired

    def close(self) -> None:
        with self._lock:
            self._accepting = False
            active_ids = tuple(
                job_id
                for job_id, record in self._records.items()
                if record.job.status in _ACTIVE_STATUSES
            )
        for job_id in active_ids:
            self.cancel(job_id)
        if self._owns_executor:
            executor = self._executor
            assert isinstance(executor, ThreadPoolExecutor)
            executor.shutdown(wait=True, cancel_futures=False)

    def _run(self, job_id: str) -> None:
        try:
            record = self._record_for_run(job_id)
            if record is None:
                return
            self._check_cancelled(record)
            plan_path = self._plan_path(job_id)
            scope_plan = self.planner.plan_scope(record.scope, plan_path)
            self._check_cancelled(record)
            self._update(
                job_id,
                total_tasks=scope_plan.selected_count,
                eligible_tasks=scope_plan.eligible_count,
                excluded_nonterminal=scope_plan.excluded_nonterminal,
            )

            planned = self._spool_planned_tasks(job_id, record, scope_plan.plan_path)
            self._update(
                job_id,
                total_bytes=planned.total_bytes,
                tasks_with_missing_inputs=planned.tasks_with_missing_inputs,
                missing_input_files=planned.missing_input_files,
            )
            required_bytes = _estimated_archive_bytes(
                planned.total_bytes,
                planned.file_count,
                planned.member_name_bytes,
                planned.manifest_size,
            )
            self._preflight_capacity(required_bytes)
            self._check_cancelled(record)
            self._transition(job_id, "packing")
            self._pack(job_id, record, planned)
        except _Cancelled:
            self._cancelled(job_id)
        except ValueError as exc:
            if self._is_cancelled(job_id):
                self._cancelled(job_id)
            else:
                self._fail(job_id, _safe_error_code(exc))
        except Exception:
            if self._is_cancelled(job_id):
                self._cancelled(job_id)
            else:
                self._fail(job_id, "backup_export_failed")

    def _record_for_run(self, job_id: str) -> _JobRecord | None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None or record.job.status != "queued":
                return None
            record.job = self._changed(record.job, status="planning")
            self._write_status(record.job)
            snapshot = record.job
        self._notify(snapshot)
        return record

    def _spool_planned_tasks(
        self,
        job_id: str,
        record: _JobRecord,
        plan_path: Path,
    ) -> _PlannedSpool:
        spool_path = self._tasks_path(job_id)
        descriptor = os.open(spool_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        task_count = 0
        file_count = 0
        total_bytes = 0
        member_name_bytes = 0
        tasks_with_missing_inputs = 0
        missing_input_files = 0
        try:
            restrict_file_descriptor(descriptor)
            with (
                plan_path.open("r", encoding="utf-8") as source,
                os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination,
            ):
                descriptor = -1
                for line in source:
                    self._check_cancelled(record)
                    task_id = _scope_plan_task_id(line)
                    task = self.planner.plan_task(task_id)
                    if task.missing_input_files:
                        tasks_with_missing_inputs += 1
                        missing_input_files += task.missing_input_files
                    json.dump(
                        _planned_task_json(task),
                        destination,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    destination.write("\n")
                    task_count += 1
                    file_count += len(task.files)
                    for planned_file in task.files:
                        total_bytes += planned_file.entry.size_bytes
                        member_name_bytes += len(planned_file.entry.path.encode("utf-8"))
                destination.flush()
                os.fsync(destination.fileno())
        except OSError as exc:
            raise ValueError("backup_plan_unreadable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        manifest_path = self._write_manifest_file(
            job_id,
            record.scope,
            spool_path,
            task_count=task_count,
            file_count=file_count,
            total_bytes=total_bytes,
        )
        return _PlannedSpool(
            path=spool_path,
            manifest_path=manifest_path,
            task_count=task_count,
            file_count=file_count,
            total_bytes=total_bytes,
            member_name_bytes=member_name_bytes + len(b"manifest.json"),
            manifest_size=manifest_path.stat().st_size,
            tasks_with_missing_inputs=tasks_with_missing_inputs,
            missing_input_files=missing_input_files,
        )

    def _write_manifest_file(
        self,
        job_id: str,
        scope: BackupExportScope,
        spool_path: Path,
        *,
        task_count: int,
        file_count: int,
        total_bytes: int,
    ) -> Path:
        record = self._records.get(job_id)
        if record is None:
            raise _Cancelled()
        manifest_path = self._manifest_path(job_id)
        descriptor = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        prefix = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_FORMAT_VERSION,
            "created_at": record.job.created_at,
            "app_version": self._app_version,
            "scope": {
                "kind": scope.kind,
                "selected_count": record.job.total_tasks,
                "eligible_count": record.job.eligible_tasks,
                "excluded_nonterminal": record.job.excluded_nonterminal,
            },
            "task_count": task_count,
            "file_count": file_count,
            "uncompressed_bytes": total_bytes,
        }
        try:
            restrict_file_descriptor(descriptor)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
                descriptor = -1
                encoded_prefix = json.dumps(
                    prefix,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                destination.write(encoded_prefix[:-1])
                destination.write(',"tasks":[')
                first = True
                for task in self._iter_spooled_tasks(spool_path):
                    if not first:
                        destination.write(",")
                    first = False
                    json.dump(
                        _manifest_task_json(task),
                        destination,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    if destination.tell() > self._max_manifest_bytes:
                        raise ValueError("backup_export_manifest_too_large")
                destination.write("]}")
                destination.flush()
                if destination.tell() > self._max_manifest_bytes:
                    raise ValueError("backup_export_manifest_too_large")
                os.fsync(destination.fileno())
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            self._safe_unlink(manifest_path)
            raise
        return manifest_path

    def _preflight_capacity(self, required_bytes: int) -> None:
        usage = self._disk_usage(self.root)
        try:
            filesystem_total = int(getattr(usage, "total"))
            free = int(getattr(usage, "free"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("backup_export_capacity_unavailable") from exc
        reserve = max(self._min_free_bytes, int(filesystem_total * self._free_ratio))
        if free - required_bytes < reserve:
            raise ValueError("backup_export_insufficient_space")

    def _pack(
        self,
        job_id: str,
        record: _JobRecord,
        planned: _PlannedSpool,
    ) -> None:
        partial_path = self._partial_path(job_id)
        final_name = f"{_PREFIX}{job_id}.zip"
        final_path = self._artifact_path(final_name)
        descriptor = os.open(partial_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            restrict_file_descriptor(descriptor)
            with os.fdopen(descriptor, "w+b") as destination:
                descriptor = -1
                with zipfile.ZipFile(
                    destination,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    allowZip64=True,
                ) as archive:
                    for task in self._iter_spooled_tasks(planned.path):
                        self._check_cancelled(record)
                        for planned_file in task.files:
                            self._write_member(job_id, record, archive, planned_file)
                        self._increment(job_id, completed_tasks=1)
                    with (
                        planned.manifest_path.open("rb") as source,
                        archive.open("manifest.json", "w", force_zip64=True) as member,
                    ):
                        for chunk in iter(lambda: source.read(self._chunk_bytes), b""):
                            self._check_cancelled(record)
                            member.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            self._check_cancelled(record)
            os.replace(partial_path, final_path)
            now = self._now()
            with self._lock:
                current = self._records.get(job_id)
                if current is None:
                    self._safe_unlink(final_path)
                    return
                if current.cancelled.is_set():
                    raise _Cancelled()
                current.job = self._changed(
                    current.job,
                    status="ready",
                    filename=final_name,
                    download_url=f"/api/task-history/backup-exports/{job_id}/download",
                )
                current.ready_at = now
                self._write_status(current.job)
                snapshot = current.job
            self._notify(snapshot)
            self._delete_planning_artifacts(job_id)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            self._safe_unlink(partial_path)
            self._safe_unlink(final_path)
            raise

    def _write_member(self, job_id, record, archive, planned_file) -> None:
        expected = planned_file.entry
        digest = hashlib.sha256()
        actual_size = 0
        with archive.open(expected.path, "w", force_zip64=True) as member:
            if planned_file.inline_bytes is not None:
                payload = planned_file.inline_bytes
                for offset in range(0, len(payload), self._chunk_bytes):
                    self._check_cancelled(record)
                    chunk = payload[offset : offset + self._chunk_bytes]
                    if actual_size + len(chunk) > expected.size_bytes:
                        raise ValueError("backup_source_changed")
                    member.write(chunk)
                    digest.update(chunk)
                    actual_size += len(chunk)
                    self._increment(job_id, completed_bytes=len(chunk))
            elif planned_file.source_path is not None:
                try:
                    with planned_file.source_path.open("rb") as source:
                        while True:
                            self._check_cancelled(record)
                            chunk = source.read(self._chunk_bytes)
                            if not chunk:
                                break
                            if actual_size + len(chunk) > expected.size_bytes:
                                raise ValueError("backup_source_changed")
                            member.write(chunk)
                            digest.update(chunk)
                            actual_size += len(chunk)
                            self._increment(job_id, completed_bytes=len(chunk))
                except OSError as exc:
                    raise ValueError("backup_source_unreadable") from exc
            else:
                raise ValueError("backup_plan_invalid")
        if actual_size != expected.size_bytes or digest.hexdigest() != expected.sha256:
            raise ValueError("backup_source_changed")

    def _iter_spooled_tasks(self, path: Path) -> Iterator[PlannedBackupTask]:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("backup_plan_invalid")
            with path.open("r", encoding="utf-8") as source:
                if not is_private_file_descriptor(source.fileno()):
                    raise ValueError("backup_plan_invalid")
                for line in source:
                    yield _planned_task_from_json_line(line)
        except OSError as exc:
            raise ValueError("backup_plan_unreadable") from exc

    def _transition(self, job_id: str, status: BackupExportStatus) -> None:
        self._update(job_id, status=status)

    def _increment(self, job_id: str, **increments: int) -> None:
        snapshot: BackupExportJob | None = None
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise _Cancelled()
            values = {
                field: getattr(record.job, field) + amount
                for field, amount in increments.items()
            }
            record.job = self._changed(record.job, **values)
            now = self._monotonic()
            should_flush = (
                record.job.completed_bytes - record.last_persisted_bytes >= 8 * 1024 * 1024
                or record.job.completed_tasks - record.last_persisted_tasks >= 25
                or now - record.last_persisted_at >= 1.0
            )
            if should_flush:
                self._write_status(record.job)
                record.last_persisted_bytes = record.job.completed_bytes
                record.last_persisted_tasks = record.job.completed_tasks
                record.last_persisted_at = now
                snapshot = record.job
        if snapshot is not None:
            self._notify(snapshot)

    def _update(self, job_id: str, **changes: object) -> BackupExportJob:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                raise _Cancelled()
            record.job = self._changed(record.job, **changes)
            self._write_status(record.job)
            record.last_persisted_bytes = record.job.completed_bytes
            record.last_persisted_tasks = record.job.completed_tasks
            record.last_persisted_at = self._monotonic()
            snapshot = record.job
        self._notify(snapshot)
        return snapshot

    def _changed(self, job: BackupExportJob, **changes: object) -> BackupExportJob:
        return replace(job, updated_at=_timestamp(self._now()), **changes)

    def _cancelled(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is not None:
                record.job = self._changed(
                    record.job,
                    status="cancelled",
                    filename=None,
                    download_url=None,
                )
                self._write_status(record.job)
                snapshot = record.job
            else:
                snapshot = None
        self._delete_job_artifacts(job_id, keep_status=True)
        if snapshot is not None:
            self._notify(snapshot)

    def _fail(self, job_id: str, code: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is not None:
                record.job = self._changed(
                    record.job,
                    status="failed",
                    filename=None,
                    download_url=None,
                    error_code=code,
                    error_message=code,
                )
                self._write_status(record.job)
                snapshot = record.job
            else:
                snapshot = None
        self._delete_job_artifacts(job_id, keep_status=True)
        if snapshot is not None:
            self._notify(snapshot)

    def _check_cancelled(self, record: _JobRecord) -> None:
        if record.cancelled.is_set():
            raise _Cancelled()

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            record = self._records.get(job_id)
            return record is not None and record.cancelled.is_set()

    def _notify(self, job: BackupExportJob) -> None:
        observer = self._progress_observer
        if observer is not None:
            observer(job)

    def _write_status(self, job: BackupExportJob) -> None:
        path = self._status_path(job.job_id)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self.root,
        )
        temporary = Path(temporary_name)
        try:
            restrict_file_descriptor(descriptor)
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                descriptor = -1
                json.dump(asdict(job), destination, separators=(",", ":"), sort_keys=True)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, path)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    def _recover_statuses(self) -> None:
        interrupted_ids: list[str] = []
        for path in self.root.glob(f"{_PREFIX}*.status.json"):
            job_id = path.name.removeprefix(_PREFIX).removesuffix(".status.json")
            if not _JOB_ID_RE.fullmatch(job_id):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = _job_from_status(payload, job_id)
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue
            if _is_claimed_tombstone(job):
                self._cleanup_claimed_tombstone(job_id)
                continue
            record = _JobRecord(
                job=job,
                scope=BackupExportScope.all(),
                cancelled=Event(),
                ready_at=_parse_timestamp(job.updated_at) if job.status == "ready" else None,
                last_persisted_bytes=job.completed_bytes,
                last_persisted_tasks=job.completed_tasks,
                last_persisted_at=self._monotonic(),
            )
            if job.status in _ACTIVE_STATUSES:
                record.cancelled.set()
                record.job = self._changed(
                    job,
                    status="interrupted",
                    filename=None,
                    download_url=None,
                    error_code="backup_export_interrupted",
                    error_message="backup_export_interrupted",
                )
                self._write_status(record.job)
                interrupted_ids.append(job_id)
            self._records[job_id] = record
        for job_id in interrupted_ids:
            self._delete_job_artifacts(job_id, keep_status=True)

    def _delete_job_artifacts(self, job_id: str, *, keep_status: bool) -> None:
        if not _JOB_ID_RE.fullmatch(job_id):
            return
        paths = [
            self._plan_path(job_id),
            self._tasks_path(job_id),
            self._manifest_path(job_id),
            self._partial_path(job_id),
            self._zip_path(job_id),
        ]
        if not keep_status:
            paths.append(self._status_path(job_id))
        for path in paths:
            self._safe_unlink(path)

    def _cleanup_claimed_tombstone(self, job_id: str) -> bool:
        if not _JOB_ID_RE.fullmatch(job_id):
            return False
        artifact_paths = (
            self._plan_path(job_id),
            self._tasks_path(job_id),
            self._manifest_path(job_id),
            self._partial_path(job_id),
            self._zip_path(job_id),
        )
        status_path = self._status_path(job_id)
        try:
            for path in (*artifact_paths, status_path):
                if path.parent.resolve() != self.root.resolve():
                    return False
                if not path.name.startswith(_PREFIX):
                    return False
            for path in artifact_paths:
                path.unlink(missing_ok=True)
            if any(path.exists() for path in artifact_paths):
                return False
            _fsync_directory(self.root)
            status_path.unlink(missing_ok=True)
            if status_path.exists():
                return False
            _fsync_directory(self.root)
        except OSError:
            return False
        return True

    def _safe_unlink(self, path: Path) -> None:
        try:
            candidate = Path(path)
            if candidate.parent.resolve() != self.root.resolve():
                return
            if not candidate.name.startswith(_PREFIX):
                return
            if not candidate.name.endswith(
                (".status.json", ".plan.jsonl", ".tasks.jsonl", ".manifest.json", ".partial", ".zip")
            ):
                return
            candidate.unlink(missing_ok=True)
        except OSError:
            return

    def _artifact_path(self, filename: str) -> Path:
        return self.root / filename

    def _status_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.status.json"

    def _plan_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.plan.jsonl"

    def _partial_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.partial"

    def _tasks_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.tasks.jsonl"

    def _manifest_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.manifest.json"

    def _delete_planning_artifacts(self, job_id: str) -> None:
        self._safe_unlink(self._plan_path(job_id))
        self._safe_unlink(self._tasks_path(job_id))
        self._safe_unlink(self._manifest_path(job_id))

    def _zip_path(self, job_id: str) -> Path:
        return self.root / f"{_PREFIX}{job_id}.zip"

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _safe_error_code(error: ValueError) -> str:
    code = str(error)
    if code in _SAFE_ERROR_CODES:
        return code
    return "backup_export_failed"


def _job_from_status(payload: object, expected_job_id: str) -> BackupExportJob:
    if not isinstance(payload, dict) or payload.get("job_id") != expected_job_id:
        raise ValueError("backup_status_invalid")
    status = payload.get("status")
    if status not in _ALL_STATUSES:
        raise ValueError("backup_status_invalid")
    counts: dict[str, int] = {}
    for field in (
        "total_tasks",
        "eligible_tasks",
        "excluded_nonterminal",
        "completed_tasks",
        "total_bytes",
        "completed_bytes",
    ):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("backup_status_invalid")
        counts[field] = value
    for field in ("tasks_with_missing_inputs", "missing_input_files"):
        value = payload.get(field, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("backup_status_invalid")
        counts[field] = value
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise ValueError("backup_status_invalid")
    _parse_timestamp(created_at)
    _parse_timestamp(updated_at)
    filename = payload.get("filename")
    expected_filename = f"{_PREFIX}{expected_job_id}.zip"
    if filename not in (None, expected_filename):
        raise ValueError("backup_status_invalid")
    download_url = payload.get("download_url")
    expected_url = f"/api/task-history/backup-exports/{expected_job_id}/download"
    if download_url not in (None, expected_url):
        raise ValueError("backup_status_invalid")
    error_code = payload.get("error_code")
    error_message = payload.get("error_message")
    if error_code is not None and error_code not in _PERSISTED_ERROR_CODES:
        raise ValueError("backup_status_invalid")
    if error_message is not None and error_message != error_code:
        raise ValueError("backup_status_invalid")
    return BackupExportJob(
        job_id=expected_job_id,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
        filename=filename,
        download_url=download_url,
        error_code=error_code,
        error_message=error_message,
        **counts,
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("backup_status_invalid")
    return parsed.astimezone(timezone.utc)


def _is_claimed_tombstone(job: BackupExportJob) -> bool:
    return (
        job.status == "expired"
        and job.error_code == "backup_export_claimed"
        and job.error_message == "backup_export_claimed"
        and job.filename is None
        and job.download_url is None
    )


def _scope_plan_task_id(line: str) -> str:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("backup_plan_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"task_id"}:
        raise ValueError("backup_plan_invalid")
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("backup_plan_invalid")
    return task_id


def _planned_task_json(task: PlannedBackupTask) -> dict[str, object]:
    return {
        "task_id": task.entry.task_id,
        "created_at": task.entry.created_at,
        "fingerprint": task.entry.fingerprint,
        "files": [
            {
                "entry": asdict(item.entry),
                "source_path": str(item.source_path) if item.source_path is not None else None,
                "inline_base64": (
                    base64.b64encode(item.inline_bytes).decode("ascii")
                    if item.inline_bytes is not None
                    else None
                ),
            }
            for item in task.files
        ],
    }


def _planned_task_from_json_line(line: str) -> PlannedBackupTask:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError("backup_plan_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "task_id", "created_at", "fingerprint", "files"
    }:
        raise ValueError("backup_plan_invalid")
    task_id = payload["task_id"]
    created_at = payload["created_at"]
    fingerprint = payload["fingerprint"]
    raw_files = payload["files"]
    if (
        not isinstance(task_id, str)
        or not task_id
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
        or not isinstance(raw_files, list)
    ):
        raise ValueError("backup_plan_invalid")
    planned_files: list[PlannedBackupFile] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "entry", "source_path", "inline_base64"
        }:
            raise ValueError("backup_plan_invalid")
        raw_entry = raw_file["entry"]
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "path", "role", "required", "size_bytes", "sha256", "source_index"
        }:
            raise ValueError("backup_plan_invalid")
        path = raw_entry["path"]
        role = raw_entry["role"]
        required = raw_entry["required"]
        size_bytes = raw_entry["size_bytes"]
        sha256 = raw_entry["sha256"]
        source_index = raw_entry["source_index"]
        if (
            not isinstance(path, str)
            or not isinstance(role, str)
            or not isinstance(required, bool)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or (
                source_index is not None
                and (isinstance(source_index, bool) or not isinstance(source_index, int) or source_index < 1)
            )
        ):
            raise ValueError("backup_plan_invalid")
        try:
            expected_path = safe_backup_member_path(task_id, role, path.rsplit("/", 1)[-1])
        except ValueError as exc:
            raise ValueError("backup_plan_invalid") from exc
        if path != expected_path:
            raise ValueError("backup_plan_invalid")
        source_value = raw_file["source_path"]
        inline_value = raw_file["inline_base64"]
        if (source_value is None) == (inline_value is None):
            raise ValueError("backup_plan_invalid")
        if source_value is not None:
            if not isinstance(source_value, str) or not source_value:
                raise ValueError("backup_plan_invalid")
            source_path = Path(source_value)
            inline_bytes = None
        else:
            if not isinstance(inline_value, str):
                raise ValueError("backup_plan_invalid")
            try:
                inline_bytes = base64.b64decode(inline_value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("backup_plan_invalid") from exc
            source_path = None
        entry = BackupFileEntry(
            path=path,
            role=role,
            required=required,
            size_bytes=size_bytes,
            sha256=sha256,
            source_index=source_index,
        )
        planned_files.append(
            PlannedBackupFile(entry=entry, source_path=source_path, inline_bytes=inline_bytes)
        )
    entries = tuple(item.entry for item in planned_files)
    return PlannedBackupTask(
        entry=BackupTaskEntry(
            task_id=task_id,
            created_at=created_at,
            fingerprint=fingerprint,
            files=entries,
        ),
        files=tuple(planned_files),
    )


def _manifest_task_json(task: PlannedBackupTask) -> dict[str, object]:
    return {
        "task_id": task.entry.task_id,
        "created_at": task.entry.created_at,
        "fingerprint": task.entry.fingerprint,
        "files": [asdict(item.entry) for item in task.files],
    }


def _estimated_archive_bytes(
    payload_bytes: int,
    file_count: int,
    member_name_bytes: int,
    manifest_size: int,
) -> int:
    member_count = file_count + 1
    total_payload = payload_bytes + manifest_size
    metadata_bytes = 256 + (192 * member_count) + (2 * member_name_bytes)
    compression_overhead = (64 * member_count) + (total_payload // 100)
    return total_payload + metadata_bytes + compression_overhead


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = (
    "BackupExportJob",
    "BackupExportStatus",
    "HistoryBackupExportService",
)
