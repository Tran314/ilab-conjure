"""Read-only comparisons using the same duplicate identities as incremental restore."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from .generation_presets import MAX_GENERATION_PRESETS
from .user_config_backup_presets import (
    GENERATION_PRESETS_MEMBER, plan_generation_presets_merge,
)


def gallery_items(payloads: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(value["id"]): value
        for path, value in payloads.items()
        if path.startswith("gallery/items/") and path.endswith("/metadata.json")
    }


def gallery_reference_impact(
    imported: Mapping[str, dict[str, Any]],
    current: Mapping[str, dict[str, Any]],
    reference_counts: Mapping[str, int],
) -> int:
    """Count task-to-image references which replacement would invalidate."""
    return sum(
        count
        for item_id, count in reference_counts.items()
        if item_id in current
        and (
            item_id not in imported
            or imported[item_id].get("sha256") != current[item_id].get("sha256")
        )
    )


def _compare_records(imported, current, *, identity, collision) -> tuple[int, int]:
    identities = {identity(item) for item in current}
    collisions = {key for item in current for key in collision(item)}
    identical = conflicts = 0
    for item in imported:
        if identity(item) in identities:
            identical += 1
        elif any(key in collisions for key in collision(item)):
            conflicts += 1
        # Mirror incremental merges, including duplicate entries inside an archive.
        identities.add(identity(item))
        collisions.update(collision(item))
    return identical, conflicts


def compare_section(
    section: str,
    imported: dict[str, Any],
    current: dict[str, Any],
    imported_members,
    current_members,
    *,
    contains_secrets: bool,
    current_warnings=(),
) -> tuple[int, int, int, tuple[str, ...]]:
    identical = conflicts = missing = 0
    warnings: list[str] = []
    preset_conflicts = 0
    if section == "chips":
        archive_colors = imported.get("chips/colors.json", {})
        local_colors = current.get("chips/colors.json", {})
        for field in ("favorites", "recent_colors"):
            identity = (lambda item: item["hex"]) if field == "favorites" else (lambda item: item)
            duplicate, _ = _compare_records(
                archive_colors.get(field, []), local_colors.get(field, []),
                identity=identity, collision=lambda item: (),
            )
            identical += duplicate
        duplicate, conflicts = _compare_records(
            imported.get("chips/prompt-snippets.json", {}).get("snippets", []),
            current.get("chips/prompt-snippets.json", {}).get("snippets", []),
            identity=lambda item: (item["tag"].casefold(), item["content"]),
            collision=lambda item: (("id", item["id"]), ("tag", item["tag"].casefold())),
        )
        identical += duplicate
    elif section == "gallery":
        identical, conflicts = _compare_records(
            gallery_items(imported).values(), gallery_items(current).values(),
            identity=lambda item: item["sha256"],
            collision=lambda item: (("id", item["id"]), ("name", item["name"].casefold())),
        )
    elif section == "templates":
        archive_templates = imported.get("templates/prompt-templates.json", {}).get("templates", [])
        local_templates = current.get("templates/prompt-templates.json", {}).get("templates", [])
        identical, conflicts = _compare_records(
            archive_templates, local_templates,
            identity=lambda item: (item["title"].casefold(), item["content"]),
            collision=lambda item: (("id", item["id"]), ("title", item["title"].casefold())),
        )
        current_assets = {
            member.sha256 for member in current_members
            if member.path.startswith("templates/thumbnails/")
        }
        identical += sum(
            member.sha256 in current_assets for member in imported_members
            if member.path.startswith("templates/thumbnails/")
        )
        # A local URL without a bundled image is not a portable asset. Do not
        # silently report zero just because the ZIP itself is structurally valid.
        missing = sum(
            bool(item.get("thumbnail_url"))
            and urlsplit(str(item["thumbnail_url"])).scheme.lower() not in {"http", "https"}
            for item in archive_templates
        )
        missing += sum(warning.code == "template_thumbnail_missing" for warning in current_warnings)
    elif section == "settings":
        if GENERATION_PRESETS_MEMBER in imported:
            merged, _, duplicate, preset_conflicts = plan_generation_presets_merge(
                imported[GENERATION_PRESETS_MEMBER],
                current.get(GENERATION_PRESETS_MEMBER, {"version": 1, "presets": []}),
            )
            if len(merged["presets"]) > MAX_GENERATION_PRESETS:
                warnings.append("user_config_restore_generation_presets_merge_limit")
            identical += duplicate
            conflicts += preset_conflicts
        elif current.get(GENERATION_PRESETS_MEMBER, {}).get("presets"):
            warnings.append("user_config_restore_generation_presets_preserved")
        for path, payload in imported.items():
            if path == GENERATION_PRESETS_MEMBER:
                continue
            if not path.startswith("settings/") or path not in current:
                continue
            if path == "settings/client-preferences.json":
                # Browser-local preferences are not observable by this service.
                warnings.append("user_config_restore_client_preferences_unchecked")
                continue
            local = current[path]
            if path == "settings/providers.json" and not contains_secrets:
                payload = _without_api_keys(payload)
                local = _without_api_keys(local)
            if payload == local:
                identical += 1
            else:
                conflicts += 1
    if preset_conflicts:
        warnings.append("user_config_restore_conflicts_create_copies")
    if conflicts - preset_conflicts:
        warnings.append(
            "user_config_restore_settings_keep_existing"
            if section == "settings" else "user_config_restore_conflicts_create_copies"
        )
    if missing:
        warnings.append("user_config_restore_missing_assets_review")
    return identical, conflicts, missing, tuple(warnings)


def _without_api_keys(value):
    if isinstance(value, dict):
        return {key: _without_api_keys(item) for key, item in value.items() if key != "api_key"}
    if isinstance(value, list):
        return [_without_api_keys(item) for item in value]
    return value
