"""Read-only generation summaries and short-lived, process-local review tokens.

A token attests to the resolved plan, not to a future provider result or price.
It is deliberately independent of durable submission identities: an accepted
submission must remain replayable after this token expires or the app restarts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import HTTPException

from codex_image.generation.snapshot import generation_snapshot
from codex_image.providers.contracts import ExecutionPlan
from codex_image.webui.auth_routing import (
    DEFAULT_API_IMAGES_CONCURRENCY,
    MAX_API_IMAGES_CONCURRENCY,
)
from codex_image.webui.reference_image_order import order_reference_sources
from codex_image.webui.resource_limits import MAX_TASK_IMAGE_BYTES

PREVIEW_TOKEN_HEADER = "X-Generation-Preview-Token"
PREVIEW_TOKEN_LIFETIME_SECONDS = 15 * 60


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _stale_preview() -> HTTPException:
    return HTTPException(status_code=409, detail={
        "code": "generation_preview_stale",
        "message": "The reviewed generation settings changed or expired. Review the updated summary before submitting again.",
    })


class GenerationPreviewTokens:
    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def _signature(self, issued_at: int, material: dict[str, Any]) -> str:
        return hmac.new(self._key, _json({"issued_at": issued_at, "plan": material}), hashlib.sha256).hexdigest()

    def issue(self, material: dict[str, Any]) -> str:
        issued_at = int(time.time())
        return f"v1.{issued_at}.{self._signature(issued_at, material)}"

    def verify(self, token: str, material: dict[str, Any]) -> None:
        try:
            version, raw_time, signature = token.split(".")
            issued_at = int(raw_time)
            age = int(time.time()) - issued_at
            valid = (
                version == "v1" and len(signature) == 64
                and 0 <= age <= PREVIEW_TOKEN_LIFETIME_SECONDS
                and hmac.compare_digest(signature, self._signature(issued_at, material))
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise _stale_preview()


def preview_token_material(plan: ExecutionPlan, summary: dict[str, Any], *, params: dict[str, Any], prompt_locale: str) -> dict[str, Any]:
    """Never returned or persisted. Only its keyed digest leaves the process."""
    command = plan.command
    return {
        "snapshot": generation_snapshot(plan),
        "summary": summary,
        "params": params,
        "prompt_locale": prompt_locale,
        "prompt_sha256": _digest(command.prompt),
        "instructions_sha256": _digest(command.instructions or ""),
        "main_model": command.main_model,
        "images": [_digest(image.data_url) for image in command.image_inputs],
        "mask": _digest(command.mask_image) if command.mask_image else None,
        "files": [{"filename": item.filename, "mime_type": item.mime_type, "detail": item.detail,
                   "data_sha256": _digest(item.file_data)} for item in command.reference_files],
        # Changing credentials also requires another review, without revealing
        # even an unkeyed credential hash in the response or token.
        "credential_sha256": _digest(plan.provider.api_key),
    }


def reference_image_summary(
    *, uploads: list[Any], assets: list[dict[str, Any]],
    selected_assets: list[dict[str, Any]], galleries: list[dict[str, Any]],
    image_order: list[dict[str, str]] | None, raw_order: str | None,
) -> list[dict[str, Any]]:
    origins: dict[tuple[str, str], dict[str, Any]] = {}
    if raw_order is not None:
        selected_indices = {str(item["id"]): index for index, item in enumerate(selected_assets)}
        gallery_indices = {str(item["id"]): index for index, item in enumerate(galleries)}
        # normalize_reference_image_order has already validated every entry.
        for entry in json.loads(raw_order):
            kind = entry["kind"]
            if kind == "upload":
                index = entry["index"]
                key = ("asset", uploads[index].sha256)
            else:
                key = (kind, entry["id"])
                index = (selected_indices if kind == "asset" else gallery_indices)[entry["id"]]
            origins.setdefault(key, {"kind": kind, "source_index": index})
    else:
        for index, item in enumerate(uploads):
            origins.setdefault(("asset", item.sha256), {"kind": "upload", "source_index": index})
        for index, item in enumerate(selected_assets):
            origins.setdefault(("asset", str(item["id"])), {"kind": "asset", "source_index": index})
        for index, item in enumerate(galleries):
            origins.setdefault(("gallery", str(item["id"])), {"kind": "gallery", "source_index": index})
    sources = [
        {"kind": kind, "id": str(item["id"])}
        for kind, items in (("asset", assets), ("gallery", galleries)) for item in items
    ]
    return [
        {"position": index, **origins[(item["kind"], item["id"])]}
        for index, item in enumerate(order_reference_sources(sources, image_order), start=1)
    ]


def reference_file_summary(prepared: Any) -> list[dict[str, Any]]:
    uploads = {item.asset_id: index for index, item in enumerate(prepared.uploads)}
    selected = {identity: index for index, identity in enumerate(prepared.selected_asset_ids)}
    return [{
        "position": position,
        "kind": "upload" if str(item["id"]) in uploads else "file",
        "source_index": (uploads if str(item["id"]) in uploads else selected)[str(item["id"])],
        "mime_type": str(item.get("mime_type") or ""),
    } for position, item in enumerate(prepared.records, start=1)]


def _effective_parameters(plan: ExecutionPlan) -> dict[str, Any]:
    """Project values from the encoded request, never infer provider defaults.

    Missing controls are null (provider-selected), rather than manifest UI
    defaults, which are not necessarily inserted by the transport codec.
    """
    body = dict(plan.protocol_request.json_body or plan.protocol_request.form_fields)
    count = int(plan.command.parameters.get("output.count") or 1)
    if plan.binding.parameter_codec.startswith("gpt_"):
        values = body
        if plan.binding.protocol_profile.endswith("responses"):
            values = next((tool for tool in body.get("tools", []) if tool.get("type") == "image_generation"), {})
        mapping = {
            "canvas.size": "size", "gpt.quality": "quality", "gpt.background": "background",
            "output.format": "output_format", "gpt.moderation": "moderation",
            "gpt.output_compression": "output_compression", "gpt.input_fidelity": "input_fidelity",
            "gpt.partial_images": "partial_images",
        }
        result = {key: values.get(wire_key) for key, wire_key in mapping.items()}
        result["gpt.web_search"] = any(tool.get("type") == "web_search" for tool in body.get("tools", []))
        # Transparent backgrounds can be implemented by a provider-specific
        # instruction instead of a native parameter, but remain the intent.
        if plan.command.parameters.get("gpt.background") == "transparent":
            result["gpt.background"] = "transparent"
    else:
        config = body.get("generationConfig", body.get("generation_config", {}))
        if isinstance(config, str):
            config = json.loads(config)
        image = config.get("imageConfig") or (config.get("responseFormat") or {}).get("image") or {}
        result = {
            "canvas.aspect_ratio": body.get("aspect_ratio", image.get("aspectRatio")),
            "canvas.resolution": body.get("resolution", body.get("image_size", image.get("imageSize"))),
        }
        result["output.modalities"] = (
            "text_image" if "TEXT" in config["responseModalities"] else "image"
        ) if "responseModalities" in config else None
        safety = body.get("safetySettings", body.get("safety_settings", []))
        tools = body.get("tools", [])
        if isinstance(safety, str):
            safety = json.loads(safety)
        if isinstance(tools, str):
            tools = json.loads(tools)
        result["gemini.safety_settings"] = {item["category"]: item["threshold"] for item in safety}
        search_types = next((tool["google_search"].get("searchTypes", {}) for tool in tools if "google_search" in tool), {})
        result["gemini.google_search"] = "webSearch" in search_types
        result["gemini.google_image_search"] = "imageSearch" in search_types
    result["output.count"] = count
    return result


def generation_summary(
    plan: ExecutionPlan, *, params: dict[str, Any], image_sources: list[dict[str, Any]],
    file_sources: list[dict[str, Any]], explicit_image_order: bool,
    reference_rules: dict[str, Any], auto_start_queue: bool,
) -> dict[str, Any]:
    count = int(plan.command.parameters.get("output.count") or 1)
    uses_per_output_requests = plan.binding.parameter_codec.startswith("gpt_")
    request_count = count if uses_per_output_requests else 1
    provider_limit = max(1, min(MAX_API_IMAGES_CONCURRENCY, plan.provider.concurrency))
    direct_concurrent = plan.provider.id != "codex" or plan.binding.protocol_profile.endswith("images")
    output_limit = min(request_count, int(params.get("api_images_concurrency") or DEFAULT_API_IMAGES_CONCURRENCY)) if direct_concurrent else 1
    effective = _effective_parameters(plan)
    constraints = plan.model.input_constraints
    warnings = []
    for parameter_id, value in {**plan.command.parameters, **plan.command.legacy_compat_parameters}.items():
        if value not in (None, False, {}, "") and effective.get(parameter_id) != value:
            warnings.append({
                "code": "parameter_not_applied", "parameter_id": parameter_id,
                "message": f"The selected protocol does not apply the requested {parameter_id} value; see effective parameters.",
            })
    if plan.provider.id != "codex" and not plan.provider.api_key:
        warnings.append({"code": "provider_credentials_missing", "message": "Provider credentials are not configured; execution will be blocked."})
    if plan.binding.transparency_mode == "prompt" and effective.get("gpt.background") == "transparent":
        warnings.append({"code": "transparency_prompt_guidance", "message": "This provider uses prompt guidance for transparency; the result is not guaranteed."})
    return {
        "schema_version": 1,
        "operation": plan.command.operation,
        "model": {"id": plan.model.id, "name": plan.model.display_name, "family_id": plan.model.family_id},
        "provider": {"id": plan.provider.id, "name": plan.provider.name},
        "binding": {"id": plan.binding.id, "remote_model_id": plan.binding.remote_model_id, "protocol_profile": plan.binding.protocol_profile},
        "parameters": dict(plan.command.parameters),
        "effective_parameters": effective,
        "dimensions": {"size": effective.get("canvas.size"), "aspect_ratio": effective.get("canvas.aspect_ratio"), "resolution": effective.get("canvas.resolution")},
        "quality": effective.get("gpt.quality"),
        "main_model": plan.command.main_model if plan.binding.protocol_profile.endswith("responses") else None,
        "request_options": {"main_model": plan.command.main_model if plan.binding.protocol_profile.endswith("responses") else None,
                            "prompt_fidelity": params.get("prompt_fidelity", "off")},
        "output_count": count,
        "task_count": 1,
        "request_count": request_count,
        "queue": {
            "provider_concurrency": provider_limit,
            "task_request_concurrency": output_limit,
            "max_provider_concurrency": MAX_API_IMAGES_CONCURRENCY,
            "auto_start": bool(auto_start_queue),
            "request_count_excludes_retries": True,
        },
        "references": {
            "images": image_sources, "files": file_sources,
            "image_count": len(plan.command.image_inputs), "file_count": len(plan.command.reference_files),
            "mask_present": bool(plan.command.mask_image),
            "ordering": "explicit_first_occurrence" if explicit_image_order else "uploads_assets_gallery",
            "file_ordering": "uploads_then_saved_first_occurrence",
            "rules": reference_rules,
            "limits": {"max_images": constraints.max_images, "supports_mask": constraints.supports_mask,
                       "supports_reference_files": constraints.supports_reference_files and plan.binding.protocol_profile.endswith("responses"),
                       "max_task_image_bytes": MAX_TASK_IMAGE_BYTES},
        },
        "warnings": warnings,
    }
