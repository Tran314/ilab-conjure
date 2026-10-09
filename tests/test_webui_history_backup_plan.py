from __future__ import annotations
from tests.file_security_helpers import assert_private_file, require_symlinks

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from codex_image.webui.gallery_storage import GalleryStorage
from codex_image.webui.history_backup_format import canonical_task_fingerprint
from codex_image.webui.history_backup_plan import (
    BackupExportScope,
    TaskBackupPlanner,
)
from codex_image.webui.history_query import HistoryFilter
from codex_image.webui.reference_assets import ReferenceAssetStorage
from codex_image.webui.reference_files import (
    ReferenceFileStorage,
    validate_reference_file,
)
from codex_image.webui.storage import TaskStorage


class WebUIHistoryBackupPlanTests(unittest.TestCase):
    def _storages(self, root: Path):
        task_storage = TaskStorage(
            output_root=root / "outputs",
            input_root=root / "inputs",
            source_data_root=root / "outputs" / "source-data",
        )
        gallery_storage = GalleryStorage(root / "gallery")
        reference_asset_storage = ReferenceAssetStorage(root / "reference-assets")
        reference_file_storage = ReferenceFileStorage(root / "reference-files")
        planner = TaskBackupPlanner(
            task_storage,
            gallery_storage,
            reference_asset_storage,
            reference_file_storage,
        )
        return (
            task_storage,
            gallery_storage,
            reference_asset_storage,
            reference_file_storage,
            planner,
        )

    def test_plan_task_covers_every_owned_file_with_stable_archive_names(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, gallery, assets, reference_files, planner = self._storages(root)
            task = storage.create_task("edit")
            input_path = storage.write_input(task.task_id, "portrait unsafe name.PNG", b"input", index=1)
            mask_path = storage.write_input(task.task_id, "mask.webp", b"mask", kind="mask", index=1)
            output_one = storage.write_output(task.task_id, b"output-one", "png", index=1)
            output_two = storage.write_output(task.task_id, b"output-two", "webp", index=2)
            asset = assets.create_or_touch("old portrait.JPEG", b"asset", "image/jpeg")
            gallery_item = gallery.create_item(
                "Pose",
                "portrait",
                "pose image.png",
                b"gallery",
                "image/png",
            )
            validated_file = validate_reference_file("brief.md", b"# brief", "text/markdown")
            reference_file = reference_files.create_or_touch(validated_file)
            tag = storage.history_organizer.create_tag("Client A")
            storage.history_organizer.organize(
                [task.task_id],
                favorite=True,
                add_tag_ids=[tag.tag_id],
            )
            request = {"prompt": "restore all", "private_path": str(root / "secret")}
            metadata = {
                "task_id": task.task_id,
                "created_at": "2026-08-01T10:00:00+00:00",
                "updated_at": "2026-08-01T10:01:00+00:00",
                "status": "partial_failed",
                "archived_at": "2026-08-01T11:00:00+00:00",
                "input_files": [input_path.name],
                "mask_file": mask_path.name,
                "output_files": [storage.output_file(output_one), storage.output_file(output_two)],
                "outputs": [
                    {"index": 1, "status": "completed", "file": storage.output_file(output_one)},
                    {"index": 2, "status": "completed", "file": storage.output_file(output_two)},
                    {"index": 3, "status": "failed"},
                ],
                "reference_assets": [{"id": asset["id"], "filename": "old portrait.JPEG"}],
                "gallery_refs": [{"id": gallery_item["id"], "filename": "pose image.png"}],
                "reference_files": [
                    {
                        "id": reference_file["id"],
                        "filename": "brief.md",
                        "size_bytes": reference_file["size_bytes"],
                    }
                ],
            }
            storage.write_request(task.task_id, request)
            storage.write_metadata(task.task_id, metadata)

            planned = planner.plan_task(task.task_id)

            roles = [file.entry.role for file in planned.files]
            self.assertEqual(
                roles,
                [
                    "metadata",
                    "request",
                    "organization",
                    "output",
                    "output",
                    "input",
                    "mask",
                    "reference_asset",
                    "gallery_reference",
                    "reference_file",
                ],
            )
            self.assertEqual(
                [file.entry.path for file in planned.files],
                [
                    f"tasks/{task.task_id}/source/metadata.json",
                    f"tasks/{task.task_id}/source/request.json",
                    f"tasks/{task.task_id}/source/organization.json",
                    f"tasks/{task.task_id}/outputs/output-0001.png",
                    f"tasks/{task.task_id}/outputs/output-0002.webp",
                    f"tasks/{task.task_id}/inputs/images/input-0001.png",
                    f"tasks/{task.task_id}/inputs/masks/mask-0001.webp",
                    f"tasks/{task.task_id}/inputs/images/reference_asset-0001.jpg",
                    f"tasks/{task.task_id}/inputs/images/gallery_reference-0001.png",
                    f"tasks/{task.task_id}/inputs/references/reference_file-0001.md",
                ],
            )
            organization = {
                "favorite": True,
                "tags": [{"name": "Client A"}],
            }
            organization_bytes = json.dumps(
                organization,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            expected_payloads = [
                storage.metadata_path(task.task_id).read_bytes(),
                storage.request_path(task.task_id).read_bytes(),
                organization_bytes,
                b"output-one",
                b"output-two",
                b"input",
                b"mask",
                b"asset",
                b"gallery",
                b"# brief",
            ]
            for file, payload in zip(planned.files, expected_payloads, strict=True):
                self.assertEqual(file.entry.size_bytes, len(payload))
                self.assertEqual(file.entry.sha256, hashlib.sha256(payload).hexdigest())
                self.assertEqual(
                    file.inline_bytes if file.inline_bytes is not None else file.source_path.read_bytes(),
                    payload,
                )
            self.assertEqual(planned.entry.created_at, metadata["created_at"])
            self.assertEqual(planned.entry.files, tuple(file.entry for file in planned.files))
            self.assertEqual(
                planned.entry.fingerprint,
                canonical_task_fingerprint(
                    metadata,
                    request,
                    planned.entry.files,
                    organization,
                ),
            )
            self.assertEqual(planner.current_task_fingerprint(task.task_id), planned.entry.fingerprint)
            self.assertIsNone(planner.current_task_fingerprint("missing-task"))

    def test_plan_task_warns_and_omits_missing_input_sources_without_blocking_outputs(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("edit")
            output = storage.write_output(task.task_id, b"complete-output", "png", index=1)
            storage.write_request(task.task_id, {"prompt": "keep prompt and output"})
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "output_files": [storage.output_file(output)],
                    "input_files": [f"{task.task_id}-input-01.png"],
                    "mask_file": f"{task.task_id}-mask-01.png",
                    "reference_assets": [{"id": "a" * 64}],
                    "gallery_refs": [{"id": "missing-gallery"}],
                    "reference_files": [
                        {"id": "b" * 64, "filename": "brief.md", "size_bytes": 1}
                    ],
                },
            )

            planned = planner.plan_task(task.task_id)

            self.assertEqual(planned.missing_input_files, 5)
            self.assertEqual(
                [item.entry.role for item in planned.files],
                ["metadata", "request", "organization", "output"],
            )

    def test_plan_task_rejects_missing_escaped_unowned_and_nonterminal_sources(self) -> None:
        scenarios = (
            ("missing", {"output_files": ["missing.png"]}, "backup_source_missing"),
            ("escaped", {"output_files": ["../outside.png"]}, "backup_source_path_invalid"),
            ("unowned", {"input_files": ["someone-else-input-01.png"]}, "task_input_not_owned"),
            ("running", {"status": "running"}, "task_backup_not_terminal"),
        )
        for label, patch, error in scenarios:
            with self.subTest(label=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage, _, _, _, planner = self._storages(root)
                task = storage.create_task("generate")
                metadata = {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                }
                metadata.update(patch)
                if label == "missing":
                    metadata["output_files"] = [f"{task.task_id}-missing.png"]
                storage.write_request(task.task_id, {"prompt": "test"})
                storage.write_metadata(task.task_id, metadata)

                with self.assertRaisesRegex(ValueError, error):
                    planner.plan_task(task.task_id)

    def test_plan_task_rejects_cross_task_and_content_storage_path_escape(self) -> None:
        require_symlinks(self)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, gallery, assets, reference_files, planner = self._storages(root)
            task = storage.create_task("generate")
            other = storage.create_task("generate")
            other_output = storage.write_output(other.task_id, b"other", "png", index=1)
            storage.write_request(task.task_id, {"prompt": "test"})
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "output_files": [storage.output_file(other_output)],
                },
            )
            with self.assertRaisesRegex(ValueError, "task_output_not_owned"):
                planner.plan_task(task.task_id)

            gallery_item = gallery.create_item(
                "Broken",
                "portrait",
                "original.png",
                b"gallery",
                "image/png",
            )
            outside_gallery = root / "outside-gallery.png"
            outside_gallery.write_bytes(b"outside")
            gallery_metadata = gallery.root / gallery_item["id"] / "metadata.json"
            payload = json.loads(gallery_metadata.read_text(encoding="utf-8"))
            payload["filename"] = "../../outside-gallery.png"
            gallery_metadata.write_text(json.dumps(payload), encoding="utf-8")
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "gallery_refs": [{"id": gallery_item["id"]}],
                },
            )
            with self.assertRaisesRegex(ValueError, "backup_source_path_invalid"):
                planner.plan_task(task.task_id)

            asset = assets.create_or_touch("asset.png", b"asset", "image/png")
            asset_path = assets.image_path(asset["id"])
            outside_asset = root / "outside-asset.png"
            outside_asset.write_bytes(b"asset")
            asset_path.unlink()
            asset_path.symlink_to(outside_asset)
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "reference_assets": [{"id": asset["id"]}],
                },
            )
            with self.assertRaisesRegex(ValueError, "backup_source_path_invalid"):
                planner.plan_task(task.task_id)

            validated = validate_reference_file("brief.md", b"# safe", "text/markdown")
            reference = reference_files.create_or_touch(validated)
            reference_path = reference_files.file_path(reference["id"])
            outside_reference = root / "outside-reference.bin"
            outside_reference.write_bytes(b"# safe")
            reference_path.unlink()
            reference_path.symlink_to(outside_reference)
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "reference_files": [
                        {
                            "id": reference["id"],
                            "filename": "brief.md",
                            "size_bytes": reference["size_bytes"],
                        }
                    ],
                },
            )
            with self.assertRaisesRegex(ValueError, "backup_source_path_invalid"):
                planner.plan_task(task.task_id)

    def test_plan_task_rejects_nested_sensitive_request_keys_without_false_positives(self) -> None:
        sensitive_keys = (
            "api_key",
            "api-key",
            "provider_api_key",
            "access_token",
            "refresh_token",
            "authorization",
            "proxy_authorization",
            "cookie",
            "set_cookie",
            "password",
            "client_secret",
            "secret_key",
            "bearer_token",
            "token",
            "secret",
            "APIKey",
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                },
            )
            storage.write_request(
                task.task_id,
                {
                    "max_tokens": 1024,
                    "token_usage": {"input_tokens": 12},
                    "nested": [{"safe": True}],
                },
            )
            planner.plan_task(task.task_id)

            for key in sensitive_keys:
                with self.subTest(key=key):
                    storage.write_request(
                        task.task_id,
                        {"nested": [{"credentials": {key: "DO-NOT-ECHO"}}]},
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "^request_contains_sensitive_fields$",
                    ) as caught:
                        planner.plan_task(task.task_id)
                    self.assertNotIn(key, str(caught.exception))
                    self.assertNotIn("DO-NOT-ECHO", str(caught.exception))

    def test_plan_task_rejects_nested_sensitive_metadata_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_request(task.task_id, {"prompt": "safe"})
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "nested": [{"providerCredentials": {"APIKey": "DO-NOT-ECHO"}}],
                },
            )

            with self.assertRaisesRegex(
                ValueError,
                "^metadata_contains_sensitive_fields$",
            ) as caught:
                planner.plan_task(task.task_id)
            self.assertNotIn("DO-NOT-ECHO", str(caught.exception))

    def test_plan_task_serializes_portable_organization_without_local_tag_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_request(task.task_id, {"prompt": "safe"})
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                },
            )
            tag = storage.history_organizer.create_tag("Travel")
            storage.history_organizer.organize([task.task_id], add_tag_ids=[tag.tag_id])

            planned = planner.plan_task(task.task_id)
            organization_file = next(
                item for item in planned.files if item.entry.role == "organization"
            )
            organization = json.loads(organization_file.inline_bytes)

            self.assertEqual(organization, {"favorite": False, "tags": [{"name": "Travel"}]})

    def test_metadata_json_and_digest_use_one_bounded_snapshot_under_replacement_race(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_request(task.task_id, {"prompt": "safe"})
            metadata_path = storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "marker": "first",
                },
            )
            original_raw = metadata_path.read_bytes()
            replacement_raw = json.dumps(
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                    "marker": "replacement",
                }
            ).encode("utf-8")
            original_open = Path.open
            reads = 0

            def racing_open(path: Path, *args, **kwargs):
                nonlocal reads
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == metadata_path and "r" in mode:
                    reads += 1
                    if reads == 2:
                        metadata_path.write_bytes(replacement_raw)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", autospec=True, side_effect=racing_open):
                planned = planner.plan_task(task.task_id)

            metadata_file = next(item for item in planned.files if item.entry.role == "metadata")
            self.assertEqual(reads, 1)
            self.assertEqual(metadata_file.entry.size_bytes, len(original_raw))
            self.assertEqual(metadata_file.entry.sha256, hashlib.sha256(original_raw).hexdigest())
            self.assertEqual(metadata_file.source_path, metadata_path)
            self.assertIsNone(metadata_file.inline_bytes)

    def test_request_json_and_digest_use_one_bounded_snapshot_under_replacement_race(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                },
            )
            request_path = storage.write_request(task.task_id, {"prompt": "first"})
            original_raw = request_path.read_bytes()
            replacement_raw = json.dumps({"prompt": "replacement"}).encode("utf-8")
            original_open = Path.open
            reads = 0

            def racing_open(path: Path, *args, **kwargs):
                nonlocal reads
                mode = args[0] if args else kwargs.get("mode", "r")
                if path == request_path and "r" in mode:
                    reads += 1
                    if reads == 2:
                        request_path.write_bytes(replacement_raw)
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", autospec=True, side_effect=racing_open):
                planned = planner.plan_task(task.task_id)

            request_file = next(item for item in planned.files if item.entry.role == "request")
            self.assertEqual(reads, 1)
            self.assertEqual(request_file.entry.size_bytes, len(original_raw))
            self.assertEqual(request_file.entry.sha256, hashlib.sha256(original_raw).hexdigest())
            self.assertEqual(request_file.source_path, request_path)
            self.assertIsNone(request_file.inline_bytes)

    def test_metadata_and_request_snapshots_reject_bounded_read_overflow_with_stable_codes(self) -> None:
        for role, expected_code in (
            ("metadata", "task_backup_metadata_invalid"),
            ("request", "task_backup_request_invalid"),
        ):
            with self.subTest(role=role), TemporaryDirectory() as tmp:
                root = Path(tmp)
                storage, _, _, _, planner = self._storages(root)
                task = storage.create_task("generate")
                storage.write_metadata(
                    task.task_id,
                    {
                        "task_id": task.task_id,
                        "created_at": "2026-08-01T10:00:00+00:00",
                        "status": "completed",
                    },
                )
                storage.write_request(task.task_id, {"prompt": "safe"})
                target = (
                    storage.metadata_path(task.task_id)
                    if role == "metadata"
                    else storage.request_path(task.task_id)
                )
                target.write_bytes(
                    target.read_bytes() + b" " * (512 if role == "request" else 32)
                )

                with patch(
                    "codex_image.webui.history_backup_plan._MAX_TASK_JSON_BYTES",
                    target.stat().st_size - 1,
                ):
                    with self.assertRaisesRegex(ValueError, f"^{expected_code}$"):
                        planner.plan_task(task.task_id)

    def test_plan_scope_streams_eligible_ids_and_counts_excluded_nonterminal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            for number, status in enumerate(("completed", "running", "failed", "queued")):
                task = storage.create_task("generate")
                storage.write_request(task.task_id, {"prompt": str(number)})
                storage.write_metadata(
                    task.task_id,
                    {
                        "task_id": task.task_id,
                        "created_at": f"2026-08-01T10:0{number}:00+00:00",
                        "status": status,
                    },
                )
                if number == 0:
                    first_id = task.task_id
                elif number == 1:
                    running_id = task.task_id
                elif number == 2:
                    failed_id = task.task_id
            plan_path = root / "plans" / "selected.jsonl"

            result = planner.plan_scope(
                BackupExportScope.selected([running_id, first_id, failed_id, first_id]),
                plan_path,
            )

            self.assertEqual(result.selected_count, 3)
            self.assertEqual(result.eligible_count, 2)
            self.assertEqual(result.excluded_nonterminal, 1)
            self.assertEqual(result.plan_path, plan_path)
            self.assertEqual(
                plan_path.read_text(encoding="utf-8").splitlines(),
                [
                    json.dumps({"task_id": first_id}, separators=(",", ":")),
                    json.dumps({"task_id": failed_id}, separators=(",", ":")),
                ],
            )
            assert_private_file(self, plan_path)

            filtered_path = root / "plans" / "filtered.jsonl"
            with (
                patch.object(
                    storage.history_query,
                    "count_task_ids",
                    side_effect=AssertionError("separate count forbidden"),
                ),
                patch.object(
                    storage.history_query,
                    "iter_task_ids",
                    side_effect=AssertionError("terminal-only second query forbidden"),
                ),
            ):
                filtered = planner.plan_scope(
                    BackupExportScope.filtered(HistoryFilter(sort="oldest")),
                    filtered_path,
                )
            self.assertEqual(filtered.selected_count, 4)
            self.assertEqual(filtered.eligible_count, 2)
            self.assertEqual(filtered.excluded_nonterminal, 2)
            self.assertEqual(
                [json.loads(line)["task_id"] for line in filtered_path.read_text().splitlines()],
                [first_id, failed_id],
            )

    def test_plan_scope_rejects_missing_selected_id_without_final_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, _, planner = self._storages(root)
            plan_path = root / "plan.jsonl"
            with self.assertRaisesRegex(ValueError, "history_task_not_found"):
                planner.plan_scope(BackupExportScope.selected(["missing"]), plan_path)
            self.assertFalse(plan_path.exists())

    def test_summarize_scope_counts_eligible_tasks_without_creating_a_plan_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task_ids: list[str] = []
            for number, status in enumerate(("completed", "running", "failed")):
                task = storage.create_task("generate")
                task_ids.append(task.task_id)
                storage.write_request(task.task_id, {"prompt": str(number)})
                storage.write_metadata(
                    task.task_id,
                    {
                        "task_id": task.task_id,
                        "created_at": f"2026-08-01T11:0{number}:00+00:00",
                        "status": status,
                    },
                )

            selected = planner.summarize_scope(
                BackupExportScope.selected([task_ids[1], task_ids[0], task_ids[0]])
            )
            all_tasks = planner.summarize_scope(BackupExportScope.all())

            self.assertEqual(
                (selected.selected_count, selected.eligible_count, selected.excluded_nonterminal),
                (2, 1, 1),
            )
            self.assertEqual(
                (all_tasks.selected_count, all_tasks.eligible_count, all_tasks.excluded_nonterminal),
                (3, 2, 1),
            )
            self.assertEqual(list(root.rglob("*.jsonl")), [])

    def test_plan_scope_sets_private_mode_before_publish_and_cleans_replace_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage, _, _, _, planner = self._storages(root)
            task = storage.create_task("generate")
            storage.write_metadata(
                task.task_id,
                {
                    "task_id": task.task_id,
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "status": "completed",
                },
            )
            plan_path = root / "plans" / "scope.jsonl"
            real_replace = os.replace

            def checked_replace(source, target):
                assert_private_file(self, source)
                real_replace(source, target)

            with (
                patch("codex_image.webui.history_backup_plan.os.replace", side_effect=checked_replace),
                patch(
                    "codex_image.webui.history_backup_plan.os.chmod",
                    side_effect=AssertionError("post-publish chmod forbidden"),
                ),
            ):
                planner.plan_scope(BackupExportScope.all(), plan_path)
            assert_private_file(self, plan_path)

            failed_path = root / "plans" / "failed.jsonl"
            with patch(
                "codex_image.webui.history_backup_plan.os.replace",
                side_effect=OSError("publish failed"),
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    planner.plan_scope(BackupExportScope.all(), failed_path)
            self.assertFalse(failed_path.exists())
            self.assertEqual(list(failed_path.parent.glob(f".{failed_path.name}.*.tmp")), [])

    def test_backup_export_scope_rejects_mismatched_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "backup_scope_invalid"):
            BackupExportScope.selected([])
        with self.assertRaisesRegex(ValueError, "backup_scope_invalid"):
            BackupExportScope(kind="all", task_ids=("task",))
        with self.assertRaisesRegex(ValueError, "backup_scope_invalid"):
            BackupExportScope(kind="filtered", filters=None)


if __name__ == "__main__":
    unittest.main()
