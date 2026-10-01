from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlencode

from codex_image.client_types import OPENAI_COMPATIBLE_USER_AGENT
from codex_image.http import HTTPResponseTooLarge
from codex_image.webui.context import WebUIContext
from codex_image.webui.provider_validation import normalize_v2_base_url, provider_url_origin

MODEL_DISCOVERY_TIMEOUT_SECONDS = 10.0
MAX_MODEL_DISCOVERY_BYTES = 2 * 1024 * 1024
MAX_DISCOVERED_MODELS = 5000
MAX_MODEL_DISCOVERY_PAGES = 20


class ModelDiscoveryError(ValueError):
    def __init__(self, code: str, *, status_code: int = 502) -> None:
        super().__init__(code)
        self.status_code = status_code


def _discovery_connection(
    payload: Mapping[str, Any], settings: Mapping[str, Any]
) -> tuple[str, str, bool]:
    base_url = normalize_v2_base_url(payload.get("base_url"))
    # Match the settings editor's endpoint cleanup, without adding a version path.
    for suffix in ("/responses", "/images/generations", "/images/edits"):
        if base_url.endswith(suffix):
            base_url = base_url.removesuffix(suffix).rstrip("/")
            break
    protocol = payload.get("protocol")
    if not isinstance(protocol, str) or protocol not in {"gemini", "openai_images", "openai_responses"}:
        raise ModelDiscoveryError("model_discovery_protocol_unsupported", status_code=400)
    if payload.get("api_key") is not None and not isinstance(payload["api_key"], str):
        raise ModelDiscoveryError("model_discovery_key_required", status_code=400)
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        source_id = payload.get("api_key_source_provider_id") or payload.get("provider_id")
        source = next(
            (provider for provider in settings.get("providers", []) if provider["id"] == source_id),
            None,
        )
        if source is not None:
            if provider_url_origin(source["base_url"]) != provider_url_origin(base_url):
                raise ModelDiscoveryError("api_key_origin_mismatch", status_code=400)
            api_key = str(source.get("api_key") or "").strip()
    if not api_key:
        raise ModelDiscoveryError("model_discovery_key_required", status_code=400)
    if any(ord(char) < 32 or ord(char) == 127 for char in api_key):
        raise ModelDiscoveryError("model_discovery_key_required", status_code=400)
    return base_url, api_key, protocol == "gemini"


def discover_provider_models(ctx: WebUIContext, payload: Mapping[str, Any]) -> dict[str, Any]:
    base_url, api_key, gemini = _discovery_connection(payload, ctx.api_settings.read())
    endpoint = f"{base_url}/models"
    headers = {"Accept": "application/json", "User-Agent": OPENAI_COMPATIBLE_USER_AGENT}
    headers["x-goog-api-key" if gemini else "Authorization"] = api_key if gemini else f"Bearer {api_key}"
    transport = ctx.network_egress_manager.transport(
        ctx.network_egress_manager.snapshot(), timeout_seconds=MODEL_DISCOVERY_TIMEOUT_SECONDS
    )
    models: dict[str, dict[str, str]] = {}
    page_token = ""
    seen_tokens: set[str] = set()
    for _page in range(MAX_MODEL_DISCOVERY_PAGES):
        query = urlencode({"pageToken": page_token}) if page_token else ""
        try:
            response = transport.request_bounded(
                method="GET", url=f"{endpoint}?{query}" if query else endpoint,
                headers=headers, body=b"", max_response_bytes=MAX_MODEL_DISCOVERY_BYTES,
            )
        except HTTPResponseTooLarge as exc:
            raise ModelDiscoveryError("model_discovery_too_large") from exc
        except Exception as exc:
            # Upstream errors can echo credentials or proxy URLs; return only a safe code.
            raise ModelDiscoveryError("model_discovery_network_error") from exc
        if response.status in {401, 403}:
            raise ModelDiscoveryError("model_discovery_unauthorized")
        if response.status in {404, 405}:
            raise ModelDiscoveryError("model_discovery_not_supported")
        if response.status == 429:
            raise ModelDiscoveryError("model_discovery_rate_limited")
        if not 200 <= response.status < 300:
            raise ModelDiscoveryError("model_discovery_upstream_error")
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelDiscoveryError("model_discovery_invalid_response") from exc
        entries = data.get("models" if gemini else "data") if isinstance(data, dict) else None
        # Gemini can omit an empty repeated field.
        if gemini and isinstance(data, dict) and not data:
            entries = []
        if not isinstance(entries, list):
            raise ModelDiscoveryError("model_discovery_invalid_response")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("name" if gemini else "id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip().removeprefix("models/") if gemini else model_id.strip()
            if not model_id or len(model_id) > 512 or any(ord(char) < 32 for char in model_id):
                continue
            # Never return the full provider response or infer image compatibility from names.
            models[model_id] = {"id": model_id}
            if len(models) > MAX_DISCOVERED_MODELS:
                raise ModelDiscoveryError("model_discovery_too_large")
        next_token = data.get("nextPageToken", "") if gemini else ""
        if not next_token:
            return {"models": sorted(models.values(), key=lambda model: model["id"].casefold())}
        if not isinstance(next_token, str) or len(next_token) > 4096 or next_token in seen_tokens:
            raise ModelDiscoveryError("model_discovery_invalid_response")
        seen_tokens.add(next_token)
        page_token = next_token
    raise ModelDiscoveryError("model_discovery_too_large")
