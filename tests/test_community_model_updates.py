import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sorter.db import Database
from sorter.model_io import (
    _extract_to,
    find_update_target,
    import_model,
    record_installed_version,
)
from sorter.models import Model
from sorter.repository import ApiModelAliasRepo, HeadstampRepo, ModelRepo, SettingsRepo


def _community_archive(
    path: Path,
    *,
    uid: str = "community-9mm",
    version: int = 1,
    name: str = "Community 9mm",
    headstamps: tuple[str, ...] = ("FC", "WIN"),
    checkpoint: bytes | None = b"CHECKPOINT-V1",
    checkpoint_name: str = "trainedmodel.zip",
) -> Path:
    manifest = {
        "CartridgeName": "9mm",
        "Headstamps": list(headstamps),
        "ExportMode": "ModelOnly",
        "ModelInfo": {
            "name": name,
            "model_mode": "convnext_tiny",
            "community_model_uid": uid,
            "model_version": version,
            "trained_image_count": 100 * version,
            "feedback_loop_enabled": True,
            "feedback_loop_upload_mode": "Instant",
        },
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        if checkpoint is not None:
            archive.writestr(f"model/{checkpoint_name}", checkpoint)
    return path


class CommunityModelUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "casesorter.db")
        self.db.ensure_initialized()
        self.models = ModelRepo(self.db)
        self.headstamps = HeadstampRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.images = self.root / "images"
        self.checkpoints = self.root / "models"

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def import_archive(self, archive: Path, **kwargs) -> tuple[int, int]:
        return import_model(
            archive,
            db=self.db,
            images_target_dir=self.images,
            models_target_dir=self.checkpoints,
            **kwargs,
        )

    def test_community_update_preserves_local_routing_and_identity(self) -> None:
        first = _community_archive(self.root / "v1.zip")
        _, model_id = self.import_archive(first)

        self.settings.set_active_model_id(model_id)
        ApiModelAliasRepo(self.db).assign("9mm", model_id, preload=True)
        fc = next(
            item for item in self.headstamps.list_for_model(model_id)
            if item.name == "FC"
        )
        self.headstamps.update_slot(fc.id, 3)
        local = self.models.get(model_id)
        local.name = "My Production Model"
        local.feedback_loop_enabled = False
        local.ai_model_config.api_key = "local-secret"
        self.models.update(local)

        second = _community_archive(
            self.root / "v2.zip",
            version=2,
            name="Publisher Name v2",
            headstamps=("FC", "WIN", "RP"),
            checkpoint=b"CHECKPOINT-V2",
        )
        _, updated_id = self.import_archive(second)

        self.assertEqual(model_id, updated_id)
        api_assignment = ApiModelAliasRepo(self.db).get("9MM")
        self.assertEqual(model_id, api_assignment.model_id)
        self.assertTrue(api_assignment.preload)
        self.assertEqual(model_id, self.settings.get_active_model_id())
        self.assertEqual(1, len([
            model for model in self.models.list()
            if model.community_model_uid == "community-9mm"
        ]))

        updated = self.models.get(model_id)
        self.assertEqual("My Production Model", updated.name)
        self.assertEqual(2, updated.model_version)
        self.assertEqual(200, updated.trained_image_count)
        self.assertFalse(updated.feedback_loop_enabled)
        self.assertEqual("local-secret", updated.ai_model_config.api_key)
        self.assertEqual(
            b"CHECKPOINT-V2",
            (self.checkpoints / f"{model_id}.pth").read_bytes(),
        )

        slots = {
            item.name: item.slot
            for item in self.headstamps.list_for_model(model_id)
        }
        self.assertEqual(3, slots["FC"])
        self.assertEqual(0, slots["WIN"])
        self.assertEqual(0, slots["RP"])

    def test_images_only_update_keeps_existing_checkpoint(self) -> None:
        _, model_id = self.import_archive(
            _community_archive(self.root / "v1.zip")
        )
        checkpoint = self.checkpoints / f"{model_id}.pth"

        self.import_archive(
            _community_archive(
                self.root / "metadata-only.zip",
                version=2,
                checkpoint=None,
            )
        )

        self.assertEqual(b"CHECKPOINT-V1", checkpoint.read_bytes())
        self.assertEqual(str(checkpoint), self.models.get(model_id).model_path)

    def test_community_download_accepts_publisher_checkpoint_filename(self) -> None:
        archive = _community_archive(
            self.root / "community-download.zip",
            checkpoint_name="training_models_24.zip",
        )

        _, model_id = self.import_archive(
            archive,
            community_download=True,
        )

        checkpoint = self.checkpoints / f"{model_id}.pth"
        self.assertEqual(b"CHECKPOINT-V1", checkpoint.read_bytes())
        self.assertEqual("CommunityManaged", self.models.get(model_id).model_type)

    def test_manual_import_still_rejects_arbitrary_checkpoint_zip(self) -> None:
        archive = _community_archive(
            self.root / "manual-import.zip",
            checkpoint_name="publisher-output.zip",
        )

        with self.assertRaisesRegex(ValueError, "unexpected model entry"):
            self.import_archive(archive)

    def test_plain_reimport_cannot_downgrade_community_ownership(self) -> None:
        archive = _community_archive(self.root / "community.zip")
        _, model_id = self.import_archive(
            archive,
            community_download=True,
        )

        self.import_archive(archive)

        self.assertEqual("CommunityManaged", self.models.get(model_id).model_type)

    def test_import_rejects_multiple_checkpoint_entries(self) -> None:
        archive = _community_archive(self.root / "multiple.zip")
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr("model/second.pth", b"SECOND")

        with self.assertRaisesRegex(ValueError, "multiple model entries"):
            self.import_archive(archive, community_download=True)

    def test_import_can_force_a_separate_copy(self) -> None:
        archive = _community_archive(self.root / "model.zip")
        _, first_id = self.import_archive(archive)
        _, second_id = self.import_archive(archive, update_existing=False)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual("Community 9mm (2)", self.models.get(second_id).name)

    def test_find_update_target_reports_installed_match(self) -> None:
        archive = _community_archive(self.root / "model.zip")
        self.assertIsNone(find_update_target(archive, db=self.db))
        _, model_id = self.import_archive(archive)

        target = find_update_target(archive, db=self.db)

        self.assertIsNotNone(target)
        self.assertEqual(model_id, target.id)

    def test_duplicate_lookup_prefers_active_model(self) -> None:
        cartridge_id = self.db.conn.execute(
            "SELECT id FROM cartridges WHERE name = '9mm'"
        ).fetchone()[0]
        first = self.models.create(Model(
            name="First", cartridge_id=cartridge_id,
            community_model_uid="duplicate-uid",
        ))
        second = self.models.create(Model(
            name="Second", cartridge_id=cartridge_id,
            community_model_uid="duplicate-uid",
        ))

        self.assertEqual(
            first.id,
            self.models.find_by_community_uid("duplicate-uid").id,
        )
        self.settings.set_active_model_id(second.id)
        self.assertEqual(
            second.id,
            self.models.find_by_community_uid("duplicate-uid").id,
        )

    def test_catalog_version_is_authoritative_after_download(self) -> None:
        _, model_id = self.import_archive(
            _community_archive(self.root / "model.zip", version=1)
        )
        record_installed_version(self.db, model_id, 4)

        self.assertEqual(4, self.models.get(model_id).model_version)

    def test_failed_extraction_does_not_overwrite_installed_checkpoint(self) -> None:
        destination = self.root / "installed.pth"
        destination.write_bytes(b"KNOWN-GOOD")

        class BrokenSource:
            def __init__(self) -> None:
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def read(self, _size: int = -1) -> bytes:
                self.read_count += 1
                if self.read_count == 1:
                    return b"PARTIAL"
                raise OSError("simulated archive read failure")

        class BrokenArchive:
            def open(self, _entry):
                return BrokenSource()

        with self.assertRaisesRegex(OSError, "simulated archive read failure"):
            _extract_to(BrokenArchive(), object(), destination)

        self.assertEqual(b"KNOWN-GOOD", destination.read_bytes())
        self.assertFalse((self.root / "installed.pth.tmp").exists())


if __name__ == "__main__":
    unittest.main()
