#!/usr/bin/env python3
"""Headless package checks; never a substitute for GNOME X11/Wayland testing."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


DLOPEN_CHECK = """
import ctypes
ctypes.CDLL('libgtk-3.so.0')
for name in ('libayatana-appindicator3.so.1', 'libappindicator3.so.1'):
    try:
        ctypes.CDLL(name)
        print('AppIndicator dlopen OK:', name)
        break
    except OSError:
        pass
else:
    raise SystemExit('Missing runtime AppIndicator library: install libayatana-appindicator3-1')
import ssl, sqlite3, fastapi, uvicorn, multipart, httpx, PIL
print('Bundled Python and GTK3 imports OK')
"""


def glibc_versions(text: str) -> set[tuple[int, ...]]:
    return {tuple(map(int, version.split("."))) for version in re.findall(r"GLIBC_([0-9]+(?:\.[0-9]+)+)", text)}


def verify_glibc_baseline(bundle: Path, maximum: str) -> None:
    limit = tuple(map(int, maximum.split(".")))
    for binary in bundle.rglob("*"):
        if not binary.is_file():
            continue
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        result = subprocess.run(["readelf", "--version-info", str(binary)], check=True, capture_output=True, text=True)
        too_new = sorted(version for version in glibc_versions(result.stdout) if version > limit)
        if too_new:
            raise SystemExit(f"{binary} requires GLIBC_{'.'.join(map(str, too_new[-1]))}; build on Ubuntu 22.04 (maximum {maximum})")
    print(f"All packaged ELF files satisfy GLIBC <= {maximum}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--max-glibc", help="Fail if any delivered ELF file requires newer glibc (release baseline: 2.35)")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    if args.max_glibc:
        verify_glibc_baseline(bundle, args.max_glibc)
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update({
        "PYTHONPATH": os.pathsep.join((str(bundle / "app"), str(bundle / "app/.deps"))),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    subprocess.run([str(bundle / "python/bin/python3"), "-c", DLOPEN_CHECK], env=env, check=True)
    for binary in (bundle / "ilab-conjure-launcher", bundle / "python/bin/python3"):
        result = subprocess.run(["ldd", str(binary)], check=True, capture_output=True, text=True)
        print(result.stdout)
        if "not found" in result.stdout:
            raise SystemExit(f"Missing linked library for {binary}")
    print("Headless runtime checks passed; real GNOME tray/browser/session checks remain pending.")


if __name__ == "__main__":
    main()
