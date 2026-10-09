"""Regression coverage for backup restoration, using real isolated stores."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ProviderRestoreRegressionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_user_config_backup_components import UserConfigBackupComponentTests
        from tests.test_provider_settings_v2 import ProviderSettingsV2Tests
        from codex_image.webui.user_config_backup_import import UserConfigBackupImportService

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        helper = UserConfigBackupComponentTests()
        self.source_planner, self.source = helper._planner(self.root / "source")
        self.target_planner, self.target = helper._planner(self.root / "target")
        self.fixtures = ProviderSettingsV2Tests()
        self.restore = UserConfigBackupImportService(
            self.target_planner, self.root / "imports", min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(self.restore.close)

    def _round_trip(self, current, imported, *, mode, include_keys=False):
        from tests.test_user_config_backup_import import UserConfigBackupImportTests
        from tests.test_user_config_backup_export import DirectExecutor
        from codex_image.webui.user_config_backup_components import ClientPreferences
        from codex_image.webui.user_config_backup_export import UserConfigBackupExportService

        self.target["provider_settings"].replace_snapshot(current)
        self.source["provider_settings"].replace_snapshot(imported)
        exporter = UserConfigBackupExportService(
            self.source_planner, self.root / "exports", executor=DirectExecutor(),
            min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(exporter.close)
        job = exporter.create(("settings",), include_keys, ClientPreferences("system", True, False))
        payload = exporter.download_path(job.job_id).read_bytes()
        session = UserConfigBackupImportTests()._upload(self.restore, payload)
        before = self.target["provider_settings"].path.read_bytes()
        preview = self.restore.validate(session.session_id)
        self.assertTrue(preview.restorable)
        self.assertEqual(self.target["provider_settings"].path.read_bytes(), before)
        result = self.restore.restore(
            session.session_id, sections=("settings",), mode=mode,
            archive_sha256=preview.archive_sha256, preview_revision=preview.preview_revision,
            confirm_replace=mode == "replace",
        )
        self.assertEqual(result.status, "restored")
        return self.target["provider_settings"].backup_snapshot(include_api_keys=True)

    def test_replace_inherits_keys_only_on_same_origin(self):
        for url, expected in (
            ("https://relay.example/another-path", "current-dummy"),
            ("https://RELAY.example:443/v2", "current-dummy"),
            ("https://other.invalid/v1", ""),
            ("http://relay.example/v1", ""),
            ("https://relay.example:444/v1", ""),
        ):
            with self.subTest(url=url):
                current = self.fixtures.v2_payload(providers=[self.fixtures.provider(api_key="current-dummy")])
                imported = self.fixtures.v2_payload(providers=[self.fixtures.provider(base_url=url)])
                restored = self._round_trip(current, imported, mode="replace")
                self.assertEqual(restored["providers"][0]["api_key"], expected)

    def test_replace_respects_explicit_imported_key_on_different_origin(self):
        current = self.fixtures.v2_payload()
        imported = self.fixtures.v2_payload(providers=[self.fixtures.provider(
            base_url="https://other.invalid/v1", api_key="imported-dummy",
        )])
        restored = self._round_trip(current, imported, mode="replace", include_keys=True)
        self.assertEqual(restored["providers"][0]["api_key"], "imported-dummy")

    def test_incremental_imported_keys_require_same_origin(self):
        for url, expected in (("https://relay.example/v2", "imported-dummy"),
                              ("https://other.invalid/v1", "")):
            with self.subTest(url=url):
                current = self.fixtures.v2_payload(providers=[self.fixtures.provider(api_key="")])
                imported = self.fixtures.v2_payload(providers=[self.fixtures.provider(
                    base_url=url, api_key="imported-dummy",
                )])
                restored = self._round_trip(current, imported, mode="incremental", include_keys=True)
                self.assertEqual(restored["providers"][0]["api_key"], expected)

    def _gemini(self, provider_id="gemini"):
        return self.fixtures.provider(id=provider_id, bindings=[{
            "id": "gemini-image", "canonical_model_id": "nano-banana-pro",
            "remote_model_id": "gemini-image", "protocol_profile": "gemini_generate_content",
            "parameter_codec": "gemini_generate_content_image", "operations": ["generate", "edit"],
        }])

    def test_different_models_restore_in_both_modes(self):
        for mode in ("incremental", "replace"):
            with self.subTest(mode=mode):
                current = self.fixtures.v2_payload()
                imported = self.fixtures.v2_payload(
                    providers=[self._gemini()], active_provider_id="gemini",
                    default_provider_by_model={"nano-banana-pro": "gemini"},
                )
                restored = self._round_trip(current, imported, mode=mode)
                self.assertEqual(restored["default_provider_by_model"], {
                    "gpt-image-2": "relay", "nano-banana-pro": "gemini",
                })
                self.assertEqual(restored["active_provider_id"], "relay" if mode == "incremental" else "gemini")

    def test_incremental_id_collision_does_not_add_unsupported_defaults(self):
        current = self.fixtures.v2_payload()
        imported = self.fixtures.v2_payload(
            providers=[self._gemini("relay"), self._gemini("available")],
            default_provider_by_model={"nano-banana-pro": "relay"},
        )
        original = deepcopy(imported)
        self.target["provider_settings"].replace_snapshot(current)
        candidate = self.restore._merge_providers(imported)
        self.assertEqual(imported, original)
        self.assertEqual(candidate["default_provider_by_model"], {
            "gpt-image-2": "relay", "nano-banana-pro": "available",
        })

    def test_existing_default_choice_wins_in_incremental_mode(self):
        current = self.fixtures.v2_payload()
        imported = self.fixtures.v2_payload(
            providers=[self.fixtures.provider(id="second")],
            active_provider_id="second", default_provider_by_model={"gpt-image-2": "second"},
        )
        restored = self._round_trip(current, imported, mode="incremental")
        self.assertEqual(restored["default_provider_by_model"], {"gpt-image-2": "relay"})

    def test_permission_failure_closes_upload_and_removes_partial_file(self):
        descriptors = []

        def fail(descriptor):
            descriptors.append(descriptor)
            raise OSError("permission setup failed")

        with patch("codex_image.webui.user_config_backup_import.restrict_file_descriptor", side_effect=fail):
            with self.assertRaisesRegex(OSError, "permission setup failed"):
                self.restore.create("settings.zip", 10)
        self.assertEqual(list(self.restore.root.glob("*.upload")), [])
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        # A failed creation must not retain the single active session slot.
        self.assertEqual(self.restore.create("retry.zip", 10).status, "uploading")


if __name__ == "__main__":
    unittest.main()
