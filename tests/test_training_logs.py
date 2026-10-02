from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sorter.events import EventBus
from sorter.models import TrainingConfig
from sorter.training.manager import MAX_TRAINING_LOGS, TrainingJob, TrainingManager, training_logs


class TrainingLogTests(unittest.TestCase):
    def test_training_output_is_persisted_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "data"
            script = Path(temp) / "fake_trainer.py"
            script.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[sys.argv.index('--output_model') + 1]).write_bytes(b'checkpoint')\n"
                "print('[SETUP] fake runtime', flush=True)\n"
                "print('native warning', file=sys.stderr, flush=True)\n"
                "print('[PROGRESS] {\"event\":\"done\",\"best_val_acc\":0.5,\"env\":{\"torch_version\":\"2.13.0\"}}', flush=True)\n",
                encoding="utf-8",
            )
            job = TrainingJob(
                image_dir=Path(temp) / "images",
                output_model=Path(temp) / "model.pth",
                config=TrainingConfig(epochs=1),
            )
            with patch.dict(os.environ, {"CASESORTER_DATA_DIR": str(data_dir)}):
                manager = TrainingManager(EventBus())
                manager.spawn(job, python=sys.executable, script=script)
                self.assertTrue(manager.wait(timeout=10))
                self.assertIsNotNone(manager.log_path)
                text = manager.log_path.read_text(encoding="utf-8")
                self.assertIn("[SETUP] fake runtime", text)
                self.assertIn("[stderr] native warning", text)
                self.assertIn("# exit code: 0", text)
                self.assertEqual("2.13.0", manager.last_result()["env"]["torch_version"])

                log_dir = manager.log_path.parent
                for index in range(MAX_TRAINING_LOGS + 4):
                    (log_dir / f"training-20000101-000000-{index:06d}.log").write_text(
                        "old", encoding="utf-8"
                    )
                # A new run prunes before opening its own log.
                second = TrainingManager(EventBus())
                second.spawn(job, python=sys.executable, script=script)
                self.assertTrue(second.wait(timeout=10))
                self.assertLessEqual(len(training_logs()), MAX_TRAINING_LOGS)


if __name__ == "__main__":
    unittest.main()
