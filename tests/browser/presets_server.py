"""Disposable browser-test server. Never constructs a provider client."""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import uvicorn

from codex_image.webui.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ilab-presets-browser-") as temporary:
        root = Path(temporary)
        provider_calls = []

        def no_provider_client():
            provider_calls.append("forbidden")
            raise AssertionError("Browser tests must never execute provider requests")

        settings = {name: root / (name + ".json") for name in (
            "auth_settings_path", "api_settings_path", "webui_settings_path",
            "network_egress_settings_path", "color_settings_path",
            "prompt_snippets_path", "prompt_templates_path",
        )}
        app = create_app(
            output_root=root / "outputs", input_root=root / "inputs",
            source_data_root=root / "source-data", client_factory=no_provider_client,
            auth_checker=lambda: True, auto_start_queue=False, **settings,
        )

        @app.get("/__qa__/state")
        def test_state():
            return {
                "instance_token": os.environ["ILAB_BROWSER_TEST_TOKEN"],
                "provider_calls": len(provider_calls),
                "task_ids": [task["task_id"] for task in app.state.storage.list_tasks()],
                "presets": app.state.generation_preset_settings.list(),
            }

        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
