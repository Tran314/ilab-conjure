"""Shared portable/installed Linux entry point; program files stay read-only."""
from __future__ import annotations

import os
from pathlib import Path

from codex_image.webui.app import create_app
from codex_image.webui.standard_storage import load_standard_storage_paths


APP_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = APP_DIR.parent


def default_data_dir() -> Path:
    if os.environ.get("APP_LAUNCHER_MODE") != "standard":
        return BUNDLE_DIR / "data"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return base / "iLab GPT CONJURE"


def build_app():
    data_dir = Path(os.environ.get("ILAB_CONJURE_DATA_DIR") or default_data_dir()).expanduser().resolve()
    # Use saved custom input/output/gallery/source paths in both Linux packages.
    # This also keeps a portable data copy's explicit external paths intact.
    paths = load_standard_storage_paths(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return create_app(
        **paths,
        reference_asset_root=paths["input_root"] / "reference-assets",
        webui_settings_path=data_dir / "webui-settings.json",
        auth_settings_path=data_dir / "webui-auth-settings.json",
        api_settings_path=data_dir / "webui-api-settings.json",
        network_egress_settings_path=data_dir / "webui-network-egress-settings.json",
        color_settings_path=data_dir / "webui-color-settings.json",
        prompt_snippets_path=data_dir / "webui-prompt-snippets.json",
        prompt_templates_path=data_dir / "webui-prompt-templates.json",
        enforce_single_instance=True,
    )


app = build_app()
