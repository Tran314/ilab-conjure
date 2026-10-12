from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from codex_image.webui.generation_presets import (
    GenerationPresetError, GenerationPresetSettings, GenerationPresetStoreError,
    generation_preset_compatibility, normalize_reference_rules,
    validate_generation_presets_payload, validate_preset_input_rules,
)
from codex_image.webui.generation_request import codex_provider_connection


def preset_payload(**changes):
    return {
        "name": "常用横版", "operation": "generate", "canonical_model_id": "gpt-image-2",
        "provider_id": "codex", "binding_id": "codex-gpt-image-2-images",
        "parameters": {"canvas.size": "1536x1024", "gpt.quality": "high", "output.count": 2},
        **changes,
    }


def preset_record(**changes):
    return {
        **preset_payload(), "id": str(uuid.uuid4()),
        "reference_rules": {"images": "preserve", "files": "preserve", "order": "preserve"},
        "request_options": {},
        "created_at": "2026-10-12T20:00:00+00:00", "updated_at": "2026-10-12T20:00:00+00:00", **changes,
    }


def catalog():
    return {"providers": [{**asdict(codex_provider_connection("images")), "available": True}]}


class GenerationPresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "generation-presets.json"
        self.store = GenerationPresetSettings(self.path)

    def test_old_configuration_without_preset_file_is_empty_and_read_only(self):
        self.assertEqual(self.store.read(), {"version": 1, "presets": []})
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.path.exists())

    def test_empty_versioned_file_roundtrip(self):
        self.store.write({"version": 1, "presets": []})
        self.assertEqual(self.store.list(), [])

    def test_create_update_delete_roundtrip(self):
        preset = self.store.create(preset_payload())
        self.assertEqual(self.store.get(preset["id"]), preset)
        self.assertEqual(preset["reference_rules"], {"images": "preserve", "files": "preserve", "order": "preserve"})
        updated = self.store.update(preset["id"], {"name": " 新名字 "})
        self.assertEqual(updated["name"], "新名字")
        self.assertEqual(updated["created_at"], preset["created_at"])
        self.store.delete(preset["id"], confirm=True)
        self.assertEqual(self.store.list(), [])

    def test_long_chinese_name_uses_character_limit_not_byte_limit(self):
        name = "高清商品海报" * 20
        self.assertEqual(len(name), 120)
        self.assertEqual(self.store.create(preset_payload(name=name))["name"], name)
        with self.assertRaises(GenerationPresetError):
            self.store.create(preset_payload(name=name + "多"))

    def test_names_normalize_nfkc_trim_and_casefold_for_create_and_rename(self):
        self.store.create(preset_payload(name="  Ｔｅｓｔ  "))
        for name in ("test", "TEST", "ＴＥＳＴ"):
            with self.subTest(name=name), self.assertRaises(GenerationPresetError) as caught:
                self.store.create(preset_payload(name=name))
            self.assertEqual(caught.exception.status_code, 409)
        second = self.store.create(preset_payload(name="other"))
        with self.assertRaises(GenerationPresetError) as caught:
            self.store.update(second["id"], {"name": "tEsT"})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.store.list()), 2)

    def test_names_normalize_composed_unicode(self):
        self.store.create(preset_payload(name="Café"))
        with self.assertRaises(GenerationPresetError):
            self.store.create(preset_payload(name="CAFE\u0301"))

    def test_name_empty_controls_and_wrong_type_rejected(self):
        for name in ("", "  ", "line\nline", "bad\x00name", 7, None):
            with self.subTest(name=name), self.assertRaises(GenerationPresetError):
                self.store.create(preset_payload(name=name))
        self.assertFalse(self.path.exists())

    def test_missing_and_immutable_fields_rejected(self):
        payload = preset_payload()
        del payload["binding_id"]
        with self.assertRaises(GenerationPresetError):
            self.store.create(payload)
        preset = self.store.create(preset_payload())
        for changes in ({}, {"id": str(uuid.uuid4())}, {"created_at": "later"}):
            with self.subTest(changes=changes), self.assertRaises(GenerationPresetError):
                self.store.update(preset["id"], changes)

    def test_unknown_provider_and_model_snapshot_remains_manageable(self):
        stale = preset_record(canonical_model_id="removed-model", provider_id="removed-provider", binding_id="removed-binding")
        self.store.replace_snapshot({"version": 1, "presets": [stale]})
        self.assertEqual(self.store.backup_snapshot()["presets"], [stale])
        renamed = self.store.update(stale["id"], {"name": "可管理"}, validator=lambda item: self.fail("Rename must not invoke compatibility validator"))
        self.assertEqual(renamed["name"], "可管理")
        self.store.delete(stale["id"], confirm=True)
        self.assertEqual(self.store.list(), [])

    def test_delete_requires_explicit_boolean_confirmation(self):
        preset = self.store.create(preset_payload())
        for confirm in (False, None, 1, "true"):
            with self.subTest(confirm=confirm), self.assertRaises(GenerationPresetError):
                self.store.delete(preset["id"], confirm=confirm)
        self.assertEqual(len(self.store.list()), 1)

    def test_not_found_does_not_write(self):
        for action in (lambda: self.store.get("missing"), lambda: self.store.update("missing", {"name": "x"}), lambda: self.store.delete("missing", confirm=True)):
            with self.assertRaises(GenerationPresetError) as caught:
                action()
            self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(self.path.exists())

    def test_corrupt_future_duplicate_and_invalid_stores_fail_closed(self):
        invalid = [b"", b"{bad", b"[]", b"\xff", b'{"version":2,"presets":[]}', b'{"version":true,"presets":[]}', b'{"version":1,"version":1,"presets":[]}', b'{"version":1,"presets":null}', b'{"version":1,"presets":[],"prompt":"private"}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                actions = (self.store.read, self.store.list, lambda: self.store.create(preset_payload()), lambda: self.store.update("missing", {"name": "x"}), lambda: self.store.delete("missing", confirm=True), lambda: self.store.write({"version": 1, "presets": []}), lambda: self.store.replace_snapshot({"version": 1, "presets": []}))
                for action in actions:
                    with self.assertRaises(GenerationPresetStoreError):
                        action()
                    self.assertEqual(self.path.read_bytes(), raw)

    def test_duplicate_id_and_normalized_name_in_snapshot_rejected(self):
        preset = preset_record(name="ABC")
        for other in ({**preset, "name": "different"}, {**preset, "id": str(uuid.uuid4()), "name": "ａｂｃ"}):
            with self.assertRaises(GenerationPresetError):
                self.store.replace_snapshot({"version": 1, "presets": [preset, other]})
        self.assertFalse(self.path.exists())

    def test_sensitive_and_unrecognized_fields_cannot_be_persisted(self):
        mutations = [
            {"prompt": "secret prompt"}, {"api_key": "sk-secret"}, {"image_inputs": ["data:image/png;base64,eA=="]},
            {"parameters": {"prompt": "private"}}, {"parameters": {"api_key": "sk-secret"}},
            {"parameters": {"canvas.size": "/home/private/image.png"}},
            {"parameters": {"canvas.size": "C:\\private\\image.png"}},
            {"parameters": {"canvas.size": "data:image/png;base64,eA=="}},
            {"parameters": {"gemini.safety_settings": {"api_key": "sk-secret"}}},
            {"reference_rules": {"images": "preserve", "path": "/home/private"}},
            {"request_options": {"instructions": "private"}}, {"request_options": {"main_model": "sk-secret"}},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(GenerationPresetError):
                self.store.create(preset_payload(**changes))
        self.assertFalse(self.path.exists())

    def test_canonical_types_values_and_object_rows_are_strict(self):
        for parameters in ({"output.count": True}, {"output.count": 0}, {"output.count": 5}, {"gpt.quality": "secret"}, {"gpt.web_search": "true"}, {"gpt.output_compression": 2.5}, {"gemini.safety_settings": {"unknown_category": "OFF"}}, {"gemini.safety_settings": {"HARM_CATEGORY_HATE_SPEECH": {"token": "x"}}}):
            with self.subTest(parameters=parameters), self.assertRaises(GenerationPresetError):
                self.store.create(preset_payload(parameters=parameters))

    def test_namespaced_main_model_is_preserved_without_accepting_paths(self):
        options = {"main_model": "vendor/model-2026.10", "prompt_fidelity": "original"}
        preset = self.store.create(preset_payload(binding_id="codex-gpt-image-2-responses", request_options=options))
        self.assertEqual(self.store.get(preset["id"])["request_options"], options)
        self.assertTrue(generation_preset_compatibility(preset, catalog())["compatible"])
        for value in ("/tmp/model", "~/model", "C:/model", "C:\\model", "vendor/../model", "../model", "vendor/./model", "vendor//model", "https://host/model", "file:///model", "vendor/model\n", "vendor/model\x00", "vendor/sk-secret"):
            with self.subTest(value=value), self.assertRaises(GenerationPresetError):
                self.store.create(preset_payload(name="bad", request_options={"main_model": value}))

    def test_record_ids_timestamps_and_identifiers_are_strict(self):
        for changes in ({"id": "../../path"}, {"created_at": "2026-10-12"}, {"provider_id": "https://example.test/?key=x"}, {"canonical_model_id": "/tmp/model"}, {"binding_id": ""}, {"operation": "unknown"}, {"reference_rules": None}):
            with self.subTest(changes=changes), self.assertRaises(GenerationPresetError):
                validate_generation_presets_payload({"version": 1, "presets": [preset_record(**changes)]})

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary(self):
        preset = self.store.create(preset_payload())
        before = self.path.read_bytes()
        with patch("codex_image.atomic_files.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(GenerationPresetError) as caught:
                self.store.update(preset["id"], {"name": "never committed"})
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not a Windows access check")
    def test_file_permissions_private(self):
        self.store.create(preset_payload())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_read_modify_write_is_serialized(self):
        def create(index):
            self.store.create(preset_payload(name=f"预设 {index}"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(create, range(32)))
        self.assertEqual(len(self.store.list()), 32)

    def test_simultaneous_duplicate_create_only_writes_one(self):
        def create(index):
            try:
                self.store.create(preset_payload(name="ABC" if index % 2 else "ａｂｃ"))
                return True
            except GenerationPresetError as exc:
                self.assertEqual(exc.status_code, 409)
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(create, range(12)))
        self.assertEqual(sum(result), 1)

    def test_store_and_input_results_do_not_alias_caller_objects(self):
        payload = preset_payload()
        preset = self.store.create(payload)
        payload["parameters"]["gpt.quality"] = "low"
        preset["parameters"]["gpt.quality"] = "medium"
        self.assertEqual(self.store.list()[0]["parameters"]["gpt.quality"], "high")

    def test_store_limit_does_not_truncate(self):
        full = {"version": 1, "presets": [preset_record(name=f"Preset {i}") for i in range(200)]}
        self.store.write(full)
        with self.assertRaises(GenerationPresetError):
            self.store.create(preset_payload(name="extra"))
        self.assertEqual(len(self.store.list()), 200)


class GenerationPresetCompatibilityTests(unittest.TestCase):
    def check(self, changes=None, *, inputs=None, available_catalog=None):
        return generation_preset_compatibility(preset_record(**(changes or {})), available_catalog or catalog(), inputs=inputs)

    def test_resolver_validates_without_transport_or_prompt_content(self):
        with patch("codex_image.generation.resolver.BindingResolver.resolve", wraps=None) as resolve:
            result = self.check(inputs={"image_count": 2, "reference_file_count": 0, "has_mask": False})
        self.assertTrue(result["compatible"])
        command = resolve.call_args.args[0]
        self.assertEqual(command.prompt, "Preset compatibility check")
        self.assertEqual(command.binding_id, "codex-gpt-image-2-images")
        self.assertEqual(len(command.image_inputs), 2)
        self.assertEqual(command.parameters["gpt.quality"], "high")

    def test_known_preset_is_compatible(self):
        self.assertTrue(self.check()["compatible"])

    def test_unknown_model_provider_binding_do_not_fallback(self):
        for field, value, code in (("canonical_model_id", "missing-model", "model_not_found"), ("provider_id", "missing-provider", "provider_not_found"), ("binding_id", "missing-binding", "binding_not_found")):
            with self.subTest(field=field):
                result = self.check({field: value})
                self.assertFalse(result["compatible"])
                self.assertEqual(result["issues"][0]["code"], code)

    def test_model_binding_mismatch_is_rejected(self):
        result = self.check({"canonical_model_id": "gpt-image-2.5-flare"})
        self.assertFalse(result["compatible"])
        self.assertEqual(result["issues"][0]["code"], "provider_model_binding_missing")

    def test_unavailable_provider_is_not_applied_but_can_be_saved(self):
        current = catalog()
        current["providers"][0]["available"] = False
        self.assertEqual(self.check(available_catalog=current)["issues"][0]["code"], "provider_unavailable")
        self.assertTrue(generation_preset_compatibility(preset_record(), current, require_available=False)["compatible"])

    def test_inapplicable_parameter_is_not_dropped(self):
        for parameters in ({"output.format": "png", "gpt.output_compression": 80}, {"canvas.resolution": "1K"}):
            with self.subTest(parameters=parameters):
                self.assertFalse(self.check({"parameters": parameters})["compatible"])

    def test_transparency_restriction_uses_resolver(self):
        result = self.check({"parameters": {"output.format": "jpeg", "gpt.background": "transparent"}})
        self.assertFalse(result["compatible"])
        self.assertEqual(result["issues"][0]["code"], "transparent_background_requires_png_or_webp")

    def test_reference_require_forbid_and_default_preserve(self):
        for kind, count_key in (("images", "image_count"), ("files", "reference_file_count")):
            for policy, count in (("require", 0), ("forbid", 1)):
                with self.subTest(kind=kind, policy=policy):
                    result = self.check({"reference_rules": {kind: policy}}, inputs={count_key: count})
                    self.assertFalse(result["compatible"])
                    self.assertIn("reference_" + kind, result["issues"][0]["code"])
        self.assertTrue(self.check(inputs={"image_count": 3})["compatible"])

    def test_file_input_not_silently_discarded_by_images_codec(self):
        self.assertFalse(self.check(inputs={"reference_file_count": 1})["compatible"])
        self.assertTrue(self.check({"binding_id": "codex-gpt-image-2-responses"}, inputs={"reference_file_count": 1})["compatible"])

    def test_edit_input_and_model_limit_are_enforced(self):
        self.assertTrue(self.check({"operation": "edit"})["compatible"])
        self.assertEqual(self.check({"operation": "edit"}, inputs={})["issues"][0]["code"], "generation_preset_edit_image_required")
        self.assertEqual(self.check(inputs={"image_count": 17})["issues"][0]["code"], "image_input_limit_exceeded")
        self.assertEqual(self.check(inputs={"has_mask": True})["issues"][0]["code"], "generation_preset_mask_image_required")

    def test_rule_shape_and_input_metadata_are_strict(self):
        for rules in ({"images": "drop"}, {"order": "reverse"}, {"files": ["require"]}, {"path": "/tmp/x"}):
            with self.subTest(rules=rules), self.assertRaises(GenerationPresetError):
                normalize_reference_rules(rules)
        for inputs in ({"image_count": True}, {"image_count": -1}, {"reference_file_count": 1001}, {"has_mask": "false"}, {"prompt": "private"}):
            with self.subTest(inputs=inputs):
                self.assertFalse(self.check(inputs=inputs)["compatible"])

    def test_model_reference_limits_independently_available_to_submission(self):
        for inputs in ({"has_mask": True, "reference_file_count": 0}, {"has_mask": False, "reference_file_count": 1}):
            with self.assertRaises(GenerationPresetError):
                validate_preset_input_rules({"canonical_model_id": "nano-banana-pro", "operation": "generate"}, image_count=1, **inputs)

    def test_auxiliary_options_roundtrip_and_binding_applicability(self):
        changes = {"binding_id": "codex-gpt-image-2-responses", "request_options": {"main_model": "gpt-6-luna", "prompt_fidelity": "original"}}
        self.assertTrue(self.check(changes)["compatible"])
        self.assertFalse(self.check({"request_options": {"main_model": "gpt-6-luna"}})["compatible"])
        self.assertTrue(self.check({"request_options": {"prompt_fidelity": "strict"}})["compatible"])
        with self.assertRaises(GenerationPresetError):
            validate_generation_presets_payload({"version": 1, "presets": [preset_record(request_options={"prompt_fidelity": "fallback"})]})


if __name__ == "__main__":
    unittest.main()
