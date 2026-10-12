"""Durable, fail-closed browser submission identities, scoped to the local data store.

Only an opaque ID, a content digest and the allocated task ID are retained here.
The reservation commits *before* queuing work; a lost response or process crash
therefore cannot turn a retry into a second paid generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from .task_metadata import _with_file_urls

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{16,128}$")


def _error(code: str, message: str, status: int = 409) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


class SubmissionIdentity:
    def __init__(self, ctx: Any, request_id: str, fingerprint: str) -> None:
        self.ctx = ctx
        self.request_id = request_id
        self.fingerprint = fingerprint
        self.path = ctx.source_data_root / "webui-submissions.db"

    def _connect(self) -> sqlite3.Connection:
        # Create private before SQLite opens it. Existing files retain their mode.
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS submissions (
            request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            task_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        return connection

    def _matching_task(self, connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT fingerprint, task_id FROM submissions WHERE request_id = ?",
            (self.request_id,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != self.fingerprint:
            raise _error("submission_id_conflict", "This submission ID belongs to different inputs. Restore the original inputs or start a new submission.")
        return str(row[1])

    def _response(self, task_id: str) -> dict[str, Any]:
        try:
            metadata = self.ctx.storage.read_metadata(task_id)
            request_payload = json.loads(self.ctx.storage.request_path(task_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # May still be being persisted, or was interrupted/deleted. Never
            # discard the tombstone: the provider may already have accepted it.
            raise _error("submission_result_unknown", "This submission was already reserved, but its result is not available. Check task history before generating again.") from None
        return {
            "task": _with_file_urls(metadata, self.ctx.active_task_ids,
                self.ctx.gallery_storage, self.ctx.reference_asset_storage,
                self.ctx.reference_file_storage),
            "request": request_payload,
            "client_request_id": self.request_id,
            "deduplicated": True,
        }

    def replay(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            task_id = self._matching_task(connection)
        return self._response(task_id) if task_id else None

    def commit(self, operation: str, create: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._matching_task(connection)
            if existing:
                connection.rollback()
                return self._response(existing)
            task = self.ctx.storage.create_task(operation)
            connection.execute(
                "INSERT INTO submissions (request_id, fingerprint, task_id) VALUES (?, ?, ?)",
                (self.request_id, self.fingerprint, task.task_id),
            )
            connection.commit()
        finally:
            connection.close()
        result = create(task.task_id)
        result["client_request_id"] = self.request_id
        result["deduplicated"] = False
        return result


async def prepare_submission_identity(ctx: Any, request: Request) -> SubmissionIdentity | None:
    form = await request.form()
    values = form.getlist("client_request_id")
    if not values:
        return None  # Older clients remain supported.
    if len(values) != 1 or not isinstance(values[0], str) or not _ID_RE.fullmatch(values[0]):
        raise _error("submission_id_invalid", "The submission ID is invalid.", 400)
    fields: list[tuple[str, Any]] = []
    # Field order is irrelevant; repeated field order (e.g. reference images)
    # is meaningful. Multipart boundaries and upload mtime are not content.
    for key in sorted(set(form.keys()) - {"client_request_id"}):
        for value in form.getlist(key):
            if isinstance(value, UploadFile):
                digest = hashlib.sha256()
                position = value.file.tell()
                await value.seek(0)
                while chunk := await value.read(1024 * 1024):
                    digest.update(chunk)
                await value.seek(position)
                fields.append((key, {"filename": value.filename, "content_type": value.content_type, "sha256": digest.hexdigest()}))
            else:
                fields.append((key, str(value)))
    fingerprint = hashlib.sha256(json.dumps(
        {"endpoint": request.url.path, "fields": fields},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return SubmissionIdentity(ctx, values[0], fingerprint)
