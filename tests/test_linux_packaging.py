from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LinuxPackagingTests(unittest.TestCase):
    def test_pbs_pin_is_exact_and_uses_official_asset(self) -> None:
        build = load_module("linux_build", "packaging/linux/build.py")
        pin = build.runtime_pin()
        self.assertEqual(pin["release"], "20261009")
        self.assertEqual(pin["python_version"], "3.13.16")
        self.assertEqual(pin["triple"], "x86_64-unknown-linux-gnu")
        self.assertEqual(pin["sha256"], "24801ea31c7d406d559a17e2226325c723d50c56900b9c0da7b62adaae2affc9")
        self.assertIn("/releases/download/20261009/", pin["url"])
        self.assertEqual(len(pin["verification_sources"]), 2)

    def test_cached_runtime_corruption_fails_before_unpack(self) -> None:
        build = load_module("linux_build", "packaging/linux/build.py")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            pin = build.runtime_pin()
            (cache / pin["asset"]).write_bytes(b"corrupt runtime")
            with patch.object(build, "urlopen") as network:
                with self.assertRaisesRegex(ValueError, "PBS SHA256 mismatch"):
                    build.verified_runtime(cache, pin)
                network.assert_not_called()

    def test_bundle_identity_and_debian_dependencies_are_explicit(self) -> None:
        build = load_module("linux_build", "packaging/linux/build.py")
        for mode, package, launcher_mode in (
            ("portable", "portable-tar-gz", "portable"),
            ("installed", "standard-deb", "standard"),
        ):
            self.assertEqual(build.metadata(mode, "1.2.3"), {
                "schema_version": 1, "mode": mode, "platform": "linux-x86_64",
                "package": package, "launcher_mode": launcher_mode, "version": "1.2.3",
            })
        self.assertIn("libgtk-3-0t64 | libgtk-3-0", build.DEB_DEPENDS)
        self.assertIn("libayatana-appindicator3-1 | libappindicator3-1", build.DEB_DEPENDS)
        for dependency in ("libxdo3", "curl", "ca-certificates", "zenity", "libglib2.0-bin"):
            self.assertIn(dependency, build.DEB_DEPENDS)
        check = (ROOT / "packaging/linux/verify-runtime.py").read_text()
        self.assertIn("ctypes.CDLL", check)
        for library in ("libgtk-3.so.0", "libayatana-appindicator3.so.1", "libappindicator3.so.1"):
            self.assertIn(library, check)

    def test_linux_runtime_cleanup_preserves_shared_libraries_certificates_and_licenses(self) -> None:
        cleanup = load_module("cleanup_runtime", "packaging/cleanup-runtime.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, runtime = root / "app", root / "python"
            keep = (
                app / "codex_image/webui/static/app.js", app / ".deps/certifi/cacert.pem",
                runtime / "lib/libpython3.13.so.1.0", runtime / "lib/python3.13/lib-dynload/_ssl.so",
                runtime / "share/licenses/Python/LICENSE", runtime / "bin/python3",
            )
            remove = (
                runtime / "include/python3.13/Python.h", runtime / "lib/libpython3.13.a",
                runtime / "lib/python3.13/test/test_os.py", runtime / "lib/python3.13/ensurepip/__init__.py",
                runtime / "lib/python3.13/site-packages/pip/__init__.py", runtime / "bin/pip3",
                app / "codex_image/webui/frontend/src/main.ts", app / "codex_image/webui/static/app.js.map",
            )
            for path in keep + remove:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            cleanup.cleanup_runtime(app, runtime, "linux")
            for path in keep:
                self.assertTrue(path.is_file(), str(path))
            for path in remove:
                self.assertFalse(path.exists(), str(path))

    @unittest.skipUnless(shutil.which("dpkg-deb"), "DEB tools require Linux")
    def test_deb_uses_identical_payload_without_mutable_data(self) -> None:
        build = load_module("linux_build", "packaging/linux/build.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            portable = root / "portable"
            portable.mkdir()
            for relative in ("app/linux_webui_app.py", "app/.deps/example/data/required.dat", "python/bin/python3", "ilab-conjure-launcher", "data/keep-private.txt", "Start-WebUI.sh", "portable-version.txt"):
                path = portable / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            build.write_json(portable / "bundle-metadata.json", build.metadata("portable", "1.2.3"))
            with patch.object(build, "smoke_runtime"):
                asset = build.build_deb(root, "1.2.3", portable)
            extracted = root / "extracted"
            subprocess.run(["dpkg-deb", "-x", str(asset), str(extracted)], check=True)
            program = extracted / "opt/ilab-conjure"
            self.assertFalse((program / "data").exists())
            self.assertTrue((program / "app/.deps/example/data/required.dat").is_file())
            self.assertFalse((program / "portable-version.txt").exists())
            self.assertEqual((program / "app/linux_webui_app.py").read_bytes(), (portable / "app/linux_webui_app.py").read_bytes())
            self.assertEqual(json.loads((program / "bundle-metadata.json").read_text())["mode"], "installed")
            self.assertTrue((extracted / "usr/share/applications/ilab-conjure.desktop").is_file())
            self.assertTrue((extracted / "usr/bin/ilab-conjure").stat().st_mode & 0o111)
            control = subprocess.check_output(["dpkg-deb", "-f", str(asset), "Depends"], text=True)
            self.assertIn("libgtk-3-0t64 | libgtk-3-0", control)
            self.assertEqual(build.sha256(asset), asset.with_name(asset.name + ".sha256.txt").read_text().split()[0])

    def test_glibc_baseline_rejects_newer_builder_abi(self) -> None:
        verifier = load_module("linux_verify", "packaging/linux/verify-runtime.py")
        self.assertEqual(verifier.glibc_versions("GLIBC_2.35 GLIBC_2.39 GLIBCXX_3.4.30"), {(2, 35), (2, 39)})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "launcher").write_bytes(b"\x7fELFfixture")
            with patch.object(verifier.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="GLIBC_2.39")):
                with self.assertRaisesRegex(SystemExit, "requires GLIBC_2.39"):
                    verifier.verify_glibc_baseline(root, "2.35")
            with patch.object(verifier.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="GLIBC_2.35")):
                verifier.verify_glibc_baseline(root, "2.35")

    def test_release_workflow_builds_verifies_and_uploads_both_linux_formats(self) -> None:
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        for value in ("linux-packages:", "linux-package-runtime:", "packaging/linux/smoke-service.py", "--max-glibc 2.35"):
            self.assertIn(value, ci)
        workflow = (ROOT / ".github/workflows/release-portable.yml").read_text()
        for value in ("linux-packages:", "linux-package-runtime:", "ubuntu-22.04", "ubuntu-24.04", "packaging/linux/build-portable.sh", "packaging/linux/build-app.sh", "packaging/linux/verify-runtime.py", "dist/*.tar.gz", "dist/*.deb", '-name "*.tar.gz"', '-name "*.deb"', "      - linux-packages", "      - linux-package-runtime"):
            self.assertIn(value, workflow)


