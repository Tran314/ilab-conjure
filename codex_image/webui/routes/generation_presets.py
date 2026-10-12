from __future__ import annotations

from typing import Any, TYPE_CHECKING

from fastapi import Body, FastAPI, HTTPException, Query

from codex_image.webui.generation_presets import (
    GenerationPresetError,
    generation_preset_compatibility,
)
from .generation_catalog import generation_catalog_payload

if TYPE_CHECKING:
    from codex_image.webui.context import WebUIContext


def register_generation_preset_routes(app: FastAPI, ctx: WebUIContext) -> None:
    store = ctx.generation_preset_settings

    def public_preset(preset: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
        result = generation_preset_compatibility(preset, catalog)
        return {**preset, "availability": {"available": result["compatible"], "issues": result["issues"]}}

    def public_list(catalog: dict[str, Any]) -> list[dict[str, Any]]:
        return [public_preset(preset, catalog) for preset in store.list()]

    def check(preset: dict[str, Any], catalog: dict[str, Any], *, inputs: dict[str, Any] | None = None, require_available: bool = False) -> None:
        result = generation_preset_compatibility(preset, catalog, inputs=inputs, require_available=require_available)
        if not result["compatible"]:
            issue = result["issues"][0]
            raise GenerationPresetError(issue["code"], issue["message"], 422 if issue["code"] == "generation_preset_invalid" else 409)

    @app.get("/api/generation-presets")
    def list_presets() -> dict[str, Any]:
        try:
            return {"version": 1, "presets": public_list(generation_catalog_payload(ctx))}
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc

    @app.post("/api/generation-presets", status_code=201)
    def create_preset(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            catalog = generation_catalog_payload(ctx)
            preset = store.create(payload, validator=lambda item: check(item, catalog))
            return {"preset": public_preset(preset, catalog), "presets": public_list(catalog)}
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc

    @app.patch("/api/generation-presets/{preset_id}")
    def update_preset(preset_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            catalog = generation_catalog_payload(ctx)
            preset = store.update(preset_id, payload, validator=lambda item: check(item, catalog))
            return {"preset": public_preset(preset, catalog), "presets": public_list(catalog)}
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc

    @app.delete("/api/generation-presets/{preset_id}")
    def delete_preset(preset_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
        try:
            store.delete(preset_id, confirm=confirm)
            return {"presets": public_list(generation_catalog_payload(ctx))}
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc

    @app.post("/api/generation-presets/{preset_id}/compatibility")
    def preset_compatibility(preset_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return generation_preset_compatibility(store.get(preset_id), generation_catalog_payload(ctx), inputs=payload)
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc

    @app.post("/api/generation-presets/{preset_id}/apply")
    def apply_preset(preset_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            # Keep this operation read-only: a successful response is a validated
            # composer settings snapshot, never a generation or input mutation.
            catalog = generation_catalog_payload(ctx)
            with store.exclusive():
                preset = store.get(preset_id)
                check(preset, catalog, inputs=payload, require_available=True)
                return {"preset": public_preset(preset, catalog)}
        except GenerationPresetError as exc:
            raise HTTPException(exc.status_code, detail=exc.detail()) from exc


__all__ = ("register_generation_preset_routes",)
