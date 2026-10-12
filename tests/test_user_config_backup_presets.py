"""Named presets remain optional, portable, and transactional in schema-1 backups."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5
import zipfile


MEMBER = "settings/generation-presets.json"


def preset(name="Portrait", **changes):
    return {
        "id": str(uuid5(NAMESPACE_URL, name)),
        "name": name,
        "operation": "generate",
        "canonical_model_id": "gpt-image-2",
        "provider_id": "missing-provider",
        "binding_id": "missing-binding",
        "parameters": {"canvas.size": "1024x1024", "gpt.quality": "high"},
        "reference_rules": {"images": "preserve", "files": "preserve", "order": "preserve"},
        "request_options": {},
        "created_at": "2026-10-12T12:00:00Z",
        "updated_at": "2026-10-12T12:00:00Z",
        **changes,
    }


class UserConfigBackupPresetTests(unittest.TestCase):
    def setUp(self):
        from tests.test_user_config_backup_components import UserConfigBackupComponentTests
        from codex_image.webui.generation_presets import GenerationPresetSettings
        from codex_image.webui.user_config_backup_components import UserConfigBackupPlanner
        from codex_image.webui.user_config_backup_import import UserConfigBackupImportService

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        helper = UserConfigBackupComponentTests()
        _, self.source = helper._planner(self.root / "source")
        _, self.target = helper._planner(self.root / "target")
        self.source_store = GenerationPresetSettings(self.root / "source" / "generation-presets.json")
        self.target_store = GenerationPresetSettings(self.root / "target" / "generation-presets.json")
        self.source_planner = UserConfigBackupPlanner(**self.source, generation_preset_settings=self.source_store)
        self.target_planner = UserConfigBackupPlanner(**self.target, generation_preset_settings=self.target_store)
        self.service = UserConfigBackupImportService(
            self.target_planner, self.root / "imports", min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(self.service.close)

    def _archive(self, *, legacy=False, mutate=None):
        from tests.test_user_config_backup_import import UserConfigBackupImportTests
        from codex_image.webui.user_config_backup_components import ClientPreferences

        plan = self.source_planner.plan(
            ("settings",), include_api_keys=False,
            client_preferences=ClientPreferences("system", True, False),
        )
        members = {item.entry.path: item.data for item in plan.members}
        if legacy:
            members.pop(MEMBER)
        if mutate:
            mutate(members)
        return UserConfigBackupImportTests()._archive(self.root / "archive.zip", members=members)

    def _preview(self, data):
        session = self.service.create("backup.zip", len(data))
        self.service.append_chunk(session.session_id, 0, data, hashlib.sha256(data).hexdigest())
        return self.service.validate(session.session_id)

    def _restore(self, preview, mode="incremental"):
        return self.service.restore(
            preview.session_id, sections=("settings",), mode=mode,
            archive_sha256=preview.archive_sha256, preview_revision=preview.preview_revision,
            confirm_replace=mode == "replace",
        )

    def _write(self, store, *records):
        store.write({"version": 1, "presets": list(records)})

    def test_old_archive_never_clears_presets_in_either_mode(self):
        self._write(self.target_store, preset())
        original = self.target_store.path.read_bytes()
        for mode in ("incremental", "replace"):
            with self.subTest(mode=mode):
                preview = self._preview(self._archive(legacy=True))
                section = preview.sections[0]
                self.assertEqual(section.archive_count, 5)
                self.assertEqual(section.replace_existing_count, 5)
                self.assertNotIn("generation_presets", {group.group for group in section.groups})
                self.assertIn("user_config_restore_generation_presets_preserved", section.warnings)
                self._restore(preview, mode)
                self.assertEqual(self.target_store.path.read_bytes(), original)

    def test_legacy_archive_does_not_create_an_absent_preset_store(self):
        self.assertFalse(self.target_store.path.exists())
        self._restore(self._preview(self._archive(legacy=True)), "replace")
        self.assertFalse(self.target_store.path.exists())

    def test_export_round_trip_preserves_unknown_provider_without_secrets(self):
        from tests.test_user_config_backup_export import DirectExecutor
        from codex_image.webui.user_config_backup_components import ClientPreferences
        from codex_image.webui.user_config_backup_export import UserConfigBackupExportService

        record = preset(request_options={"main_model": "gpt-5", "prompt_fidelity": "strict"})
        self._write(self.source_store, record)
        self.source["provider_settings"].write({"api_key": "sk-private-sentinel"})
        exporter = UserConfigBackupExportService(
            self.source_planner, self.root / "exports", executor=DirectExecutor(),
            min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(exporter.close)
        job = exporter.create(("settings",), False, ClientPreferences("system", True, False))
        archive_path = exporter.download_path(job.job_id)
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format_version"], 1)
            self.assertFalse(manifest["contains_secrets"])
            self.assertEqual(json.loads(archive.read(MEMBER)), {"version": 1, "presets": [record]})
            for member in archive.namelist():
                self.assertNotIn(b"sk-private-sentinel", archive.read(member))
        self._restore(self._preview(archive_path.read_bytes()))
        self.assertEqual(self.target_store.read(), self.source_store.read())
        self.assertEqual(self.target_store.read()["presets"][0]["provider_id"], "missing-provider")
        from codex_image.webui.generation_presets import generation_preset_compatibility
        compatibility = generation_preset_compatibility(self.target_store.list()[0], {"providers": []})
        self.assertFalse(compatibility["compatible"])
        self.assertEqual(compatibility["issues"][0]["code"], "provider_not_found")
        self.assertEqual({item.section: item.item_count for item in self.source_planner.summary()}["settings"], 6)

    def test_merge_conflicts_get_unique_ids_and_length_safe_names(self):
        name = "画" * 120
        local = preset(name)
        collision = preset(name, parameters={"gpt.quality": "low"})
        existing_copy = preset(name[:109] + " (restored)")
        self._write(self.target_store, local, existing_copy)
        self._write(self.source_store, collision)
        preview = self._preview(self._archive())
        section = preview.sections[0]
        self.assertEqual(section.archive_count, 6)
        self.assertEqual(section.replace_existing_count, 7)
        self.assertEqual(section.conflicts, 1)
        self.assertIn("user_config_restore_conflicts_create_copies", section.warnings)
        self.assertNotIn("user_config_restore_settings_keep_existing", section.warnings)
        result = self._restore(preview)
        records = self.target_store.read()["presets"]
        self.assertEqual(records[:2], [local, existing_copy])
        self.assertNotIn(records[2]["id"], {local["id"], existing_copy["id"]})
        self.assertEqual(len(records[2]["name"]), 120)
        self.assertTrue(records[2]["name"].endswith(" (restored 2)"))
        self.assertEqual(result.section_stats["settings"].added, 1)
        self.assertEqual(result.section_stats["settings"].recovery_copies, 1)

    def test_same_named_configuration_ignores_storage_history_for_duplicates(self):
        local = preset()
        self._write(self.target_store, local)
        self._write(self.source_store, preset(
            id=str(uuid5(NAMESPACE_URL, "different")), updated_at="2026-10-12T13:00:00Z",
        ))
        preview = self._preview(self._archive())
        self.assertEqual(preview.sections[0].identical_count, 5)
        result = self._restore(preview)
        self.assertEqual(self.target_store.read()["presets"], [local])
        self.assertEqual(result.section_stats["settings"].skipped, 1)

    def test_replace_preview_counts_records_and_replaces_only_after_confirmation(self):
        self._write(self.source_store, preset("Imported"))
        self._write(self.target_store, preset("Local A"), preset("Local B"), preset("Local C"))
        before = self.target_store.path.read_bytes()
        preview = self._preview(self._archive())
        self.assertEqual(self.target_store.path.read_bytes(), before)
        section = preview.sections[0]
        self.assertEqual((section.archive_count, section.replace_existing_count), (6, 8))
        groups = {group.group: group for group in section.groups}
        self.assertEqual((groups["generation_presets"].archive_count, groups["generation_presets"].current_count), (1, 3))
        with self.assertRaisesRegex(ValueError, "confirm_replace_required"):
            self.service.restore(
                preview.session_id, sections=("settings",), mode="replace",
                archive_sha256=preview.archive_sha256, preview_revision=preview.preview_revision,
                confirm_replace=False,
            )
        self.assertEqual(self.target_store.path.read_bytes(), before)
        result = self._restore(preview, "replace")
        self.assertEqual(self.target_store.read(), self.source_store.read())
        self.assertEqual(result.section_stats["settings"].replaced, 8)

    def test_explicit_empty_presets_retains_empty_replace_safety_guard(self):
        self._write(self.target_store, preset())
        before = self.target_store.path.read_bytes()
        preview = self._preview(self._archive())
        groups = {group.group: group for group in preview.sections[0].groups}
        self.assertEqual((groups["generation_presets"].archive_count, groups["generation_presets"].current_count), (0, 1))
        with self.assertRaisesRegex(ValueError, "empty_replace_blocked"):
            self._restore(preview, "replace")
        self.assertEqual(self.target_store.path.read_bytes(), before)
        self._restore(preview)
        self.assertEqual(self.target_store.read()["presets"], [preset()])

    def test_preset_changes_invalidate_preview(self):
        self._write(self.source_store, preset())
        preview = self._preview(self._archive())
        self._write(self.target_store, preset("Newer local change"))
        before = self.target_store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "preview_stale"):
            self._restore(preview)
        self.assertEqual(self.target_store.path.read_bytes(), before)

    def test_failed_completion_rolls_back_presets_and_all_settings(self):
        self._write(self.source_store, preset("Imported"))
        self._write(self.target_store, preset("Local"))
        self.target["webui_settings"].write_locale("zh-CN")
        before = self.service._capture_selected_json_backups(("settings",))
        preview = self._preview(self._archive())
        write_status = self.service._write_status

        def fail_after_apply(record):
            if record.session.status == "restored":
                raise OSError("simulated commit failure")
            write_status(record)

        with patch.object(self.service, "_write_status", side_effect=fail_after_apply):
            with self.assertRaisesRegex(ValueError, "apply_failed"):
                self._restore(preview, "replace")
        self.assertEqual(self.service._capture_selected_json_backups(("settings",)), before)
        self.assertEqual(self.service.get_snapshot(preview.session_id).session.status, "validated")

    def test_startup_rollback_journal_includes_preset_store(self):
        from codex_image.webui.user_config_backup_import import UserConfigBackupImportService

        self._write(self.target_store, preset("Before crash"))
        before = self.target_store.path.read_bytes()
        backups = self.service._capture_selected_json_backups(("settings",))
        self.assertIn(self.target_store.path, backups)
        session_id = "f" * 32
        self.service._write_rollback_journal(session_id, backups, {})
        self._write(self.target_store, preset("Half written"))
        recovered = UserConfigBackupImportService(
            self.target_planner, self.root / "imports", min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(recovered.close)
        self.assertEqual(self.target_store.path.read_bytes(), before)
        self.assertFalse((self.service.root / f"{session_id}.rollback").exists())

    def test_schema_keys_secrets_paths_and_future_versions_are_rejected(self):
        from codex_image.webui.user_config_backup_validation import UserConfigArchiveValidator
        from codex_image.webui.user_config_backup_format import parse_user_config_manifest
        from io import BytesIO

        base = {"version": 1, "presets": [preset()]}
        invalid = [
            {**base, "version": 2}, {**base, "unexpected": True},
            {"version": 1, "presets": [preset(), preset()]},
            {"version": 1, "presets": [preset(prompt="do not store prompts")]},
            {"version": 1, "presets": [preset(api_key="sk-do-not-restore")]},
            {"version": 1, "presets": [preset(request_options={"prompt": "do not store prompts"})]},
            {"version": 1, "presets": [preset(request_options={"main_model": "/private/model"})]},
            {"version": 1, "presets": [preset(parameters={"input_root": "/private/local"})]},
            {"version": 1, "presets": [preset(reference_rules={"images": ["/private/image.png"], "files": "preserve", "order": "preserve"})]},
        ]
        self._write(self.target_store, preset("Keep"))
        before = self.target_store.path.read_bytes()
        validator = UserConfigArchiveValidator(100, 1_000_000, 1_000_000, 200)
        for bad in invalid:
            with self.subTest(bad=bad):
                data = self._archive(mutate=lambda members: members.__setitem__(MEMBER, json.dumps(bad).encode()))
                with zipfile.ZipFile(BytesIO(data)) as archive:
                    manifest = parse_user_config_manifest(archive.read("manifest.json"))
                    staging = self.root / "validation"
                    archive.extractall(staging)
                with self.assertRaises(ValueError):
                    validator._validate_semantics(manifest, staging)
                self.assertEqual(self.target_store.path.read_bytes(), before)

    def test_merge_preview_accounts_for_conflicts_created_by_earlier_renames(self):
        self._write(self.target_store, preset("Portrait", parameters={"gpt.quality": "low"}))
        self._write(self.source_store, preset("Portrait"), preset("Portrait (restored)"))
        preview = self._preview(self._archive())
        self.assertEqual(preview.sections[0].conflicts, 2)
        result = self._restore(preview)
        self.assertEqual(result.section_stats["settings"].recovery_copies, 2)
        self.assertEqual(len({item["name"].casefold() for item in self.target_store.list()}), 3)

    def test_oversize_merge_warns_without_blocking_valid_replace(self):
        self._write(self.target_store, *(preset(f"Local {index}") for index in range(200)))
        self._write(self.source_store, preset("Imported"))
        before = self.target_store.path.read_bytes()
        preview = self._preview(self._archive())
        self.assertTrue(preview.restorable)
        self.assertIn("user_config_restore_generation_presets_merge_limit", preview.sections[0].warnings)
        with self.assertRaisesRegex(ValueError, "generation_presets_merge"):
            self._restore(preview)
        self.assertEqual(self.target_store.path.read_bytes(), before)
        self._restore(preview, "replace")
        self.assertEqual(self.target_store.read(), self.source_store.read())

    def test_duplicate_preset_json_keys_are_rejected(self):
        data = self._archive(mutate=lambda members: members.__setitem__(
            MEMBER, b'{"version":2,"version":1,"presets":[]}',
        ))
        with self.assertRaisesRegex(ValueError, "json_invalid"):
            self._preview(data)

    def test_missing_required_settings_member_is_still_rejected(self):
        data = self._archive(mutate=lambda members: members.pop("settings/network.json"))
        with self.assertRaisesRegex(ValueError, "settings_members_invalid"):
            self._preview(data)

    def test_preset_store_lock_is_part_of_settings_lock_order(self):
        from contextlib import contextmanager

        calls = []

        @contextmanager
        def marker():
            calls.append("entered")
            yield
            calls.append("exited")

        with patch.object(self.target_store, "exclusive", side_effect=marker):
            with self.target_planner._exclusive(("settings",)):
                self.assertEqual(calls, ["entered"])
        self.assertEqual(calls, ["entered", "exited"])


if __name__ == "__main__":
    unittest.main()
