from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any

TASK_SOURCE_DATA_SUBDIR = "tasks"


@dataclass(frozen=True)
class SourceMetadataScanner:
    source_data_root: Path
    trust_root: Path
    trust_identity: tuple[int, int]

    def _secure_source_metadata_scan(
        self,
        *,
        read_records: bool,
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        windows = os.name == "nt"
        if windows:
            from ..windows_files import open_nofollow
        elif nofollow is None:
            raise OSError("backup_restore_reference_scan_unavailable")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | (nofollow or 0)
        file_flags = os.O_RDONLY | (nofollow or 0)
        root_descriptor = -1
        paths: list[Path] = []
        records: list[dict[str, Any]] = []
        scanned: list[tuple[int, str, Path, dict[str, Any] | None]] = []

        def matching_stat(descriptor: int, expected: os.stat_result, *, directory: bool) -> None:
            actual = os.fstat(descriptor)
            expected_type = stat.S_ISDIR if directory else stat.S_ISREG
            if (
                not expected_type(actual.st_mode)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise OSError("backup_restore_reference_scan_invalid")

        def scan_metadata_file(
            parent_descriptor: int,
            entry: os.DirEntry[str],
            relative: Path,
            group: int,
        ) -> None:
            # Windows DirEntry.stat() caches a zero file ID; query the path
            # while the parent is pinned before comparing with the opened file.
            expected = (os.stat(entry.path, follow_symlinks=False) if windows
                        else entry.stat(follow_symlinks=False))
            if not stat.S_ISREG(expected.st_mode):
                raise OSError("backup_restore_reference_scan_invalid")
            descriptor = (open_nofollow(Path(entry.path)) if windows
                          else os.open(entry.name, file_flags, dir_fd=parent_descriptor))
            try:
                matching_stat(descriptor, expected, directory=False)
                path = self.trust_root / relative / entry.name
                payload: dict[str, Any] | None = None
                if read_records:
                    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                        descriptor = -1
                        raw_payload = json.load(source)
                    if not isinstance(raw_payload, dict):
                        raise OSError("backup_restore_reference_scan_invalid")
                    payload = raw_payload
                scanned.append((group, path.as_posix(), path, payload))
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

        def open_child_directory(parent_descriptor: int, entry: os.DirEntry[str]) -> int:
            expected = (os.stat(entry.path, follow_symlinks=False) if windows
                        else entry.stat(follow_symlinks=False))
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError("backup_restore_reference_scan_invalid")
            descriptor = (open_nofollow(Path(entry.path), directory=True) if windows
                          else os.open(entry.name, directory_flags, dir_fd=parent_descriptor))
            try:
                matching_stat(descriptor, expected, directory=True)
            except Exception:
                os.close(descriptor)
                raise
            return descriptor

        try:
            self._assert_source_data_trust_binding()
            root_descriptor = (open_nofollow(self.trust_root, directory=True) if windows
                               else os.open(self.trust_root, directory_flags))
            root_stat = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or (root_stat.st_dev, root_stat.st_ino) != self.trust_identity
            ):
                raise OSError("backup_restore_reference_scan_invalid")
            self._assert_source_data_trust_binding()
            with os.scandir(self.trust_root if windows else root_descriptor) as root_entries:
                for entry in root_entries:
                    entry_stat = entry.stat(follow_symlinks=False)
                    if _is_link(entry_stat):
                        raise OSError("backup_restore_reference_scan_invalid")
                    if entry.name.endswith(".metadata.json"):
                        scan_metadata_file(root_descriptor, entry, Path(), 0)
                        continue
                    if entry.name != TASK_SOURCE_DATA_SUBDIR:
                        continue
                    tasks_descriptor = open_child_directory(root_descriptor, entry)
                    try:
                        with os.scandir(entry.path if windows else tasks_descriptor) as shard_entries:
                            for shard in shard_entries:
                                if _is_link(shard.stat(follow_symlinks=False)):
                                    raise OSError("backup_restore_reference_scan_invalid")
                                shard_descriptor = open_child_directory(tasks_descriptor, shard)
                                try:
                                    with os.scandir(shard.path if windows else shard_descriptor) as file_entries:
                                        for file in file_entries:
                                            if _is_link(file.stat(follow_symlinks=False)):
                                                raise OSError("backup_restore_reference_scan_invalid")
                                            if file.name.endswith(".metadata.json"):
                                                scan_metadata_file(
                                                    shard_descriptor,
                                                    file,
                                                    Path(TASK_SOURCE_DATA_SUBDIR) / shard.name,
                                                    1,
                                                )
                                finally:
                                    os.close(shard_descriptor)
                    finally:
                        os.close(tasks_descriptor)
            self._assert_source_data_trust_binding()
            seen: set[Path] = set()
            for _, _, path, payload in sorted(scanned, key=lambda item: (item[0], item[1])):
                if path in seen:
                    continue
                seen.add(path)
                paths.append(path)
                if payload is not None:
                    records.append(payload)
            return paths, records
        except (OSError, ValueError, TypeError, NotImplementedError, json.JSONDecodeError) as exc:
            raise OSError("backup_restore_reference_scan_unavailable") from exc
        finally:
            if root_descriptor >= 0:
                os.close(root_descriptor)


    def _assert_source_data_trust_binding(self) -> None:
        try:
            resolved = self.source_data_root.resolve(strict=True)
            current = resolved.stat()
        except OSError as exc:
            raise OSError("backup_restore_reference_scan_invalid") from exc
        if (
            resolved != self.trust_root
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self.trust_identity
        ):
            raise OSError("backup_restore_reference_scan_invalid")


def _is_link(info: os.stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400))
