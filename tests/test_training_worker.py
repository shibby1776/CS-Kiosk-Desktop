from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as application_main
from sorter.events import EventBus
from sorter.models import TrainingConfig
from sorter.training.manager import (
    TRAINING_WORKER_NAME,
    TrainingJob,
    TrainingManager,
    build_command,
)


class TrainingWorkerTests(unittest.TestCase):
    def _job(self, root: Path) -> TrainingJob:
        return TrainingJob(
            image_dir=root / "images",
            output_model=root / "model.pth",
            config=TrainingConfig(epochs=1),
        )

    def _run_script(self, source: str) -> tuple[TrainingManager, list, list]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        script = root / "fake_trainer.py"
        script.write_text(source, encoding="utf-8")
        bus = EventBus()
        done: list[dict] = []
        failed: list[dict] = []
        bus.subscribe("training/done", done.append)
        bus.subscribe("training/failed", failed.append)
        manager = TrainingManager(bus)
        data_dir = root / "data"
        with patch.dict(os.environ, {"CASESORTER_DATA_DIR": str(data_dir)}):
            manager.spawn(self._job(root), python=sys.executable, script=script)
            self.assertTrue(manager.wait(timeout=10))
        bus.drain(100)
        return manager, done, failed

    def test_frozen_command_uses_internal_worker_not_python_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "ShibbyPrintsCaseSorter.exe"
            command = build_command(
                self._job(Path(temp)), frozen=True, executable=executable
            )
        self.assertEqual(TRAINING_WORKER_NAME, Path(command[0]).name)
        self.assertEqual("--training-worker", command[1])
        self.assertNotIn("-u", command[:3])
        self.assertFalse(any(value.endswith("train_convnext.py") for value in command))

    def test_source_command_retains_upstream_python_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "train_convnext.py"
            command = build_command(
                self._job(Path(temp)),
                frozen=False,
                python="python-test",
                script=script,
            )
        self.assertEqual(["python-test", "-u", str(script)], command[:3])

    def test_worker_mode_dispatches_before_normal_ui_startup(self) -> None:
        worker_args = ["--image_dir", "images", "--output_model", "model.pth"]
        with (
            patch.object(application_main.multiprocessing, "freeze_support") as freeze,
            patch.object(application_main, "run_training_worker", return_value=17) as run,
            patch.object(sys, "argv", ["app.exe", "--training-worker", *worker_args]),
        ):
            self.assertEqual(17, application_main.main())
        freeze.assert_called_once_with()
        run.assert_called_once_with(worker_args)

    def test_done_requires_completion_marker_and_checkpoint(self) -> None:
        manager, done, failed = self._run_script(
            "import sys\n"
            "from pathlib import Path\n"
            "Path(sys.argv[sys.argv.index('--output_model') + 1]).write_bytes(b'model')\n"
            "print('[PROGRESS] {\"event\":\"done\",\"best_val_acc\":0.75}', flush=True)\n"
        )
        self.assertEqual([], failed)
        self.assertEqual(0.75, done[0]["best_val_acc"])
        self.assertEqual(0.75, manager.last_result()["best_val_acc"])

    def test_clean_silent_exit_fails_closed(self) -> None:
        manager, done, failed = self._run_script("pass\n")
        self.assertEqual([], done)
        self.assertEqual("missing_completion_marker", failed[0]["reason"])
        self.assertIsNone(manager.last_result())

    def test_done_without_checkpoint_fails_closed(self) -> None:
        manager, done, failed = self._run_script(
            "print('[PROGRESS] {\"event\":\"done\",\"best_val_acc\":0.75}', flush=True)\n"
        )
        self.assertEqual([], done)
        self.assertEqual("missing_checkpoint", failed[0]["reason"])
        self.assertIsNone(manager.last_result())

    def test_done_without_updating_existing_checkpoint_fails_closed(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        root.joinpath("model.pth").write_bytes(b"old model")
        script = root / "fake_trainer.py"
        script.write_text(
            "print('[PROGRESS] {\"event\":\"done\",\"best_val_acc\":0.75}', flush=True)\n",
            encoding="utf-8",
        )
        bus = EventBus()
        done: list[dict] = []
        failed: list[dict] = []
        bus.subscribe("training/done", done.append)
        bus.subscribe("training/failed", failed.append)
        manager = TrainingManager(bus)
        with patch.dict(os.environ, {"CASESORTER_DATA_DIR": str(root / "data")}):
            manager.spawn(self._job(root), python=sys.executable, script=script)
            self.assertTrue(manager.wait(timeout=10))
        bus.drain(100)
        self.assertEqual([], done)
        self.assertEqual("unchanged_checkpoint", failed[0]["reason"])
        self.assertIsNone(manager.last_result())


if __name__ == "__main__":
    unittest.main()
