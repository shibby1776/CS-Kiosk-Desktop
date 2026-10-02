from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from sorter import classifier
from sorter.db import Database
from sorter.model_io import model_from_export_dict, model_to_export_dict
from sorter.models import CheckpointEnv
from sorter.repository import ModelRepo, SettingsRepo


class CheckpointProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "casesorter.db")
        self.db.ensure_initialized()

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def test_database_and_manifest_round_trip(self) -> None:
        repo = ModelRepo(self.db)
        model = repo.list()[0]
        model.checkpoint_env = CheckpointEnv(
            torch="2.13.0", torchvision="0.28.0", numpy="2.3.1"
        )
        repo.update(model)

        reloaded = repo.get(model.id)
        self.assertEqual("2.13.0", reloaded.checkpoint_env.torch)
        exported = model_to_export_dict(reloaded)
        self.assertEqual("2.13.0", exported["checkpoint_env"]["torch"])
        imported = model_from_export_dict(json.loads(json.dumps(exported)))
        self.assertEqual(reloaded.checkpoint_env, imported.checkpoint_env)

    def test_older_manifest_has_empty_provenance(self) -> None:
        model = model_from_export_dict({"name": "Legacy"})
        self.assertTrue(model.checkpoint_env.is_empty())

    def test_known_newer_checkpoint_blocks_before_sorting(self) -> None:
        repo = ModelRepo(self.db)
        model = repo.list()[0]
        checkpoint = Path(self.temp.name) / "model.pth"
        checkpoint.write_bytes(b"placeholder")
        model.model_path = str(checkpoint)
        model.checkpoint_env = CheckpointEnv(torch="2.13.0")
        repo.update(model)
        SettingsRepo(self.db).set_active_model_id(model.id)

        with patch.object(classifier.local_inference, "installed_version", return_value="2.12.0+cpu"):
            problem = classifier.checkpoint_problem(self.db)

        self.assertIn("trained with PyTorch 2.13.0", problem)
        self.assertIn("has 2.12.0+cpu", problem)

        with patch.object(
            classifier.local_inference, "installed_version", return_value="2.12.0+cpu"
        ), patch.object(classifier.local_inference, "classify") as inference:
            with self.assertRaises(classifier.NoLocalCheckpointError):
                classifier.classify_active(
                    np.zeros((8, 8, 3), dtype=np.uint8), [], {}, self.db
                )
        inference.assert_not_called()

    def test_unknown_or_newer_installed_version_does_not_false_block(self) -> None:
        model = ModelRepo(self.db).list()[0]
        model.checkpoint_env = CheckpointEnv(torch="2.13.0")
        for installed in (None, "nightly", "2.13.0", "2.14.0+cu130"):
            with self.subTest(installed=installed), patch.object(
                classifier.local_inference, "installed_version", return_value=installed
            ):
                self.assertIsNone(classifier.torch_floor_problem(model))


if __name__ == "__main__":
    unittest.main()
