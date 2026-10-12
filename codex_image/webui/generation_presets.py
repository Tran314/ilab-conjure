"""Versioned, input-free generation presets and local compatibility checks.

Persistence deliberately does not depend on the current provider configuration:
removed providers must not make a saved preset impossible to rename or delete.
Applying a preset is a separate, fail-closed catalog/resolver operation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import threading
from typing import Any
import unicodedata
import uuid

from codex_image.client_types import ResponsesInputFile
from codex_image.generation.catalog import list_model_manifests
from codex_image.generation.resolver import BindingResolver
from codex_image.generation.types import GenerationCommand, ImageInput
from codex_image.providers.contracts import ProviderConnection, ProviderModelBinding
from codex_image.providers.registry import default_registry

from .atomic_files import atomic_write_text
from .storage_utils import utc_now
from .store_locks import StoreLockMixin, store_locked

GENERATION_PRESET_VERSION = 1
MAX_GENERATION_PRESETS = 200
MAX_GENERATION_PRESET_NAME_LENGTH = 120
MAX_GENERATION_PRESETS_BYTES = 4 * 1024 * 1024
_MUTABLE_FIELDS = frozenset({
    "name", "operation", "canonical_model_id", "provider_id", "binding_id",
    "parameters", "reference_rules", "request_options",
})
_REQUIRED_FIELDS = (_MUTABLE_FIELDS - {"request_options"}) | {"id", "created_at", "updated_at"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAIN_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*\Z")
_SIZE = re.compile(r"(?:auto|[1-9][0-9]{0,4}x[1-9][0-9]{0,4})\Z")
_PLACEHOLDER_IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1sAAAAASUVORK5CYII="


class GenerationPresetError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class GenerationPresetStoreError(GenerationPresetError):
    def __init__(self, code: str = "generation_preset_store_invalid") -> None:
        super().__init__(code, "The generation preset file cannot be read safely. Repair or restore it before making changes.", 503)


def _invalid(message: str) -> GenerationPresetError:
    return GenerationPresetError("generation_preset_invalid", message)


def _object(value: Any, allowed: frozenset[str] | set[str], *, required: frozenset[str] | set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise _invalid("Generation preset data has missing or unsupported fields.")
    return value


def normalize_generation_preset_name(value: Any) -> str:
    if not isinstance(value, str):
        raise _invalid("A generation preset name is required.")
    name = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(name) <= MAX_GENERATION_PRESET_NAME_LENGTH or any(
        unicodedata.category(character).startswith("C") for character in name
    ):
        raise _invalid("Generation preset names must contain 1 to 120 characters without control characters.")
    return name


def normalize_reference_rules(value: Any) -> dict[str, str]:
    if value is None:
        value = {}
    value = _object(value, {"images", "files", "order"})
    rules = {"images": value.get("images", "preserve"), "files": value.get("files", "preserve"), "order": value.get("order", "preserve")}
    if any(not isinstance(rules[key], str) or rules[key] not in {"preserve", "require", "forbid"} for key in ("images", "files")) or rules["order"] != "preserve":
        raise _invalid("Reference rules must preserve input order and use preserve, require, or forbid constraints.")
    return rules


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _invalid("Generation preset model, provider, and binding identifiers are invalid.")
    return value


def _parameters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid("Generation preset parameters must be an object.")
    # Use the union of known canonical definitions for storage. A removed model
    # can remain manageable, but cannot introduce arbitrary persisted fields.
    definitions: dict[str, list[Any]] = {}
    for model in list_model_manifests():
        for definition in model.parameters:
            definitions.setdefault(definition.id, []).append(definition)
    for key, item in value.items():
        choices = definitions.get(key) if isinstance(key, str) else None
        if choices is None:
            raise _invalid("Generation preset parameters contain an unsupported field.")
        valid = False
        for definition in choices:
            try:
                BindingResolver._validate_parameter_value(definition, item)
            except (ValueError, TypeError, OverflowError):
                continue
            if definition.value_type == "object":
                rows = {row.key for row in definition.object_choices}
                if not isinstance(item, dict) or set(item) - rows:
                    continue
            if definition.value_type == "string" and not definition.allowed_values:
                # The only free-form canonical string is a size, never a path,
                # prompt, URL, inline image, or credential-bearing arbitrary text.
                if key != "canvas.size" or not _SIZE.fullmatch(item):
                    continue
            valid = True
            break
        if not valid:
            raise _invalid(f"Invalid canonical preset parameter: {key}.")
    return deepcopy(value)


def _request_options(value: Any) -> dict[str, str]:
    value = _object(value, {"main_model", "prompt_fidelity"})
    if "main_model" in value:
        model_id = value["main_model"]
        if not isinstance(model_id, str) or len(model_id) > 128 or not _MAIN_MODEL.fullmatch(model_id):
            raise _invalid("The main model must be a model identifier or a namespaced model identifier, not a path or URL.")
        if any(part.casefold().startswith(("sk-", "sk_")) for part in model_id.split("/")):
            raise _invalid("The main model must be a model identifier, not a credential.")
    if "prompt_fidelity" in value and value["prompt_fidelity"] not in ("strict", "original", "off"):
        raise _invalid("Prompt fidelity must be strict, original, or off.")
    return dict(value)


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise _invalid("Generation preset timestamps must be timezone-aware ISO timestamps.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _invalid("Generation preset timestamps must be timezone-aware ISO timestamps.") from exc
    if parsed.tzinfo is None:
        raise _invalid("Generation preset timestamps must be timezone-aware ISO timestamps.")
    return value


def normalize_generation_preset_payload(value: Any) -> dict[str, Any]:
    value = _object(value, _REQUIRED_FIELDS | {"request_options"}, required=_REQUIRED_FIELDS)
    if not isinstance(value["id"], str):
        raise _invalid("Generation preset IDs must be UUIDs.")
    try:
        preset_id = str(uuid.UUID(value["id"]))
    except ValueError as exc:
        raise _invalid("Generation preset IDs must be UUIDs.") from exc
    if value["operation"] not in ("generate", "edit"):
        raise _invalid("Generation preset operation must be generate or edit.")
    return {
        "id": preset_id,
        "name": normalize_generation_preset_name(value["name"]),
        "operation": value["operation"],
        "canonical_model_id": _identifier(value["canonical_model_id"]),
        "provider_id": _identifier(value["provider_id"]),
        "binding_id": _identifier(value["binding_id"]),
        "parameters": _parameters(value["parameters"]),
        "reference_rules": normalize_reference_rules(_object(value["reference_rules"], {"images", "files", "order"})),
        "request_options": _request_options(value.get("request_options", {})),
        "created_at": _timestamp(value["created_at"]),
        "updated_at": _timestamp(value["updated_at"]),
    }


def validate_generation_presets_payload(payload: Any) -> dict[str, Any]:
    payload = _object(payload, {"version", "presets"}, required={"version", "presets"})
    if type(payload["version"]) is not int or payload["version"] != GENERATION_PRESET_VERSION:
        raise GenerationPresetError("generation_preset_version_unsupported", "This generation preset file uses an unsupported version.")
    if not isinstance(payload["presets"], list) or len(payload["presets"]) > MAX_GENERATION_PRESETS:
        raise _invalid("Generation presets must be an array of at most 200 entries.")
    presets = [normalize_generation_preset_payload(item) for item in payload["presets"]]
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for preset in presets:
        if preset["id"] in seen_ids or preset["name"].casefold() in seen_names:
            raise GenerationPresetError("generation_preset_name_conflict", "A generation preset with this name or ID already exists.", 409)
        seen_ids.add(preset["id"])
        seen_names.add(preset["name"].casefold())
    return {"version": GENERATION_PRESET_VERSION, "presets": presets}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class GenerationPresetSettings(StoreLockMixin):
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def default_settings() -> dict[str, Any]:
        return {"version": GENERATION_PRESET_VERSION, "presets": []}

    @store_locked
    def read(self) -> dict[str, Any]:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_GENERATION_PRESETS_BYTES + 1)
        except FileNotFoundError:
            return self.default_settings()
        except OSError as exc:
            raise GenerationPresetStoreError() from exc
        try:
            if len(raw) > MAX_GENERATION_PRESETS_BYTES:
                raise ValueError("Preset file too large")
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
            return validate_generation_presets_payload(payload)
        except GenerationPresetError as exc:
            code = exc.code if exc.code == "generation_preset_version_unsupported" else "generation_preset_store_invalid"
            raise GenerationPresetStoreError(code) from exc
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise GenerationPresetStoreError() from exc

    @store_locked
    def list(self) -> list[dict[str, Any]]:
        return self.read()["presets"]

    @store_locked
    def get(self, preset_id: str) -> dict[str, Any]:
        for preset in self.list():
            if preset["id"] == preset_id:
                return preset
        raise GenerationPresetError("generation_preset_not_found", "The generation preset no longer exists.", 404)

    @store_locked
    def create(self, payload: dict[str, Any], *, validator: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        payload = _object(payload, _MUTABLE_FIELDS, required=_MUTABLE_FIELDS - {"reference_rules", "request_options"})
        current = self.read()
        if len(current["presets"]) >= MAX_GENERATION_PRESETS:
            raise _invalid("At most 200 generation presets can be saved.")
        now = utc_now()
        preset = normalize_generation_preset_payload({
            **payload, "reference_rules": normalize_reference_rules(payload.get("reference_rules")),
            "id": str(uuid.uuid4()), "created_at": now, "updated_at": now,
        })
        candidate = validate_generation_presets_payload({**current, "presets": [*current["presets"], preset]})
        if validator:
            validator(preset)
        self.write(candidate)
        return preset

    @store_locked
    def update(self, preset_id: str, payload: dict[str, Any], *, validator: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        payload = _object(payload, _MUTABLE_FIELDS)
        if not payload:
            raise _invalid("Supply at least one generation preset field to update.")
        current = self.read()
        existing = self.get(preset_id)
        preset = normalize_generation_preset_payload({**existing, **payload, "updated_at": utc_now()})
        candidate = validate_generation_presets_payload({**current, "presets": [preset if item["id"] == preset_id else item for item in current["presets"]]})
        # Renaming an unavailable record is always permitted.
        if validator and set(payload) != {"name"}:
            validator(preset)
        self.write(candidate)
        return preset

    @store_locked
    def delete(self, preset_id: str, *, confirm: bool = False) -> None:
        if confirm is not True:
            raise GenerationPresetError("generation_preset_delete_confirmation_required", "Confirm before deleting this generation preset.", 409)
        current = self.read()
        self.get(preset_id)
        self.write({**current, "presets": [item for item in current["presets"] if item["id"] != preset_id]})

    @store_locked
    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.read()  # Never overwrite a corrupt or future-version store.
        normalized = validate_generation_presets_payload(payload)
        try:
            atomic_write_text(self.path, json.dumps(normalized, indent=2, ensure_ascii=False, allow_nan=False), mode=0o600)
        except OSError as exc:
            raise GenerationPresetError("generation_preset_write_failed", "The generation preset file could not be saved. Existing presets were not replaced.", 503) from exc
        return normalized

    @store_locked
    def backup_snapshot(self) -> dict[str, Any]:
        return self.read()

    @store_locked
    def replace_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.write(payload)


def validate_preset_input_rules(preset: Mapping[str, Any], *, image_count: int, reference_file_count: int, has_mask: bool) -> None:
    if any(type(count) is not int or not 0 <= count <= 1000 for count in (image_count, reference_file_count)) or not isinstance(has_mask, bool):
        raise _invalid("Reference input counts and mask presence are invalid.")
    rules = normalize_reference_rules(preset.get("reference_rules"))
    for key, count in (("images", image_count), ("files", reference_file_count)):
        if rules[key] == "require" and count == 0:
            raise GenerationPresetError(f"generation_preset_reference_{key}_required", f"This preset requires reference {key}.", 409)
        if rules[key] == "forbid" and count:
            raise GenerationPresetError(f"generation_preset_reference_{key}_forbidden", f"This preset does not allow reference {key}; current inputs have been preserved.", 409)
    if preset.get("operation") == "edit" and not image_count:
        raise GenerationPresetError("generation_preset_edit_image_required", "Editing requires at least one reference image.", 409)
    if has_mask and not image_count:
        raise GenerationPresetError("generation_preset_mask_image_required", "A mask requires a reference image.", 409)
    model = next((item for item in list_model_manifests() if item.id == preset.get("canonical_model_id")), None)
    if model is None:
        raise GenerationPresetError("model_not_found", "The preset's image model is unavailable.", 409)
    constraints = model.input_constraints
    if image_count > constraints.max_images:
        raise GenerationPresetError("image_input_limit_exceeded", f"This model accepts at most {constraints.max_images} reference images.", 409)
    if has_mask and not constraints.supports_mask:
        raise GenerationPresetError("mask_input_unsupported", "This model does not support a mask.", 409)
    if reference_file_count and not constraints.supports_reference_files:
        raise GenerationPresetError("reference_files_unsupported", "This model does not support reference files.", 409)


def generation_preset_compatibility(preset: dict[str, Any], catalog: Mapping[str, Any], *, inputs: dict[str, Any] | None = None, require_available: bool = True) -> dict[str, Any]:
    """Check a preset locally. Never dispatch transport or include private input data."""
    try:
        preset = normalize_generation_preset_payload(preset)
        models = {model.id: model for model in list_model_manifests()}
        if preset["canonical_model_id"] not in models:
            raise GenerationPresetError("model_not_found", "The preset's image model is unavailable.", 409)
        provider = next((item for item in catalog.get("providers", []) if item.get("id") == preset["provider_id"]), None)
        if provider is None:
            raise GenerationPresetError("provider_not_found", "The preset's provider is no longer configured.", 409)
        binding = next((item for item in provider.get("bindings", []) if item.get("id") == preset["binding_id"]), None)
        if binding is None:
            raise GenerationPresetError("binding_not_found", "The preset's provider binding is no longer configured.", 409)
        if require_available and not provider.get("available", False):
            raise GenerationPresetError("provider_unavailable", "The preset's provider needs authentication or a supported runtime.", 409)
        if binding.get("available") is False:
            raise GenerationPresetError("binding_unavailable", "The preset's binding runtime is unavailable.", 409)
        options = preset["request_options"]
        if options and models[preset["canonical_model_id"]].family_id != "gpt-image":
            raise GenerationPresetError("generation_preset_options_inapplicable", "GPT request options cannot be applied to this model.", 409)
        if "main_model" in options and binding["protocol_profile"] not in {"codex_responses", "openai_responses"}:
            raise GenerationPresetError("generation_preset_options_inapplicable", "The main model option requires a Responses binding.", 409)
        if inputs is None:
            rules = preset["reference_rules"]
            inputs = {"image_count": int(preset["operation"] == "edit" or rules["images"] == "require"), "reference_file_count": int(rules["files"] == "require"), "has_mask": False}
        inputs = _object(inputs, {"image_count", "reference_file_count", "has_mask"})
        counts = {"image_count": inputs.get("image_count", 0), "reference_file_count": inputs.get("reference_file_count", 0), "has_mask": inputs.get("has_mask", False)}
        validate_preset_input_rules(preset, **counts)
        # The Images transports cannot encode reference files, even for a model
        # whose Responses transport supports them. Never silently discard files.
        if counts["reference_file_count"] and binding["protocol_profile"] not in {"codex_responses", "openai_responses"}:
            raise GenerationPresetError("reference_files_unsupported", "This provider binding does not support reference files.", 409)
        connection = ProviderConnection(
            id=provider["id"], name=provider.get("name", provider["id"]), base_url="", api_key="", concurrency=1,
            builtin=bool(provider.get("builtin")),
            bindings=(ProviderModelBinding(
                id=binding["id"], provider_id=provider["id"], canonical_model_id=binding["canonical_model_id"],
                remote_model_id=binding["remote_model_id"], protocol_profile=binding["protocol_profile"],
                parameter_codec=binding["parameter_codec"], operations=frozenset(binding["operations"]),
                append_aspect_ratio_prompt=bool(binding.get("append_aspect_ratio_prompt", False)),
                transparency_mode=binding.get("transparency_mode", "native"),
            ),),
        )
        command = GenerationCommand(
            operation=preset["operation"], canonical_model_id=preset["canonical_model_id"], provider_id=preset["provider_id"],
            binding_id=preset["binding_id"], prompt="Preset compatibility check", parameters=preset["parameters"],
            image_inputs=tuple(ImageInput(_PLACEHOLDER_IMAGE) for _ in range(counts["image_count"])),
            reference_files=tuple(ResponsesInputFile("reference.txt", "text/plain", "data:text/plain;base64,eA==") for _ in range(counts["reference_file_count"])),
            mask_image=_PLACEHOLDER_IMAGE if counts["has_mask"] else None,
            main_model=options.get("main_model"),
        )
        BindingResolver(models=models, providers={connection.id: connection}, registry=default_registry()).resolve(command)
    except GenerationPresetError as exc:
        return {"compatible": False, "issues": [exc.detail()]}
    except (ValueError, KeyError, TypeError) as exc:
        # Resolver messages only contain validated canonical identifiers/values.
        # Never return provider records, secrets, or the generated request body.
        message = str(exc)
        if "does not support model" in message or "does not support binding" in message:
            issue = {"code": "provider_model_binding_missing", "message": "The preset's binding does not support this model and operation."}
        elif "does not support operation" in message:
            issue = {"code": "operation_unsupported", "message": "The preset's operation is unsupported by this model or binding."}
        elif "transparent_background_requires_png_or_webp" in message:
            issue = {"code": "transparent_background_requires_png_or_webp", "message": "Transparent backgrounds require PNG or WebP output."}
        else:
            issue = {"code": "generation_preset_incompatible", "message": "The preset's parameters are incompatible with the selected model or binding."}
        return {"compatible": False, "issues": [issue]}
    return {"compatible": True, "issues": []}


__all__ = (
    "GENERATION_PRESET_VERSION", "MAX_GENERATION_PRESETS", "MAX_GENERATION_PRESET_NAME_LENGTH",
    "GenerationPresetError", "GenerationPresetSettings", "GenerationPresetStoreError",
    "generation_preset_compatibility", "normalize_generation_preset_name", "normalize_generation_preset_payload",
    "normalize_reference_rules", "validate_generation_presets_payload", "validate_preset_input_rules",
)
