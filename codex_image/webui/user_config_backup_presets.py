"""Portable preset identities and non-destructive incremental restore."""
from __future__ import annotations

from copy import deepcopy
import json
import unicodedata
from typing import Any
from uuid import uuid4

from .generation_presets import (
    MAX_GENERATION_PRESETS, MAX_GENERATION_PRESET_NAME_LENGTH,
    validate_generation_presets_payload,
)


GENERATION_PRESETS_MEMBER = "settings/generation-presets.json"


def preset_name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


def preset_identity(preset: dict[str, Any]) -> str:
    # IDs and timestamps describe storage history, not the named configuration.
    content = {key: value for key, value in preset.items()
               if key not in {"id", "created_at", "updated_at"}}
    content["name"] = preset_name_key(content["name"])
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def merge_generation_presets(
    imported: dict[str, Any], current: dict[str, Any],
) -> tuple[dict[str, Any], int, int, int]:
    """Return a strictly valid candidate and added, skipped, recovery-copy counts."""
    merged, added, skipped, copies = plan_generation_presets_merge(imported, current)
    if len(merged["presets"]) > MAX_GENERATION_PRESETS:
        raise ValueError("user_config_restore_generation_presets_merge_limit")
    return validate_generation_presets_payload(merged), added, skipped, copies


def plan_generation_presets_merge(
    imported: dict[str, Any], current: dict[str, Any],
) -> tuple[dict[str, Any], int, int, int]:
    """Preview identical rename decisions, even when the merged store exceeds its limit."""
    incoming = validate_generation_presets_payload(imported)
    merged = deepcopy(validate_generation_presets_payload(current))
    records = merged["presets"]
    identities = {preset_identity(item) for item in records}
    ids = {item["id"] for item in records}
    names = {preset_name_key(item["name"]) for item in records}
    added = skipped = copies = 0
    for raw in incoming["presets"]:
        identity = preset_identity(raw)
        if identity in identities:
            skipped += 1
            continue
        item = deepcopy(raw)
        if item["id"] in ids or preset_name_key(item["name"]) in names:
            item["id"] = str(uuid4())
            while item["id"] in ids:
                item["id"] = str(uuid4())
            base = item["name"]
            index = 1
            while True:
                suffix = " (restored)" if index == 1 else f" (restored {index})"
                candidate = base[:MAX_GENERATION_PRESET_NAME_LENGTH - len(suffix)].rstrip() + suffix
                if preset_name_key(candidate) not in names:
                    item["name"] = candidate
                    break
                index += 1
            copies += 1
        records.append(item)
        identities.add(identity)
        ids.add(item["id"])
        names.add(preset_name_key(item["name"]))
        added += 1
    return merged, added, skipped, copies
