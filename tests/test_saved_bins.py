import json
import tempfile
import unittest
from pathlib import Path

from sorter.config import Config
from sorter.db import Database
from sorter.saved_bins import (
    MAX_FILE_BYTES,
    SavedBinAssignment,
    SavedBinsProfile,
    SavedBinsService,
    SavedBinsStore,
    SavedBinsTarget,
    copy_layout_file,
)


class SavedBinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = Database(root / "casesorter.db")
        self.db.ensure_initialized()
        self.config = Config(self.db).load()
        self.store = SavedBinsStore(root / "Saved Bins")
        self.service = SavedBinsService(self.db, self.config)
        self.cart_id = self.db.conn.execute(
            "SELECT id FROM cartridges WHERE name = '9mm'"
        ).fetchone()[0]

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def add_model(self, name: str, labels: list[str]) -> int:
        model_id = self.db.conn.execute(
            "INSERT INTO models(name, cartridge_id, model_mode) VALUES (?, ?, ?)",
            (name, self.cart_id, "convnext_tiny"),
        ).lastrowid
        self.db.conn.executemany(
            "INSERT INTO headstamps(name, model_id, slot) VALUES (?, ?, 0)",
            [(label, model_id) for label in labels],
        )
        return int(model_id)

    def profile(self) -> SavedBinsProfile:
        return SavedBinsProfile(
            layout_name="9mm Test",
            caliber="9mm",
            routing_mode="headstamp",
            assignments=[
                SavedBinAssignment("TULA", (5,)),
                SavedBinAssignment("NORMA", (5,)),
                SavedBinAssignment("BLAZER", (7,)),
                SavedBinAssignment("SAR", (7,)),
                SavedBinAssignment("PMC", (4,)),
            ],
        )

    @staticmethod
    def write_profile_file(path: Path, profile: SavedBinsProfile) -> None:
        path.write_text(
            json.dumps(profile.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

    def test_reads_legacy_single_bin_assignment_format(self) -> None:
        path = self.store.ensure_directory() / "legacy-single-bin.bins.json"
        path.write_text(
            json.dumps({
                "file_type": "shibbyprints.saved_bins",
                "schema_version": 1,
                "layout_name": "Legacy",
                "caliber": "9mm",
                "assignments": [{"headstamp": "TULA", "bin": 5}],
            }),
            encoding="utf-8",
        )

        loaded = self.store.read(path)

        self.assertEqual("headstamp", loaded.routing_mode)
        self.assertEqual(("headstamp", "tula"), loaded.assignments[0].key)
        self.assertEqual((5,), loaded.assignments[0].bins)

    def test_older_model_editor_uses_union_without_mutating_layout(self) -> None:
        model_id = self.add_model("9mm Older", ["TULA", "NORMA"])
        self.db.conn.execute(
            "UPDATE headstamps SET slot = 3 WHERE model_id = ? AND name = 'TULA'",
            (model_id,),
        )
        profile = self.profile()
        before = profile.to_dict()

        rows = self.service.edit_rows(profile, model_id)
        by_name = {str(item["name"]).casefold(): item for item in rows}

        self.assertTrue(by_name["tula"]["available"])
        self.assertEqual((5,), by_name["tula"]["bins"])
        self.assertTrue(by_name["norma"]["available"])
        self.assertFalse(by_name["pmc"]["available"])
        self.assertEqual((4,), by_name["pmc"]["bins"])
        self.assertFalse(by_name["blazer"]["available"])
        self.assertEqual((7,), by_name["blazer"]["bins"])
        self.assertEqual(before, profile.to_dict())

    def test_applying_older_model_does_not_rewrite_saved_layout(self) -> None:
        model_id = self.add_model("9mm Older", ["TULA", "NORMA"])
        path = self.store.write(self.profile())
        before = path.read_bytes()

        loaded = self.store.read(path)
        result = self.service.apply(loaded, model_id, slot_count=8)

        self.assertEqual(2, result["matched_count"])
        self.assertEqual(3, result["unavailable_count"])
        self.assertEqual(before, path.read_bytes())

    def test_apply_uses_intersection_and_clears_existing_model_assignments(self) -> None:
        model_id = self.add_model(
            "9mm Older", ["TULA", "NORMA", "BLAZER", "SAR", "OLDONLY"]
        )
        self.db.conn.execute(
            "UPDATE headstamps SET slot = 2 WHERE model_id = ?",
            (model_id,),
        )
        profile = self.profile()

        result = self.service.apply(profile, model_id, slot_count=8)
        rows = self.db.conn.execute(
            "SELECT name, slot FROM headstamps WHERE model_id = ?",
            (model_id,),
        ).fetchall()
        slots = {row["name"]: row["slot"] for row in rows}

        self.assertEqual(4, result["matched_count"])
        self.assertEqual(1, result["unavailable_count"])
        self.assertEqual(5, slots["TULA"])
        self.assertEqual(5, slots["NORMA"])
        self.assertEqual(7, slots["BLAZER"])
        self.assertEqual(7, slots["SAR"])
        self.assertEqual(0, slots["OLDONLY"])
        self.assertEqual(model_id, self.service.settings.get_active_model_id())
        self.assertEqual(5, self.config.slot_for_headstamp("tula"))
        self.assertEqual(7, self.config.slot_for_headstamp("SAR"))

    def test_newer_model_reports_new_names_without_changing_profile(self) -> None:
        model_id = self.add_model(
            "9mm Newer",
            ["TULA", "NORMA", "BLAZER", "SAR", "PMC", "AGUILA"],
        )
        profile = self.profile()

        result = self.service.analyse(profile, model_id, slot_count=8)

        self.assertEqual(5, result["matched_count"])
        self.assertEqual(["AGUILA"], result["new_names"])
        self.assertEqual(5, len(profile.assignments))

    def test_unsupported_target_bin_is_reported_and_not_applied(self) -> None:
        model_id = self.add_model("9mm", ["TULA"])
        profile = SavedBinsProfile(
            layout_name="Too Many Bins",
            caliber="9mm",
            routing_mode="headstamp",
            assignments=[SavedBinAssignment("TULA", (9,))],
        )

        result = self.service.analyse(profile, model_id, slot_count=8)

        self.assertEqual(0, result["matched_count"])
        self.assertEqual(1, result["unsupported_count"])

    def test_store_round_trip_is_deterministic(self) -> None:
        profile = self.profile()
        path = self.store.write(profile)

        loaded = self.store.read(path)

        self.assertEqual(profile.layout_name, loaded.layout_name)
        self.assertEqual(
            {item.key: item.bins for item in profile.assignments},
            {item.key: item.bins for item in loaded.assignments},
        )
        self.assertFalse(path.with_name(f"{path.name}.partial").exists())

    def test_bins_json_is_discovered_and_loadable(self) -> None:
        path = self.store.ensure_directory() / "example.bins.json"
        self.write_profile_file(path, self.profile())

        profiles, errors = self.store.scan()

        self.assertEqual([], errors)
        self.assertEqual(["example.bins.json"], [p.path.name for p in profiles])
        self.assertEqual("9mm Test", self.store.read(path).layout_name)

    def test_legacy_suffix_is_ignored_and_not_recognized(self) -> None:
        legacy = self.store.ensure_directory() / "example.shibby-bins.json"
        self.write_profile_file(legacy, self.profile())

        profiles, errors = self.store.scan()

        self.assertEqual([], profiles)
        self.assertEqual([], errors)
        self.assertTrue(legacy.exists())
        self.assertFalse((legacy.parent / "example.bins.json").exists())
        with self.assertRaisesRegex(ValueError, r"must end in \.bins\.json"):
            self.store.read(legacy)
        with self.assertRaisesRegex(ValueError, r"must end in \.bins\.json"):
            self.store.write(self.profile(), path=legacy, overwrite=True)

    def test_saving_creates_bins_json_with_partial_cleanup(self) -> None:
        profile = self.profile()
        profile.layout_name = "example"

        path = self.store.write(profile)

        self.assertEqual("example.bins.json", path.name)
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name("example.bins.json.partial").exists())

    def test_store_resolves_and_writes_usb_import_destination(self) -> None:
        destination = self.store.path_for_name("9mm Production Layout")
        self.assertEqual(
            self.store.directory / "9mm-Production-Layout.bins.json",
            destination,
        )

        usb_store = SavedBinsStore(Path(self.temp.name) / "USB")
        external = usb_store.write(self.profile())
        copy_layout_file(external, destination)

        self.assertTrue(destination.is_file())
        self.assertEqual("9mm Test", self.store.read(destination).layout_name)

    def test_usb_export_copy_preserves_local_file_and_uses_usb_root(self) -> None:
        local = self.store.write(self.profile())
        before = local.read_bytes()
        usb_root = Path(self.temp.name) / "USB"
        destination = usb_root / local.name

        copied = copy_layout_file(local, destination)

        self.assertEqual(destination, copied)
        self.assertEqual(usb_root, copied.parent)
        self.assertEqual(before, copied.read_bytes())
        self.assertEqual(before, local.read_bytes())
        self.assertFalse(copied.with_name(f"{copied.name}.partial").exists())

    def test_usb_import_copy_validates_and_preserves_external_file(self) -> None:
        usb_root = Path(self.temp.name) / "USB"
        usb_store = SavedBinsStore(usb_root)
        external = usb_store.write(self.profile())
        before = external.read_bytes()
        destination = self.store.ensure_directory() / external.name

        copy_layout_file(external, destination)

        self.assertEqual(before, external.read_bytes())
        self.assertEqual(before, destination.read_bytes())
        self.assertEqual("9mm Test", self.store.read(destination).layout_name)

    def test_usb_copy_rejects_legacy_suffix_and_unconfirmed_overwrite(self) -> None:
        local = self.store.write(self.profile())
        usb_root = Path(self.temp.name) / "USB"
        usb_root.mkdir()
        destination = usb_root / local.name
        destination.write_bytes(b"existing")

        with self.assertRaises(FileExistsError):
            copy_layout_file(local, destination)
        legacy = usb_root / "legacy.shibby-bins.json"
        legacy.write_bytes(local.read_bytes())
        with self.assertRaisesRegex(ValueError, r"must end in \.bins\.json"):
            copy_layout_file(legacy, usb_root / "imported.bins.json")
        self.assertEqual(b"existing", destination.read_bytes())

    def test_usb_scan_ignores_legacy_suffix_files(self) -> None:
        usb_store = SavedBinsStore(Path(self.temp.name) / "USB")
        usb = usb_store.ensure_directory()
        self.write_profile_file(usb / "current.bins.json", self.profile())
        self.write_profile_file(
            usb / "legacy.shibby-bins.json", self.profile()
        )

        profiles, errors = usb_store.scan()

        self.assertEqual([], errors)
        self.assertEqual(["current.bins.json"], [p.path.name for p in profiles])

    def test_valid_file_remains_visible_beside_legacy_file(self) -> None:
        folder = self.store.ensure_directory()
        self.write_profile_file(folder / "visible.bins.json", self.profile())
        self.write_profile_file(
            folder / "ignored.shibby-bins.json", self.profile()
        )

        profiles, errors = self.store.scan()

        self.assertEqual([], errors)
        self.assertEqual(["visible.bins.json"], [p.path.name for p in profiles])

    def test_filename_change_preserves_json_schema_validation(self) -> None:
        valid = self.profile().to_dict()
        invalid_cases = {
            "file_type": {**valid, "file_type": "wrong.type"},
            "schema_version": {**valid, "schema_version": 99},
            "routing_mode": {**valid, "routing_mode": "unsupported"},
            "duplicate_names": {
                **valid,
                "assignments": [
                    {"headstamp": "TULA", "bin": 5},
                    {"headstamp": "tula", "bin": 6},
                ],
            },
            "bin_limit": {
                **valid,
                "assignments": [{"headstamp": "TULA", "bin": 256}],
            },
        }
        folder = self.store.ensure_directory()
        for name, payload in invalid_cases.items():
            with self.subTest(validation=name):
                path = folder / f"{name}.bins.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.store.read(path)

    def test_filename_change_preserves_size_and_path_safety(self) -> None:
        oversized = self.store.ensure_directory() / "oversized.bins.json"
        oversized.write_bytes(b" " * (MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.store.read(oversized)

        outside = Path(self.temp.name) / "outside.bins.json"
        with self.assertRaisesRegex(ValueError, "remain in the Saved Bins folder"):
            self.store.write(self.profile(), path=outside)

    def test_included_sample_has_requested_assignments(self) -> None:
        sample = (
            Path(__file__).resolve().parents[1]
            / "Saved Bins Samples"
            / "9mm-Tula-Norma-Blazer-SAR.bins.json"
        )

        loaded = self.store.read(sample)
        assignments = {
            item.name.casefold(): item.bins for item in loaded.assignments
        }

        self.assertEqual("9mm", loaded.caliber)
        self.assertEqual((5,), assignments["tula"])
        self.assertEqual((5,), assignments["norma"])
        self.assertEqual((7,), assignments["blazer"])
        self.assertEqual((7,), assignments["sar"])

    def test_remote_profile_applies_to_live_ai_runtime(self) -> None:
        local_model = self.add_model("Local", ["LOCAL"])
        self.config.settings.set_active_model_id(local_model)
        self.config.synchronize_remote_headstamps(
            ["TULA", "NORMA", "BLAZER", "SAR", "REMOTE ONLY"]
        )
        target = SavedBinsTarget.remote("9mm", "remote-9mm")

        result = self.service.apply(self.profile(), target, slot_count=8)
        runtime = {
            item["name"]: item["slot"] for item in self.config.headstamps
        }

        self.assertEqual(4, result["matched_count"])
        self.assertIsNone(self.config.settings.get_active_model_id())
        self.assertEqual(5, runtime["TULA"])
        self.assertEqual(5, runtime["NORMA"])
        self.assertEqual(7, runtime["BLAZER"])
        self.assertEqual(7, runtime["SAR"])
        self.assertEqual(0, runtime["REMOTE ONLY"])
        self.assertEqual(5, self.config.slot_for_headstamp("tula"))

    def test_remote_sync_tracks_server_names_and_preserves_matching_slots(self) -> None:
        self.config.synchronize_remote_headstamps(["TULA", "OLD"])
        self.config.set_remote_headstamp_slots({"TULA": 5, "OLD": 2})

        result = self.config.synchronize_remote_headstamps(["Tula", "AGUILA"])
        runtime = {
            item["name"]: item["slot"] for item in self.config.remote_headstamps()
        }

        self.assertEqual({"received": 2, "added": 1, "removed": 1}, result)
        self.assertEqual(5, runtime["Tula"])
        self.assertEqual(0, runtime["AGUILA"])
        self.assertNotIn("OLD", runtime)

    def test_remote_package_profile_routes_case_insensitively(self) -> None:
        self.config.synchronize_remote_headstamps(["TULA"])
        target = SavedBinsTarget.remote("9mm", "remote-9mm")
        profile = SavedBinsProfile(
            layout_name="Remote package",
            caliber="9mm",
            routing_mode="package",
            assignments=[SavedBinAssignment("tula", (5, 6))],
        )

        self.service.apply(profile, target, slot_count=8)

        self.assertTrue(self.config.run_package_mode)
        self.assertEqual([5, 6], self.config.slots_for_headstamp_package("TuLa"))

    def test_editor_adds_new_remote_name_and_preserves_unavailable_name(self) -> None:
        self.config.synchronize_remote_headstamps(
            ["TULA", "NORMA", "BLAZER", "SAR", "AGUILA"]
        )
        target = SavedBinsTarget.remote("9mm", "remote-9mm")
        profile = self.profile()
        rows = self.service.edit_rows(profile, target)
        by_name = {str(row["name"]).casefold(): row for row in rows}

        self.assertTrue(by_name["aguila"]["available"])
        self.assertEqual((), by_name["aguila"]["bins"])
        self.assertFalse(by_name["pmc"]["available"])

        assignments = [
            SavedBinAssignment("TULA", (5,)),
            SavedBinAssignment("NORMA", (5,)),
            SavedBinAssignment("BLAZER", (7,)),
            SavedBinAssignment("SAR", (7,)),
            SavedBinAssignment("AGUILA", (6,)),
        ]
        updated = self.service.replace_target_assignments(
            profile, target, assignments, slot_count=8
        )
        saved = {item.name.casefold(): item.bins for item in updated.assignments}

        self.assertEqual((6,), saved["aguila"])
        self.assertEqual((4,), saved["pmc"])

    def test_saved_file_retains_multiple_classifications_in_one_slot(self) -> None:
        local_id = self.add_model("Local multi", ["TULA", "NORMA"])
        local_target = self.service.local_target(local_id)
        self.config.synchronize_remote_headstamps(["TULA", "NORMA"])
        remote_target = SavedBinsTarget.remote("9mm", "remote-9mm")

        for label, target in (
            ("Local", local_target),
            ("Remote", remote_target),
        ):
            with self.subTest(target=label):
                profile = SavedBinsProfile(
                    layout_name=f"{label} shared bin",
                    caliber="9mm",
                    routing_mode="headstamp",
                    assignments=[],
                )
                path = self.store.write(profile)
                self.service.replace_target_assignments(
                    profile,
                    target,
                    [
                        SavedBinAssignment("TULA", (5,)),
                        SavedBinAssignment("NORMA", (5,)),
                    ],
                    slot_count=8,
                )
                self.store.write(profile, path=path, overwrite=True)

                reloaded = self.store.read(path)
                by_name = {
                    item.name.casefold(): item.bins
                    for item in reloaded.assignments
                }
                self.assertEqual((5,), by_name["tula"])
                self.assertEqual((5,), by_name["norma"])

                self.service.apply(reloaded, target, slot_count=8)
                self.assertEqual(5, self.config.slot_for_headstamp("TULA"))
                self.assertEqual(5, self.config.slot_for_headstamp("NORMA"))

    def test_snapshot_reads_operator_assignments_from_shared_config(self) -> None:
        model_id = self.add_model("Operator assigned", ["TULA", "NORMA", "SAR"])
        self.config.settings.set_active_model_id(model_id)
        self.assertTrue(self.config.set_headstamp_slot("TULA", 5))
        self.assertTrue(self.config.set_headstamp_slot("NORMA", 5))
        self.assertTrue(self.config.set_headstamp_slot("SAR", 7))

        profile = self.service.snapshot(
            self.service.local_target(model_id), "Operator layout"
        )
        assignments = {
            item.name.casefold(): item.bins for item in profile.assignments
        }

        self.assertEqual(
            {"tula": (5,), "norma": (5,), "sar": (7,)}, assignments
        )

    def test_dialog_exposes_only_explicit_layout_file_actions(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "sorter"
            / "ui"
            / "dialog_saved_bins.py"
        ).read_text(encoding="utf-8")

        for label in (
            "Apply Layout",
            "Save Current as New",
            "Edit Layout…",
            "Refresh List",
            "Import from USB…",
            "Export to USB…",
            "Delete Layout",
        ):
            self.assertIn(f'("{label}",', source)
        self.assertNotIn('text="Update Selected"', source)
        self.assertNotIn('text="Sync Remote"', source)
        self.assertNotIn("def _update_selected", source)
        self.assertNotIn("def _sync_remote_selected", source)
        self.assertIn("actions.columnconfigure", source)
        self.assertIn("SavedBinsStore(root).scan()", source)
        self.assertIn("self.store.path_for_name(profile.layout_name)", source)
        self.assertIn("imported = self.store.read(destination)", source)
        self.assertIn("destination = root / profile.path.name", source)
        self.assertNotIn("filedialog.", source)

    def test_layout_manager_size_fits_common_scaled_display(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "sorter"
            / "ui"
            / "dialog_saved_bins.py"
        ).read_text(encoding="utf-8")

        self.assertIn("min(900, int(screen_width) - 80)", source)
        self.assertIn("min(620, int(screen_height) - 100)", source)
        self.assertIn("self.minsize(min(760, width), min(520, height))", source)

    def test_save_current_as_new_cannot_overwrite_existing_layout(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "sorter"
            / "ui"
            / "dialog_saved_bins.py"
        ).read_text(encoding="utf-8")
        save_block = source.split("def _save_current_ready", 1)[1].split(
            "def _edit_selected", 1
        )[0]

        self.assertIn("except FileExistsError", save_block)
        self.assertNotIn("overwrite=True", save_block)

if __name__ == "__main__":
    unittest.main()
