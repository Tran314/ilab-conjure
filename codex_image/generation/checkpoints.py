"""Durable generation receipts. Re-entering a submitted operation never sends it again.

The private journal contains provider responses (including expiring asset URLs),
not request credentials. Only the small, credential-free projection is public.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from codex_image.atomic_files import atomic_write_text
from codex_image.client_types import ImageResult
from codex_image.http import HTTPResponse
from codex_image.http_connection import HTTPTransportFailure

_current: ContextVar[OperationCheckpoint | None] = ContextVar("generation_checkpoint", default=None)


class RegenerationConfirmationRequired(RuntimeError):
    pass


def _valid_state(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    slots = value.get("slots")
    if not isinstance(slots, list) or not slots or any(type(index) is not int or index < 1 for index in slots) or len(set(slots)) != len(slots):
        return False
    combination = (value.get("phase"), value.get("result_certainty"), value.get("safe_retry_action"))
    if combination not in {
        ("submit", "not_submitted", "retry_submit"),
        ("submit", "submitted_unknown", "confirm_regenerate"),
        ("wait", "submitted_unknown", "resume_poll"),
        ("download", "generated", "retry_download"),
        ("completed", "saved", "none"),
    }:
        return False
    if value.get("phase") in {"wait", "download"} and not isinstance(value.get("response"), dict):
        return False
    if value.get("phase") == "wait" and not value.get("provider_task_id"):
        return False
    for name in ("response", "poll_response"):
        if name not in value:
            continue
        receipt = value[name]
        if not isinstance(receipt, dict) or type(receipt.get("status")) is not int or not 200 <= receipt["status"] < 300:
            return False
        if not isinstance(receipt.get("body"), str) or not isinstance(receipt.get("headers"), dict):
            return False
        try:
            base64.b64decode(receipt["body"], validate=True)
        except ValueError:
            return False
    return True


class OperationCheckpoint:
    def __init__(self, path: Path, slots: list[int]) -> None:
        self.path = path
        try:
            self.state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {"schema_version": 1, "slots": slots, "phase": "submit", "result_certainty": "not_submitted", "safe_retry_action": "retry_submit"}
        except (ValueError, UnicodeDecodeError) as exc:
            raise RegenerationConfirmationRequired("Generation receipt is damaged. Confirm possible additional cost before regenerating.") from exc
        if not _valid_state(self.state):
            raise RegenerationConfirmationRequired("Generation receipt is damaged. Confirm possible additional cost before regenerating.")

    def update(self, **fields: Any) -> None:
        self.state.update(fields)
        atomic_write_text(self.path, json.dumps(self.state, ensure_ascii=False), mode=0o600)

    @contextmanager
    def scope(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def request(self, transport: Any, kwargs: dict[str, Any]) -> HTTPResponse:
        method = str(kwargs.get("method", "")).upper()
        path = urlsplit(str(kwargs.get("url", ""))).path
        submit = method == "POST" and (
            path.endswith(("/images/generations", "/images/edits", "/responses", "/chat/completions"))
            or ":generateContent" in path or ":streamGenerateContent" in path
        )
        poll = method == "GET" and "/images/tasks/" in path
        if not submit and not poll:
            return transport.request(**kwargs)
        if submit:
            receipt = self.state.get("response")
            if isinstance(receipt, dict):
                return self._response(receipt)
            if self.state.get("result_certainty") != "not_submitted":
                raise RegenerationConfirmationRequired(
                    "The provider may have accepted this generation. Confirm regeneration and possible additional cost before submitting again."
                )
            # Commit uncertainty BEFORE sending. A crash at any later point cannot
            # turn this operation back into permission to POST.
            self.update(phase="submit", result_certainty="submitted_unknown", safe_retry_action="confirm_regenerate")
        try:
            response = transport.request(**kwargs)
        except HTTPTransportFailure as exc:
            if submit and exc.phase == "upstream_request" and not exc.request_sent:
                # The transport only wraps upstream connection failures before
                # sending. Read/write errors remain ambiguous and are not wrapped.
                self.update(phase="submit", result_certainty="not_submitted", safe_retry_action="retry_submit")
            raise
        if 200 <= response.status < 300:
            try:
                payload = json.loads(response.body)
            except (ValueError, UnicodeDecodeError):
                payload = {}
            task_id = str(payload.get("task_id") or "") if isinstance(payload, dict) else ""
            request_id = next((str(value) for key, value in response.headers.items()
                               if key.lower() in {"x-request-id", "request-id"}), "")
            if not request_id and isinstance(payload, dict):
                request_id = str(payload.get("id") or "")
            receipt = {"status": response.status, "body": base64.b64encode(response.body).decode("ascii"),
                       "headers": {key: value for key, value in response.headers.items()
                                   if key.lower() in {"content-type", "x-request-id", "request-id"}}}
            fields: dict[str, Any] = {}
            if submit:
                fields["response"] = receipt
            if task_id:
                fields.update(provider_task_id=task_id, phase="wait", result_certainty="submitted_unknown", safe_retry_action="resume_poll")
            elif submit:
                fields.update(phase="download", result_certainty="generated", safe_retry_action="retry_download")
            if request_id:
                fields["provider_request_id"] = request_id
            if poll:
                envelope = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(envelope, dict) and (
                    str(envelope.get("status", "")).lower() in {"completed", "success", "done", "finished"}
                    or isinstance(envelope.get("data"), list)
                    or isinstance(envelope.get("data"), dict) and any(envelope["data"].get(key) for key in ("data", "url", "b64_json"))
                ):
                    fields.update(poll_response=receipt, phase="download", result_certainty="generated", safe_retry_action="retry_download")
            self.update(**fields)
        elif submit and response.status in {400, 401, 403, 404, 422, 429}:
            # Explicit rejection permits a new request; a 5xx can hide execution.
            self.update(phase="submit", result_certainty="not_submitted", safe_retry_action="retry_submit")
        return response

    @staticmethod
    def _response(receipt: dict[str, Any]) -> HTTPResponse:
        return HTTPResponse(status=int(receipt["status"]), body=base64.b64decode(receipt["body"]), headers=dict(receipt["headers"]))


def checkpoint_active() -> bool:
    return _current.get() is not None


def checkpoint_request(transport: Any, **kwargs: Any) -> HTTPResponse:
    checkpoint = _current.get()
    if checkpoint is None:
        return transport.request(**kwargs)
    if str(kwargs.get("method")).upper() == "GET" and "/images/tasks/" in urlsplit(str(kwargs.get("url", ""))).path:
        receipt = checkpoint.state.get("poll_response")
        if isinstance(receipt, dict):
            return checkpoint._response(receipt)
    return checkpoint.request(transport, kwargs)


class TaskCheckpoints:
    def __init__(self, storage: Any, task_id: str) -> None:
        self.storage = storage
        self.task_id = task_id
        self.base = storage.metadata_path(task_id).parent

    def operation(self, index: int, slots: list[int]) -> OperationCheckpoint:
        return OperationCheckpoint(self.base / f"{self.task_id}.recovery-{index}.json", slots)

    def result_path(self, index: int) -> Path:
        return self.base / f"{self.task_id}.result-{index}.json"

    def save_result(self, index: int, result: ImageResult) -> None:
        value = asdict(result)
        value["image_bytes"] = base64.b64encode(result.image_bytes).decode("ascii")
        atomic_write_text(self.result_path(index), json.dumps(value, ensure_ascii=False), mode=0o600)

    def read_result(self, index: int) -> ImageResult | None:
        try:
            value = json.loads(self.result_path(index).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        value["image_bytes"] = base64.b64decode(value["image_bytes"])
        return ImageResult(**value)

    def states(self) -> dict[int, dict[str, Any]]:
        states: dict[int, dict[str, Any]] = {}
        for path in self.base.glob(f"{self.task_id}.recovery-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not _valid_state(value):
                    raise ValueError("invalid generation receipt")
            except (OSError, ValueError):
                # Corrupt receipts must fail closed, never permit another POST.
                operation_index = int(path.name.rsplit("-", 1)[1].removesuffix(".json"))
                metadata = self.storage.read_metadata(self.task_id)
                count = int(metadata.get("total_count") or (metadata.get("params") or {}).get("n") or 1)
                value = {"slots": [operation_index] if operation_index else list(range(1, count + 1)),
                         "phase": "submit", "result_certainty": "submitted_unknown", "safe_retry_action": "confirm_regenerate"}
            public = {key: value[key] for key in ("phase", "result_certainty", "safe_retry_action", "provider_task_id", "provider_request_id") if key in value}
            for index in value.get("slots", []):
                states[int(index)] = dict(public)
        for path in self.base.glob(f"{self.task_id}.result-*.json"):
            index = int(path.name.rsplit("-", 1)[1].removesuffix(".json"))
            states.setdefault(index, {}).update(phase="save", result_certainty="generated", safe_retry_action="retry_save")
        return states

    def compact_completed(self, metadata: dict[str, Any]) -> None:
        completed = {record["index"] for record in metadata.get("outputs", [])
                     if isinstance(record, dict) and record.get("status") == "completed" and isinstance(record.get("index"), int)}
        for path in self.base.glob(f"{self.task_id}.recovery-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not _valid_state(value) or not set(value["slots"]).issubset(completed):
                    continue
                if "response" not in value and "poll_response" not in value:
                    continue
                value.pop("response", None)
                value.pop("poll_response", None)
                value.update(phase="completed", result_certainty="saved", safe_retry_action="none")
                atomic_write_text(path, json.dumps(value), mode=0o600)
            except (OSError, ValueError):
                # Retaining a private receipt is safe; do not lose a completed
                # task merely because optional cleanup could not finish.
                continue

    def clear_ambiguous(self, slots: list[int]) -> None:
        for path in self.base.glob(f"{self.task_id}.recovery-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not _valid_state(value):
                    raise ValueError("invalid generation receipt")
            except (ValueError, UnicodeDecodeError):
                operation_index = int(path.name.rsplit("-", 1)[1].removesuffix(".json"))
                if operation_index == 0 or operation_index in slots:
                    path.unlink()
                continue
            if value.get("safe_retry_action") == "confirm_regenerate" and any(index in slots for index in value.get("slots", [])):
                path.unlink()

    def clear_for_regeneration(self, slots: list[int]) -> None:
        self.clear_ambiguous(slots)
        for path in self.base.glob(f"{self.task_id}.recovery-*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if any(index in slots for index in value.get("slots", [])):
                path.unlink()
        for index in slots:
            self.result_path(index).unlink(missing_ok=True)


def project_recovery(storage: Any, task_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    states = TaskCheckpoints(storage, task_id).states()
    records = {int(item["index"]): dict(item) for item in metadata.get("outputs", []) if isinstance(item, dict) and isinstance(item.get("index"), int)}
    # A restored backup or manually removed journal must not convert a known
    # submitted/result-bearing slot into permission for a fresh generation.
    known_remote = {"submitted_unknown", "generated", "saved"}
    legacy_attempted = metadata.get("recovery_schema_version") != 1 and (
        metadata.get("started_at") or metadata.get("attempts")
    )
    for index, record in records.items():
        if index not in states and record.get("status") not in {"completed", "deleted"} and not record.get("result_certainty") and (
            legacy_attempted or metadata.get("recovery_schema_version") != 1 and record.get("started_at")
        ):
            record.update(phase="submit", result_certainty="submitted_unknown", safe_retry_action="confirm_regenerate")
    if not records and not states and legacy_attempted and metadata.get("status") != "completed" and not metadata.get("result_certainty"):
        metadata.update(phase="submit", result_certainty="submitted_unknown", safe_retry_action="confirm_regenerate")
    for index, record in records.items():
        if index not in states and record.get("status") not in {"completed", "deleted"} and (
            record.get("result_certainty") in known_remote
            or record.get("safe_retry_action") in {"confirm_regenerate", "resume_poll", "retry_download", "retry_save"}
        ):
            record.update(result_certainty="submitted_unknown", safe_retry_action="confirm_regenerate")
    if not records and not states and metadata.get("result_certainty") in known_remote and metadata.get("status") != "completed":
        count = int(metadata.get("total_count") or (metadata.get("params") or {}).get("n") or 1)
        records = {index: {"index": index, "status": "failed", "phase": "submit",
                          "result_certainty": "submitted_unknown", "safe_retry_action": "confirm_regenerate"}
                   for index in range(1, count + 1)}
    for index, state in states.items():
        if metadata.get("status") == "completed" and index not in records:
            # Accept-successes/pruning deliberately discards unfinished slots.
            continue
        record = records.setdefault(index, {"index": index, "status": "failed" if metadata.get("status") in {"failed", "partial_failed"} else "running"})
        if record.get("status") == "completed":
            record.update(phase="completed", result_certainty="saved", safe_retry_action="none")
        else:
            record.update(state)
            if metadata.get("status") in {"failed", "partial_failed"} and record.get("status") in {"running", "queued", "waiting", ""}:
                record["status"] = "failed"
    if records:
        metadata["outputs"] = [records[index] for index in sorted(records)]
    active = [record for record in records.values() if record.get("status") not in {"completed", "deleted"}]
    priority = ["confirm_regenerate", "retry_save", "retry_download", "resume_poll", "retry_submit"]
    chosen = next((record for action in priority for record in active if record.get("safe_retry_action") == action), None)
    if chosen:
        for key in ("phase", "result_certainty", "safe_retry_action", "provider_task_id", "provider_request_id"):
            if key in chosen:
                metadata[key] = chosen[key]
    elif records and not active:
        metadata.update(phase="completed", result_certainty="saved", safe_retry_action="none")
    if chosen and metadata.get("status") in {"failed", "partial_failed"}:
        from .errors import GenerationErrorDetail
        snapshot = metadata.get("generation_snapshot") or {}
        existing = metadata.get("generation_error") or {}
        detail = GenerationErrorDetail(
            code=str(existing.get("code") or ("regeneration_confirmation_required" if chosen.get("safe_retry_action") == "confirm_regenerate" else "generation_recovery_required")),
            message=str(metadata.get("error") or metadata.get("last_error") or "Generation recovery is available."),
            retryable=chosen.get("safe_retry_action") != "confirm_regenerate",
            provider_id=str(snapshot.get("provider_id") or metadata.get("api_provider_id") or ""),
            canonical_model_id=str(snapshot.get("canonical_model_id") or ""),
            protocol_profile=str(snapshot.get("protocol_profile") or ""),
            **{key: str(chosen.get(key) or "") for key in ("phase", "result_certainty", "safe_retry_action", "provider_task_id", "provider_request_id")},
        ).to_dict()
        metadata["generation_error"] = detail
        for record in active:
            if record.get("safe_retry_action"):
                record["generation_error"] = {**detail, **{key: record.get(key, "") for key in ("phase", "result_certainty", "safe_retry_action", "provider_task_id", "provider_request_id")}}
    return metadata
