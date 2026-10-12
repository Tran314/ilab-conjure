#!/usr/bin/env python3
"""Build Ubuntu x86_64 portable tar.gz first, then derive an installed DEB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUNDLE_NAME = "ilab-gpt-conjure_linux_portable_x64"
DEB_DEPENDS = (
    "libc6 (>= 2.35), libgcc-s1, libstdc++6, "
    "libgtk-3-0t64 | libgtk-3-0, "
    "libayatana-appindicator3-1 | libappindicator3-1, "
    "libxdo3, libxkbcommon0, libx11-6, libxrandr2, libxi6, libxcursor1, "
    "libwayland-client0, libwayland-cursor0, libwayland-egl1, xdg-utils, curl, ca-certificates, zenity, libglib2.0-bin"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_pin() -> dict[str, str]:
    pin = json.loads((HERE / "python-runtime.json").read_text(encoding="utf-8"))
    expected = f"cpython-{pin['python_version']}+{pin['release']}-{pin['triple']}-install_only_stripped.tar.gz"
    if pin["asset"] != expected or pin["triple"] != "x86_64-unknown-linux-gnu":
        raise ValueError("Invalid pinned PBS asset or target triple")
    if not re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]):
        raise ValueError("Invalid pinned PBS SHA256")
    expected_url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{pin['release']}/{expected.replace('+', '%2B')}"
    )
    if pin["url"] != expected_url:
        raise ValueError("PBS URL must name the exact pinned official release asset")
    return pin


def verified_runtime(cache: Path, pin: dict[str, str]) -> Path:
    archive = cache / pin["asset"]
    cache.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix(archive.suffix + ".partial")
        try:
            with urlopen(pin["url"], timeout=120) as response, temporary.open("wb") as target:
                shutil.copyfileobj(response, target)
            if sha256(temporary) != pin["sha256"]:
                raise ValueError("PBS SHA256 mismatch; refusing to unpack the runtime")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    # Verify cached bytes too. Never extract, execute, or use pip before this check.
    if sha256(archive) != pin["sha256"]:
        raise ValueError(f"PBS SHA256 mismatch for cached runtime: {archive}")
    return archive


def metadata(mode: str, version: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "platform": "linux-x86_64",
        "package": "portable-tar-gz" if mode == "portable" else "standard-deb",
        "launcher_mode": "portable" if mode == "portable" else "standard",
        "version": version,
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run(*args: str | Path, env: dict[str, str] | None = None) -> None:
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True)


def runtime_environment(app: Path, data: Path, mode: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update({
        "PYTHONPATH": os.pathsep.join((str(app), str(app / ".deps"))),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "APP_LAUNCHER_MODE": mode,
        "ILAB_CONJURE_APP_DIR": str(app),
        "ILAB_CONJURE_BUNDLE_DIR": str(app.parent),
        "ILAB_CONJURE_DATA_DIR": str(data),
    })
    return env


def smoke_runtime(bundle: Path, mode: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ilab-package-smoke-") as temporary:
        run(
            bundle / "python/bin/python3", "-c",
            "import ssl, sqlite3, ctypes, fastapi, uvicorn, multipart, httpx, PIL; "
            "import linux_webui_app; print('Linux packaged runtime imports OK')",
            env=runtime_environment(bundle / "app", Path(temporary), mode),
        )


def finish_asset(path: Path) -> str:
    digest = sha256(path)
    path.with_name(path.name + ".sha256.txt").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def clean_bytecode(root: Path) -> None:
    for path in sorted(root.rglob("__pycache__"), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
    for path in root.rglob("*.pyc"):
        path.unlink()


def copy_frontend_licenses(bundle: Path) -> None:
    dependencies = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["dependencies"]
    for name in dependencies:
        source = ROOT / "node_modules" / name
        target = bundle / "third-party-licenses/frontend" / name
        licenses = [path for path in source.iterdir() if path.is_file() and path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))]
        if not licenses:
            raise RuntimeError(f"Frontend dependency license is missing: {name}")
        target.mkdir(parents=True, exist_ok=True)
        for path in licenses:
            shutil.copy2(path, target / path.name)
        shutil.copy2(source / "package.json", target / "package.json")


def build_portable(output: Path, version: str) -> Path:
    pin = runtime_pin()
    archive = verified_runtime(output / "_cache", pin)
    # Build-time tools never ship and are never required on the user's machine.
    run("npm", "ci")
    run("npm", "run", "check:webui")
    run("cargo", "build", "--manifest-path", ROOT / "launcher/Cargo.toml", "--release", "--locked", "--bin", "ilab-conjure-launcher")
    bundle = output / BUNDLE_NAME
    if bundle.exists():
        if (bundle / "data").exists() and any(path.is_file() or path.is_symlink() for path in (bundle / "data").rglob("*")):
            raise ValueError(f"Refusing to replace a build directory containing user data: {bundle}")
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        if any(Path(member.name).parts[0] != "python" for member in tar.getmembers()):
            raise ValueError("Unexpected PBS archive layout")
        tar.extractall(bundle, filter="data")
    app = bundle / "app"
    app.mkdir()
    for item in ("codex_image", "assets", "requirements-webui.txt", "LICENSE", "SECURITY.md"):
        source, target = ROOT / item, app / item
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)
    shutil.copy2(HERE / "linux_webui_app.py", app)
    for item in ("Start-WebUI.sh", "README-portable.md", "README-app.md", "THIRD_PARTY_NOTICES.md", "python-runtime.json"):
        shutil.copy2(HERE / item, bundle)
    shutil.copy2(ROOT / "LICENSE", bundle)
    copy_frontend_licenses(bundle)
    cargo_metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--manifest-path", str(ROOT / "launcher/Cargo.toml"),
         "--format-version", "1", "--no-deps", "--locked"], text=True, cwd=ROOT,
    ))
    shutil.copy2(Path(cargo_metadata["target_directory"]) / "release/ilab-conjure-launcher", bundle)
    (bundle / "data/logs").mkdir(parents=True)
    (bundle / "portable-version.txt").write_text(version + "\n", encoding="utf-8")
    write_json(bundle / "bundle-metadata.json", metadata("portable", version))
    python = bundle / "python/bin/python3"
    env = runtime_environment(app, bundle / "data", "portable")
    # PBS includes pip at build time. Do not perform unpinned pip/setuptools upgrades.
    run(python, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
        "--require-hashes", "--target", app / ".deps", "-r", app / "requirements-webui.txt", env=env)
    if not (app / ".deps/certifi/cacert.pem").is_file():
        raise RuntimeError("certifi CA bundle is missing")
    frozen = subprocess.check_output(
        [str(python), "-m", "pip", "freeze", "--path", str(app / ".deps")], env=env, text=True,
    )
    (bundle / "python-requirements.lock.txt").write_text(frozen, encoding="utf-8")
    run(python, ROOT / "packaging/cleanup-runtime.py", "--app-dir", app,
        "--runtime-dir", bundle / "python", "--platform", "linux", env=env)
    clean_bytecode(bundle)
    smoke_runtime(bundle, "portable")
    artifact = output / f"{BUNDLE_NAME}_{version}.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        tar.add(bundle, arcname=BUNDLE_NAME)
    digest = finish_asset(artifact)
    write_json(output / "linux-portable-build.json", {
        "version": version, "architecture": "x64", "package": "portable-tar-gz",
        "asset": artifact.name, "sha256": digest, "python_runtime": pin,
    })
    print(f"Built {artifact}")
    return bundle


def debian_version(version: str) -> str:
    version = version.removeprefix("v")
    return version if version[:1].isdigit() else "0.0.0~" + version


def build_deb(output: Path, version: str, portable: Path) -> Path:
    expected = metadata("portable", version)
    if json.loads((portable / "bundle-metadata.json").read_text(encoding="utf-8")) != expected:
        raise ValueError("Portable payload metadata does not match this build")
    stage = output / "_deb-stage"
    if stage.exists():
        shutil.rmtree(stage)
    program = stage / "opt/ilab-conjure"
    shutil.copytree(
        portable, program, symlinks=True,
        ignore=lambda directory, names: {"data"} if Path(directory) == portable else set(),
    )
    (program / "portable-version.txt").unlink()
    (program / "Start-WebUI.sh").unlink()
    (program / "app-version.txt").write_text(version + "\n", encoding="utf-8")
    write_json(program / "bundle-metadata.json", metadata("installed", version))
    launcher = stage / "usr/bin/ilab-conjure"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexec /opt/ilab-conjure/ilab-conjure-launcher "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    desktop = stage / "usr/share/applications"
    desktop.mkdir(parents=True)
    shutil.copy2(HERE / "ilab-conjure.desktop", desktop)
    icon = stage / "usr/share/icons/hicolor/scalable/apps/ilab-conjure.svg"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "launcher/assets/rabbit-logo.svg", icon)
    control = stage / "DEBIAN/control"
    control.parent.mkdir()
    control.write_text(
        f"Package: ilab-conjure\nVersion: {debian_version(version)}\nArchitecture: amd64\n"
        "Maintainer: iLab CONJURE contributors <noreply@github.com>\nSection: graphics\nPriority: optional\n"
        f"Depends: {DEB_DEPENDS}\nRecommends: gnome-shell-extension-appindicator\n"
        "Homepage: https://github.com/Tran314/ilab-conjure\n"
        "Description: Local-first AI image generation workbench\n"
        " Bundled Python runtime and prebuilt WebUI with a native tray launcher.\n",
        encoding="utf-8",
    )
    # Root-owned package payload: no writes to /opt, no mutable data in the DEB.
    for path in stage.rglob("*"):
        if not path.is_symlink():
            path.chmod((path.stat().st_mode & 0o755) if path.is_file() else 0o755)
    smoke_runtime(program, "standard")
    artifact = output / f"iLab-GPT-CONJURE-linux-x64_{version}.deb"
    run("dpkg-deb", "--root-owner-group", "--build", stage, artifact)
    digest = finish_asset(artifact)
    write_json(output / "linux-app-build.json", {
        "version": version, "architecture": "x64", "package": "standard-deb",
        "asset": artifact.name, "sha256": digest, "python_runtime": runtime_pin(),
    })
    print(f"Built {artifact}")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("portable", "deb"), required=True)
    parser.add_argument("--version", "-v", required=True)
    parser.add_argument("--output-dir", "-o", type=Path, default=ROOT / ".dist")
    parser.add_argument("--portable-dir", type=Path, help="Reuse this build's matching portable payload for DEB")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("The first Linux release targets native Linux x86_64 only")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~-]*", args.version):
        parser.error("version must be a safe package version without path separators")
    if args.portable_dir and args.format != "deb":
        parser.error("--portable-dir is only supported for DEB builds")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    portable = args.portable_dir.resolve() if args.portable_dir else build_portable(output, args.version)
    if args.format == "deb":
        build_deb(output, args.version, portable)


if __name__ == "__main__":
    main()
