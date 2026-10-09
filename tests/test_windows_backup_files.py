"""Native Windows regressions; junction tests require no symlink privilege."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_image.webui.storage_metadata_scan import SourceMetadataScanner
from tests.file_security_helpers import assert_private_file


@unittest.skipUnless(os.name == "nt", "Windows native file APIs")
class WindowsBackupFileTests(unittest.TestCase):
    def setUp(self):
        from codex_image import windows_files
        self.files = windows_files
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def _scanner(self):
        # Match TaskStorage: runner temp paths may contain an 8.3 alias.
        trust_root = self.source.resolve(strict=True)
        info = trust_root.stat()
        return SourceMetadataScanner(self.source, trust_root, (info.st_dev, info.st_ino))

    def _junction(self, path, target):
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path), str(target)],
                       check=True, capture_output=True)
        self.assertTrue(path.is_dir())
        self.assertTrue(path.lstat().st_file_attributes & 0x400)

    def test_private_files_use_protected_acl_and_allow_owner_updates(self):
        from codex_image.atomic_files import atomic_write_text
        from codex_image.file_permissions import is_private_file_descriptor, restrict_file_descriptor
        path = self.source / "private.json"
        path.write_text("empty", encoding="utf-8")
        with path.open("r+b") as file:
            self.assertFalse(is_private_file_descriptor(file.fileno()))
            restrict_file_descriptor(file.fileno())
            self.assertTrue(is_private_file_descriptor(file.fileno()))
        assert_private_file(self, path)
        atomic_write_text(path, "replacement", mode=0o600)
        self.assertEqual(path.read_text(), "replacement")
        assert_private_file(self, path)

    def test_private_directory_protects_children_and_supports_recovery(self):
        from codex_image.file_permissions import restrict_directory
        from tests.file_security_helpers import assert_private_directory
        restrict_directory(self.source)
        assert_private_directory(self, self.source)
        child = self.source / "new.json"
        child.write_text("private")
        with child.open("rb") as source:
            # Inherited ACEs must still exclude broad groups before a later
            # explicit file ACL is applied.
            dacl = self.files.file_dacl(source.fileno())
            self.assertIn(";;;OW)", dacl)
            self.assertIn(";;;SY)", dacl)
            for broad_sid in ("WD", "BU", "AU"):
                self.assertNotIn(f";;;{broad_sid})", dacl)
        restrict_directory(self.source)
        child.unlink()

    def test_directory_and_file_handles_pin_identity(self):
        descriptor = self.files.open_nofollow(self.source, directory=True)
        try:
            self.assertEqual(os.fstat(descriptor).st_ino, self.source.stat().st_ino)
            with self.assertRaises(OSError):
                self.source.rename(self.root / "renamed")
        finally:
            os.close(descriptor)
        path = self.source / "item.json"
        path.write_text("original")
        descriptor = self.files.open_nofollow(path)
        try:
            with self.assertRaises(OSError):
                path.write_text("changed")
            with self.assertRaises(OSError):
                path.rename(path.with_suffix(".old"))
        finally:
            os.close(descriptor)
        self.assertEqual(path.read_text(), "original")
        path.rename(path.with_suffix(".old"))

    def test_scanner_reads_legacy_and_sharded_metadata(self):
        legacy = self.source / "legacy.metadata.json"
        legacy.write_text(json.dumps({"task_id": "legacy"}), encoding="utf-8")
        shard = self.source / "tasks" / "2026-10-09"
        shard.mkdir(parents=True)
        task = shard / "task.metadata.json"
        task.write_text(json.dumps({"task_id": "task"}), encoding="utf-8")
        paths, records = self._scanner()._secure_source_metadata_scan(read_records=True)
        self.assertEqual(paths, [legacy.resolve(), task.resolve()])
        self.assertEqual(records, [{"task_id": "legacy"}, {"task_id": "task"}])

    def test_scanner_rejects_junction_to_external_metadata(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "foreign.metadata.json").write_text('{"task_id":"foreign"}')
        tasks = self.source / "tasks"
        tasks.mkdir()
        self._junction(tasks / "date", outside)
        with self.assertRaisesRegex(OSError, "backup_restore_reference_scan_unavailable"):
            self._scanner()._secure_source_metadata_scan(read_records=True)
        self.assertTrue((outside / "foreign.metadata.json").exists())

    def test_scanner_rejects_changed_root_identity(self):
        scanner = self._scanner()
        self.source.rename(self.root / "original")
        self.source.mkdir()
        with self.assertRaisesRegex(OSError, "backup_restore_reference_scan_unavailable"):
            scanner._secure_source_metadata_scan(read_records=True)

    def test_scanner_fails_closed_when_native_handle_open_fails(self):
        with patch.object(self.files, "open_nofollow", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(OSError, "backup_restore_reference_scan_unavailable"):
                self._scanner()._secure_source_metadata_scan(read_records=True)

    def test_unlink_deletes_only_verified_regular_file(self):
        directory = self.source / "nested"
        directory.mkdir()
        victim, sibling = directory / "victim", directory / "sibling"
        victim.write_text("owned")
        sibling.write_text("keep")
        self.files.unlink_nofollow(self.source, victim)
        self.files.unlink_nofollow(self.source, victim)
        self.assertFalse(victim.exists())
        self.assertEqual(sibling.read_text(), "keep")

    def test_unlink_rejects_junction_parent_and_path_traversal(self):
        outside = self.root / "outside"
        outside.mkdir()
        victim = outside / "victim"
        victim.write_text("keep")
        self._junction(self.source / "link", outside)
        for candidate in (self.source / "link" / "victim", self.source / ".." / "outside" / "victim"):
            with self.subTest(path=candidate):
                with self.assertRaises(OSError):
                    self.files.unlink_nofollow(self.source, candidate)
        self.assertEqual(victim.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
