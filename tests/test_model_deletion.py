import tempfile
import unittest
from pathlib import Path

from sorter.db import Database
from sorter.models import Model
from sorter.repository import HeadstampRepo, ModelRepo, SettingsRepo


class ModelDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "casesorter.db")
        self.db.ensure_initialized()
        self.models = ModelRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.model = self.models.list()[0]

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_last_model_in_caliber_can_be_deleted(self) -> None:
        self.assertEqual(1, self.models.count_in_cartridge(self.model.cartridge_id))

        active_cleared = self.models.delete(self.model.id)

        self.assertFalse(active_cleared)
        self.assertIsNone(self.models.get(self.model.id))
        self.assertEqual(0, self.models.count_in_cartridge(self.model.cartridge_id))

    def test_deleting_active_last_model_returns_to_ai_config(self) -> None:
        self.settings.set_active_model_id(self.model.id)
        HeadstampRepo(self.db).add(self.model.id, "TULA")

        active_cleared = self.models.delete(self.model.id)

        self.assertTrue(active_cleared)
        self.assertIsNone(self.settings.get_active_model_id())
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM headstamps WHERE model_id = ?",
            (self.model.id,),
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_active_model_can_select_replacement_during_delete(self) -> None:
        replacement = self.models.create(
            Model(
                name="Replacement",
                cartridge_id=self.model.cartridge_id,
                model_mode="convnext_tiny",
            )
        )
        self.settings.set_active_model_id(self.model.id)

        active_cleared = self.models.delete(
            self.model.id,
            replacement_active_id=replacement.id,
        )

        self.assertFalse(active_cleared)
        self.assertEqual(replacement.id, self.settings.get_active_model_id())


if __name__ == "__main__":
    unittest.main()
