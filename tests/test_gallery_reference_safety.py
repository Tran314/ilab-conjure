from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from codex_image.webui.gallery_storage import (
    GalleryItemInUseError, GalleryProtectionUnavailableError, GallerySnapshot,
    GalleryStorage,
)
from codex_image.webui.user_config_backup_import import UserConfigBackupImportService
from tests import test_user_config_backup_components as component_tests
from tests import test_user_config_backup_import as import_tests


def png(color):
    buffer = BytesIO()
    Image.new("RGB", (3, 3), color).save(buffer, format="PNG")
    return buffer.getvalue()


class GalleryReferenceSafetyTests(unittest.TestCase):
    def test_delete_rejects_parent_directory_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "gallery"
            root.mkdir()
            storage = GalleryStorage(root, reference_counts_provider=lambda: {})
            for item_id in (".", "..", "../gallery"):
                with self.subTest(item_id=item_id), self.assertRaises(ValueError):
                    storage.delete_item(item_id)
            self.assertTrue(root.is_dir())

    def test_route_returns_503_and_preserves_files_when_history_scan_fails(self):
        from fastapi.testclient import TestClient
        from codex_image.webui.app import create_app
        from tests.webui_helpers import FakeImageClient

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(output_root=root / "outputs", gallery_root=root / "gallery",
                             client_factory=lambda: FakeImageClient(), auth_checker=lambda: True,
                             auto_start_queue=False)
            client = TestClient(app)
            item = app.state.gallery_storage.create_item("Keep", "portrait", "image.png", png("red"), "image/png")
            app.state.gallery_storage._reference_counts_provider = lambda: None
            response = client.delete(f"/api/gallery/{item['id']}")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "gallery_protection_unavailable")
            self.assertEqual(app.state.gallery_storage.image_path(item["id"]).read_bytes(), png("red"))

    def test_delete_fails_closed_without_provider_or_with_invalid_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = GalleryStorage(Path(directory))
            item = storage.create_item("Keep", "portrait", "keep.png", png("red"), "image/png")
            for provider in (None, lambda: None, lambda: {item["id"]: -1},
                             lambda: {item["id"]: "1"}):
                storage._reference_counts_provider = provider
                with self.subTest(provider=provider), self.assertRaises(GalleryProtectionUnavailableError):
                    storage.delete_item(item["id"])
                self.assertEqual(storage.image_path(item["id"]).read_bytes(), png("red"))

    def test_referenced_image_cannot_be_deleted_or_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            counts = {}
            storage = GalleryStorage(Path(directory), reference_counts_provider=lambda: counts)
            item = storage.create_item("Keep", "portrait", "keep.png", png("red"), "image/png")
            counts[item["id"]] = 2
            with self.assertRaises(GalleryItemInUseError) as raised:
                storage.delete_item(item["id"])
            self.assertEqual(raised.exception.reference_count, 2)
            with self.assertRaises(GalleryItemInUseError):
                storage.replace_item_image(item["id"], filename="changed.png", data=png("blue"))
            self.assertEqual(storage.image_path(item["id"]).read_bytes(), png("red"))
            counts.clear()
            storage.delete_item(item["id"])
            self.assertEqual(storage.list_items(), [])


class GalleryRestoreSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        helper = component_tests.UserConfigBackupComponentTests()
        self.source, self.source_stores = helper._planner(self.root / "source")
        self.target, self.target_stores = helper._planner(self.root / "target")
        self.tasks = self.target.prompt_template_thumbnail_resolver.task_storage
        self.service = UserConfigBackupImportService(
            self.target, self.root / "imports", min_free_bytes=0, free_ratio=0,
        )
        self.addCleanup(self.service.close)

    def _item(self, planner, name="Reference", color="red"):
        return planner.gallery_storage.create_item(name, "portrait", "image.png", png(color), "image/png")

    def _reference(self, item_id, task_id="historical-task"):
        self.tasks.write_metadata(task_id, {
            "task_id": task_id, "status": "completed", "gallery_refs": [{"id": item_id}],
        })

    def _preview(self, sections=("gallery",)):
        from codex_image.webui.user_config_backup_components import ClientPreferences
        plan = self.source.plan(
            sections, include_api_keys=False,
            client_preferences=ClientPreferences("system", True, False) if "settings" in sections else None,
        )
        payload = import_tests.UserConfigBackupImportTests()._archive(
            self.root / "archive.zip", members={
                member.entry.path: member.data if member.data is not None else member.source_path.read_bytes()
                for member in plan.members
            },
        )
        session = self.service.create("archive.zip", len(payload))
        self.service.append_chunk(session.session_id, 0, payload, hashlib.sha256(payload).hexdigest())
        return self.service.validate(session.session_id)

    def _restore(self, preview, mode="replace"):
        return self.service.restore(
            preview.session_id, sections=tuple(section.section for section in preview.sections), mode=mode,
            archive_sha256=preview.archive_sha256, preview_revision=preview.preview_revision,
            confirm_replace=mode == "replace",
        )

    def _source_with_target_id(self, item_id, color):
        self._item(self.source, color=color)
        snapshot = self.source.gallery_storage.snapshot()
        item = snapshot.items[0]
        # A backup of an older image can have the same gallery ID with different bytes.
        self.source.gallery_storage.write_snapshot(GallerySnapshot(
            snapshot.categories, (replace(item, metadata={**item.metadata, "id": item_id}),),
        ))
        # Retain only the same-ID archive record (snapshot writes are additive).
        self.source.gallery_storage.delete_item(str(item.metadata["id"]))

    def test_replace_preserves_referenced_ids_and_incremental_keeps_conflict_copy(self):
        current = self._item(self.target)
        self._item(self.source, color="blue")
        self._reference(current["id"])
        self._reference(current["id"], "second-task")
        preview = self._preview()
        self.assertEqual(preview.gallery_history_reference_impact, 2)
        self.assertEqual(preview.sections[0].conflicts, 1)
        self.assertIn("user_config_restore_gallery_references_block_replace", preview.sections[0].warnings)
        with self.assertRaisesRegex(ValueError, "gallery_references_block_replace"):
            self._restore(preview)
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))
        self.assertEqual(list(self.service.root.glob("*.rollback*")), [])
        result = self._restore(preview, "incremental")
        self.assertEqual(result.section_stats["gallery"].recovery_copies, 1)
        self.assertEqual(len(self.target.gallery_storage.list_items()), 2)
        self.assertEqual(self.tasks.read_metadata("historical-task")["gallery_refs"], [{"id": current["id"]}])

    def test_replace_same_id_changed_image_is_blocked(self):
        current = self._item(self.target)
        self._source_with_target_id(current["id"], "blue")
        self._reference(current["id"])
        preview = self._preview()
        self.assertEqual(preview.gallery_history_reference_impact, 1)
        with self.assertRaisesRegex(ValueError, "gallery_references_block_replace"):
            self._restore(preview)
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))

    def test_direct_snapshot_cannot_overwrite_a_referenced_image(self):
        current = self._item(self.target)
        self._source_with_target_id(current["id"], "blue")
        self._reference(current["id"])
        with self.assertRaises(GalleryItemInUseError):
            self.target.gallery_storage.write_snapshot(self.source.gallery_storage.snapshot())
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))

    def test_incremental_conflict_copy_does_not_overwrite_existing_recovery_id(self):
        original = self._item(self.target, color="red")
        imported = self._item(self.source, color="blue")
        reserved = self._item(self.target, "Already restored", "green")
        snapshot = self.target.gallery_storage.snapshot()
        reserved_item = next(item for item in snapshot.items if item.metadata["id"] == reserved["id"])
        collision_id = f"restored-{imported['sha256'][:16]}"
        self.target.gallery_storage.write_snapshot(GallerySnapshot(snapshot.categories, (
            replace(reserved_item, metadata={**reserved_item.metadata, "id": collision_id}),
        )))
        self.target.gallery_storage.delete_item(reserved["id"])
        preview = self._preview()
        self.assertEqual(self._restore(preview, "incremental").status, "restored")
        self.assertEqual(self.target.gallery_storage.image_path(original["id"]).read_bytes(), png("red"))
        self.assertEqual(self.target.gallery_storage.image_path(collision_id).read_bytes(), png("green"))
        self.assertEqual(self.target.gallery_storage.image_path(collision_id + "-2").read_bytes(), png("blue"))

    def test_replace_same_id_and_same_image_keeps_history_valid(self):
        current = self._item(self.target)
        self._source_with_target_id(current["id"], "red")
        self._reference(current["id"])
        preview = self._preview()
        self.assertEqual(preview.gallery_history_reference_impact, 0)
        self.assertEqual(preview.sections[0].identical_count, 1)
        self.assertEqual(self._restore(preview).status, "restored")
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))

    def test_reference_added_after_preview_blocks_replacement(self):
        current = self._item(self.target)
        self._item(self.source, color="blue")
        preview = self._preview()
        self.assertEqual(preview.gallery_history_reference_impact, 0)
        self._reference(current["id"])
        with self.assertRaisesRegex(ValueError, "gallery_references_block_replace"):
            self._restore(preview)
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))

    def test_unreadable_history_blocks_deletion_and_restore_without_writes(self):
        current = self._item(self.target)
        self._item(self.source, color="blue")
        preview = self._preview()
        self._reference(current["id"])
        self.tasks.metadata_path("historical-task").write_text("{broken")
        with self.assertRaises(GalleryProtectionUnavailableError):
            self.target.gallery_storage.delete_item(current["id"])
        with self.assertRaisesRegex(ValueError, "gallery_protection_unavailable"):
            self._restore(preview)
        self.assertEqual(self.target.gallery_storage.image_path(current["id"]).read_bytes(), png("red"))

    def test_preview_counts_content_duplicates_conflicts_and_missing_thumbnails(self):
        helper = component_tests.UserConfigBackupComponentTests()
        helper._populate(self.root / "source", self.source_stores)
        helper._populate(self.root / "target", self.target_stores)
        self.source.webui_settings.write_locale("en-US")
        self._item(self.source, "Different", "blue")
        self._item(self.target, "Different", "green")
        preview = self._preview(("chips", "gallery", "templates", "settings"))
        sections = {item.section: item for item in preview.sections}
        self.assertEqual((sections["chips"].identical_count, sections["chips"].conflicts), (4, 0))
        self.assertEqual((sections["gallery"].identical_count, sections["gallery"].conflicts), (1, 1))
        self.assertEqual((sections["templates"].identical_count, sections["templates"].missing_assets), (5, 1))
        self.assertEqual((sections["settings"].identical_count, sections["settings"].conflicts), (3, 1))
        self.assertIn("user_config_restore_client_preferences_unchecked", sections["settings"].warnings)

    def test_changed_provider_key_invalidates_preview_without_exposing_key(self):
        preview = self._preview(("settings",))
        self.target.provider_settings.write({"api_key": "private-key-change"})
        with self.assertRaisesRegex(ValueError, "preview_stale"):
            self._restore(preview)
        self.assertNotIn("private-key-change", repr(preview))


if __name__ == "__main__":
    unittest.main()
