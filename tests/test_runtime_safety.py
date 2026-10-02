import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from sorter import classifier
from sorter.db import Database
from sorter.repository import ModelRepo, SettingsRepo


class ClassificationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "casesorter.db")
        self.db.ensure_initialized()
        self.model = ModelRepo(self.db).list()[0]
        SettingsRepo(self.db).set_active_model_id(self.model.id)

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_selected_local_model_stays_local_when_checkpoint_is_missing(self) -> None:
        self.model.model_path = str(Path(self.temp.name) / "missing.pth")
        ModelRepo(self.db).update(self.model)
        image = np.zeros((8, 8, 3), dtype=np.uint8)

        with patch.object(classifier.api_client, "classify") as remote:
            with self.assertRaises(classifier.NoLocalCheckpointError):
                classifier.classify_active(image, ["TULA"], {}, self.db)

        remote.assert_not_called()
        self.assertTrue(classifier.uses_local_backend(self.db))

    def test_api_mode_still_uses_remote_inference(self) -> None:
        SettingsRepo(self.db).clear_active_model()
        image = np.zeros((8, 8, 3), dtype=np.uint8)

        with patch.object(
            classifier.api_client, "classify", return_value=("TULA", 0.95)
        ) as remote:
            result = classifier.classify_active(image, ["TULA"], {}, self.db)

        self.assertEqual(("TULA", 0.95), result)
        remote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