@unittest.skipUnless(shutil.which("bash"), "GitHub Actions version bootstrap uses bash")
class LinuxPackagingVersionBootstrapTests(unittest.TestCase):
    @staticmethod
    def version_step() -> str:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        step = workflow.split("      - name: Read package version\n", 1)[1].split("\n      - name:", 1)[0]
        return textwrap.dedent(step.split("        run: |\n", 1)[1])

    def run_version_step(self, source: str) -> tuple[subprocess.CompletedProcess, str | None]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "codex_image"
            package.mkdir()
            (package / "__init__.py").write_text("raise RuntimeError('package must not be imported during bootstrap')\n")
            (package / "version.py").write_text(source, encoding="utf-8")
            output = root / "github-output"
            result = subprocess.run(
                ["bash", "-c", self.version_step()], cwd=root,
                env={**os.environ, "GITHUB_OUTPUT": str(output)}, capture_output=True, text=True,
            )
            return result, output.read_text() if output.exists() else None

    def test_version_bootstrap_does_not_import_package_or_site_packages(self) -> None:
        source = (ROOT / "codex_image/version.py").read_text(encoding="utf-8")
        source += "\nimport sys\nassert 'site' not in sys.modules\nassert 'codex_image' not in sys.modules\nassert 'httpx' not in sys.modules\n"
        result, output = self.run_version_step(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(output, r"^version=[0-9]+\.[0-9]+\.[0-9]+\n$")

    def test_version_read_failure_stops_before_writing_step_output(self) -> None:
        result, output = self.run_version_step("raise RuntimeError('version read failed')\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version read failed", result.stderr)
        self.assertIsNone(output)

    def test_invalid_version_stops_before_writing_step_output(self) -> None:
        for version in ("", "None", "0.9.7\nother=value"):
            with self.subTest(version=version):
                result, output = self.run_version_step(f"APP_VERSION = {version!r}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid application version", result.stderr)
                self.assertIsNone(output)


class LinuxVersionTests(unittest.TestCase):
    def test_installed_bundle_dir_does_not_imply_portable_and_linux_never_runs_command(self) -> None:
        from codex_image.webui import app_version

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, data = root / "app", root / "user-data"
            app.mkdir()
            data.mkdir()
            (root / "app-version.txt").write_text("1.2.3")
            (root / "portable-version.txt").write_text("9.9.9")
            (root / "Update WebUI Portable.command").write_text("must not execute")
            with patch.dict(os.environ, {
                "APP_LAUNCHER_MODE": "standard", "ILAB_CONJURE_BUNDLE_DIR": str(root),
                "ILAB_CONJURE_APP_DIR": str(app), "ILAB_CONJURE_DATA_DIR": str(data),
            }), patch.object(app_version.platform, "system", return_value="Linux"), patch.object(app_version.subprocess, "Popen") as process:
                result = app_version.app_version_payload(data / "webui-outputs")
                self.assertEqual(result["source"], "standard_app")
                self.assertEqual(result["package"], "standard-deb")
                self.assertEqual(result["current_version"], "1.2.3")
                self.assertFalse(result["portable"])
                self.assertFalse(result["updater_available"])
                with self.assertRaises(FileNotFoundError):
                    app_version.open_portable_updater(data / "webui-outputs")
                os.environ["APP_LAUNCHER_MODE"] = "portable"
                result = app_version.app_version_payload(data / "webui-outputs")
                self.assertEqual(result["package"], "portable-tar-gz")
                self.assertFalse(result["updater_available"])
                self.assertIsNone(result["post_update_onboarding"])
                with self.assertRaises(FileNotFoundError):
                    app_version.open_portable_updater(data / "webui-outputs")
                process.assert_not_called()
                os.environ["APP_LAUNCHER_MODE"] = "source"
                result = app_version.app_version_payload(data / "webui-outputs")
                self.assertEqual(result["source"], "source")
                self.assertIsNone(result["package"])

    def test_linux_shim_honors_absolute_xdg_and_custom_storage_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "profile"
            data.mkdir()
            (data / "webui-settings.json").write_text(json.dumps({
                "input_root": str(root / "custom-input"), "output_root": str(root / "custom-output"),
            }))
            with patch.dict(os.environ, {
                "APP_LAUNCHER_MODE": "standard", "ILAB_CONJURE_DATA_DIR": str(data),
                "XDG_DATA_HOME": str(root / "xdg"),
            }), patch("codex_image.webui.app.create_app") as create:
                shim = load_module("linux_webui_app", "packaging/linux/linux_webui_app.py")
                self.assertEqual(shim.default_data_dir(), root / "xdg/iLab GPT CONJURE")
                self.assertEqual(create.call_args.kwargs["input_root"], root / "custom-input")
                self.assertEqual(create.call_args.kwargs["output_root"], root / "custom-output")
                self.assertEqual(create.call_args.kwargs["webui_settings_path"], data / "webui-settings.json")
                for xdg in ("", "relative/xdg", "~/not-absolute"):
                    os.environ["XDG_DATA_HOME"] = xdg
                    self.assertEqual(shim.default_data_dir(), Path.home() / ".local/share/iLab GPT CONJURE")
                os.environ["APP_LAUNCHER_MODE"] = "portable"
                self.assertEqual(shim.default_data_dir(), shim.BUNDLE_DIR / "data")


class LinuxManifestFailureTests(unittest.TestCase):
    def test_missing_duplicate_and_corrupt_linux_assets_fail_closed(self) -> None:
        manifest = load_module("update_manifest", "scripts/build-update-manifest.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for prefix, suffix, _ in list(manifest.PLATFORM_PATTERNS.values()) + list(manifest.STANDARD_PLATFORM_PATTERNS.values()):
                path = root / f"{prefix}1.2.3{suffix}"
                path.write_bytes(b"package")
                path.with_name(path.name + ".sha256.txt").write_text(hashlib.sha256(b"package").hexdigest())
            def build():
                return manifest.build_manifest(assets_dir=root, version="1.2.3", tag="v1.2.3", repo="Tran314/ilab-conjure", notes="")
            self.assertEqual(build()["platforms"]["linux-x86_64"]["package"], "portable-tar-gz")
            with self.assertRaisesRegex(SystemExit, "release asset version mismatch"):
                manifest.build_manifest(assets_dir=root, version="9.9.9", tag="v9.9.9", repo="Tran314/ilab-conjure", notes="")
            tar = root / "ilab-gpt-conjure_linux_portable_x64_1.2.3.tar.gz"
            for candidate in (tar, root / "iLab-GPT-CONJURE-linux-x64_1.2.3.deb"):
                stale = candidate.with_name(candidate.name.replace("1.2.3", "0.0.1"))
                candidate.rename(stale)
                with self.assertRaisesRegex(SystemExit, "release asset version mismatch"):
                    build()
                stale.rename(candidate)
            duplicate = root / "duplicate" / tar.name
            duplicate.parent.mkdir()
            duplicate.write_bytes(b"package")
            with self.assertRaisesRegex(SystemExit, "exactly one linux-x86_64"):
                build()
            duplicate.unlink()
            tar.write_bytes(b"corrupt")
            with self.assertRaisesRegex(SystemExit, "SHA256 mismatch"):
                build()
            tar.unlink()
            with self.assertRaisesRegex(SystemExit, "exactly one linux-x86_64"):
                build()
            tar.write_bytes(b"package")
            deb = root / "iLab-GPT-CONJURE-linux-x64_1.2.3.deb"
            deb.write_bytes(b"corrupt")
            with self.assertRaisesRegex(SystemExit, "SHA256 mismatch"):
                build()
            deb.unlink()
            with self.assertRaisesRegex(SystemExit, "exactly one standard linux-x86_64"):
                build()


if __name__ == "__main__":
    unittest.main()
