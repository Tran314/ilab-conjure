#!/usr/bin/env python3
"""Start/reopen/stop a disposable profile through the packaged native launcher."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


PORT = 8787


def program_snapshot(bundle: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(bundle)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in bundle.rglob("*") if path.is_file()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    metadata = json.loads((bundle / "bundle-metadata.json").read_text())
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit("Smoke-test port is occupied; refusing to touch an existing service")
    before = program_snapshot(bundle)
    with tempfile.TemporaryDirectory(prefix="ilab-linux-service-") as tmp:
        root = Path(tmp)
        env = dict(os.environ)
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "ILAB_CONJURE_DATA_DIR", "PYTHONHOME"):
            env.pop(key, None)
        env.update({"ILAB_CONJURE_APP_DIR": str(bundle / "app"), "XDG_DATA_HOME": str(root / "xdg")})
        if metadata["mode"] == "installed":
            data = root / "xdg/iLab GPT CONJURE"
        else:
            data = root / "portable-data"
            env["ILAB_CONJURE_DATA_DIR"] = str(data)
        data.mkdir(parents=True)
        custom_input, custom_output = root / "custom-input", root / "custom-output"
        (data / "webui-settings.json").write_text(json.dumps({"input_root": str(custom_input), "output_root": str(custom_output)}))
        binary = str(bundle / "ilab-conjure-launcher")

        def launch(argument: str) -> str:
            result = subprocess.run([binary, argument], env=env, text=True, capture_output=True, timeout=90)
            if result.returncode:
                raise RuntimeError(f"{argument} failed: {result.stdout}\n{result.stderr}")
            return result.stdout

        def get(path: str) -> dict:
            with urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as response:
                return json.load(response)

        try:
            launch("--no-tray")
            health = get("/api/health")
            if health.get("ok") is not True or not health.get("launcher_instance"):
                raise RuntimeError("Packaged server returned invalid health identity")
            if "launcher-owned" not in launch("--status"):
                raise RuntimeError("Packaged service ownership was not established")
            launch("--no-tray")
            if get("/api/health").get("launcher_instance") != health["launcher_instance"]:
                raise RuntimeError("Second launch started a duplicate backend")
            version = get("/api/app-version")
            if version.get("package") != metadata["package"] or version.get("updater_available"):
                raise RuntimeError("Wrong package identity or automatic Linux updater exposed")
            if not custom_input.is_dir() or not custom_output.is_dir():
                raise RuntimeError("Custom storage paths were not retained")
        finally:
            launch("--stop")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", PORT)) != 0:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("Packaged backend remained alive after --stop")
    if program_snapshot(bundle) != before:
        raise RuntimeError("Launcher/backend mutated the program bundle")
    print(f"{metadata['package']}: native start/reopen/status/stop, custom paths, identity and immutable program tree passed")
    print("Real GNOME tray, browser and X11/Wayland qualification remains pending.")


if __name__ == "__main__":
    main()
