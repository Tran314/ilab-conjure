"""Read-only, explicit-whitelist local diagnostics. No logs or raw requests."""
from __future__ import annotations

import os
import platform
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .app_version import app_version_payload

ERROR_NEXT_STEPS = {
    "authentication_failed": "Check the selected provider credentials.",
    "provider_credentials_missing": "Reconnect the selected provider.",
    "provider_credentials_origin_changed": "Review the provider address before a new submission.",
    "rate_limited": "Wait for the provider rate limit to reset.",
    "invalid_parameters": "Review the model inputs and output settings.",
    "operation_unsupported": "Select a provider that supports this operation.",
    "input_limit_exceeded": "Reduce the number of reference images.",
    "snapshot_manifest_incompatible": "Review the queued task model settings.",
    "upstream_error": "Check task recovery status before retrying.",
    "request_timeout": "Check task recovery status; the provider may have accepted the request.",
    "asset_download_failed": "Retry downloading the generated asset without regenerating.",
    "asset_save_failed": "Check data directory permissions and available disk space, then retry saving.",
    "result_unknown": "Check the provider task status before confirming a new paid generation.",
    "generation_result_unknown": "Check the provider task status before confirming a new paid generation.",
    "generation_interrupted": "Use the task's safe recovery action.",
    "unknown_error": "Open the affected task and review its recovery guidance.",
}


def service_identity(ctx: Any) -> dict[str, str]:
    raw = os.environ.get("ILAB_CONJURE_DATA_DIR") or os.environ.get("APP_DATA_DIR")
    path = Path(raw).expanduser() if raw else ctx.webui_settings.path.parent
    return {
        "product": "ilab-conjure",
        "data_dir": str(path.resolve()),
        "launcher_instance": os.environ.get("ILAB_CONJURE_LAUNCHER_INSTANCE", ""),
    }


def _directory_status(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path.resolve()), "exists": False, "readable": False, "writable": False}
    try:
        mode = path.stat().st_mode
        result.update(
            exists=stat.S_ISDIR(mode),
            readable=bool(mode & 0o444) and os.access(path, os.R_OK | os.X_OK),
            writable=bool(mode & 0o222) and os.access(path, os.W_OK | os.X_OK),
            permissions=oct(stat.S_IMODE(mode)),
        )
    except OSError:
        pass
    return result


def diagnostics_payload(ctx: Any) -> dict[str, Any]:
    identity = service_identity(ctx)
    version = app_version_payload(ctx.output_root)
    worker = getattr(ctx.app.state, "queue_worker_task", None)
    queue_health = ctx.queue_worker_health.snapshot()
    unavailable: list[str] = []
    try:
        state = ctx.queue_storage.read_state()
        queue = {"waiting": len(state.get("waiting", [])), "running": len(state.get("running", {}))}
    except Exception:
        queue = {"waiting": None, "running": None}
        unavailable.append("queue")
    try:
        network = ctx.network_egress_settings.read()
        mode = network.get("mode")
        proxy = {
            "mode": mode if mode in {"system", "direct", "custom"} else "unknown",
            "configured": bool(network.get("custom_proxy_url")) if mode == "custom" else bool(
                any(os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"))
            ) if mode == "system" else False,
            "request_timeout_seconds": network.get("image_request_timeout_seconds"),
            "request_retry_count": network.get("image_request_retry_count"),
        }
    except Exception:
        proxy = {"mode": "unknown", "configured": None}
        unavailable.append("proxy")
    errors: Counter[str] = Counter()
    try:
        # Read the index directly. list_recent_tasks may rebuild it on an empty
        # index; diagnostics must never repair, rewrite, or restart anything.
        for task in ctx.storage.task_index.list_summaries(limit=50):
            detail = task.get("generation_error")
            if isinstance(detail, dict) and detail.get("code"):
                code = detail["code"]
                errors[code if isinstance(code, str) and code in ERROR_NEXT_STEPS else "unknown_error"] += 1
            elif task.get("status") in {"failed", "partial_failed"}:
                errors["unknown_error"] += 1
    except Exception:
        unavailable.append("recent_errors")
    paths = {
        "data": _directory_status(Path(identity["data_dir"])),
        "input": _directory_status(ctx.input_root),
        "output": _directory_status(ctx.output_root),
        "source_data": _directory_status(ctx.source_data_root),
    }
    steps: list[str] = []
    if any(not item["writable"] for item in paths.values()):
        steps.append("Check the displayed data directory permissions and disk availability.")
    if queue_health["status"] != "healthy":
        steps.append("Review recent task recovery status before restarting the service.")
    if unavailable:
        steps.append("Some checks could not be read. Review data directory access; no repairs were attempted.")
    return {
        "schema_version": 1,
        "product": identity["product"],
        "version": version["current_version"],
        "mode": version["source"],
        "runtime": {"python": platform.python_version(), "executable": sys.executable,
                    "platform": sys.platform, "architecture": platform.machine()},
        "directories": paths,
        "service": {"reachable": True, "queue_worker_running": bool(worker is not None and not worker.done())},
        "queue": {**queue, "health": queue_health["status"], "consecutive_failures": int(queue_health["consecutive_failures"])},
        "proxy": proxy,
        "recent_errors": [{"code": code, "count": count, "next_step": ERROR_NEXT_STEPS[code]} for code, count in errors.most_common()],
        "recent_error_window": 50,
        "unavailable_checks": unavailable,
        "next_steps": steps,
    }
