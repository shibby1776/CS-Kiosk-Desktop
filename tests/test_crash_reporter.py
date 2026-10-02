from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from sorter.crash_reporter import (
    ACTIVE_SESSION_NAME,
    CrashReporter,
    LOG_NAME,
    PENDING_NAME,
)


class CrashReporterTests(unittest.TestCase):
    def test_clean_session_creates_no_worker_and_removes_active_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            before = {id(thread) for thread in threading.enumerate()}
            reporter = CrashReporter(Path(temp), app_info={"internal": "v31"})
            reporter.start()
            after = {id(thread) for thread in threading.enumerate()}
            self.assertEqual(before, after)
            self.assertTrue((Path(temp) / ACTIVE_SESSION_NAME).is_file())
            reporter.close(clean=True)
            self.assertFalse((Path(temp) / ACTIVE_SESSION_NAME).exists())
            self.assertFalse(reporter.has_pending_report)

    def test_unclean_session_is_detected_on_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = CrashReporter(directory)
            first.start()
            first.close(clean=False)

            second = CrashReporter(directory)
            second.start()
            self.assertTrue(second.pending_from_previous_session)
            self.assertTrue(second.has_pending_report)
            pending = json.loads((directory / PENDING_NAME).read_text(encoding="utf-8"))
            self.assertIn("did not shut down cleanly", pending["summary"])
            second.close(clean=True)

    def test_exception_export_is_atomic_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "logs"
            reporter = CrashReporter(
                directory,
                app_info={"internal_version": "v31"},
                max_log_bytes=4096,
            )
            reporter.start()
            reporter.register_secret("top-secret-token")
            for index in range(80):
                reporter.record_event("bounded_event", f"row={index};" + "x" * 160)
            try:
                raise RuntimeError("request failed api_key=top-secret-token")
            except RuntimeError as exc:
                reporter.record_exception(
                    type(exc), exc, exc.__traceback__, source="unit_test", notify=False
                )

            destination = Path(temp) / "support.zip"
            reporter.export_zip(
                destination,
                camera_info={"backend": "Native DirectShow"},
                config_summary={"api_key": "set"},
            )
            self.assertTrue(destination.is_file())
            self.assertFalse(Path(str(destination) + ".partial").exists())
            self.assertFalse(reporter.has_pending_report)

            with zipfile.ZipFile(destination) as archive:
                names = set(archive.namelist())
                self.assertIn("error/error_summary.json", names)
                self.assertIn("error/runtime.json", names)
                self.assertIn("error/configuration.json", names)
                combined = b"\n".join(archive.read(name) for name in names)
            self.assertNotIn(b"top-secret-token", combined)
            self.assertIn(b"[REDACTED]", combined)

            self.assertLessEqual((directory / LOG_NAME).stat().st_size, 4600)
            previous = directory / f"{LOG_NAME}.1"
            if previous.exists():
                self.assertLessEqual(previous.stat().st_size, 4600)
            reporter.close(clean=True)

    def test_crash_capture_does_not_construct_full_diagnostics(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
        enable_start = app_source.index("def _enable_diagnostics")
        collector_import = app_source.index("from ..diagnostics import DiagnosticCollector")
        self.assertGreater(collector_import, enable_start)
        self.assertIn("self.diagnostics = None", app_source)

    def test_support_export_includes_latest_bounded_training_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"CASESORTER_DATA_DIR": temp}
        ):
            data = Path(temp)
            training_dir = data / "logs"
            training_dir.mkdir(parents=True)
            (training_dir / "training-20260827-120000-000001.log").write_text(
                "[SETUP] torch=2.13.0\n# exit code: -1073741819\n",
                encoding="utf-8",
            )
            reporter = CrashReporter(data / "crash_reports")
            reporter.start()
            destination = data / "support.zip"
            reporter.export_zip(destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertIn("error/training.latest.log", archive.namelist())
                text = archive.read("error/training.latest.log").decode("utf-8")
            self.assertIn("torch=2.13.0", text)
            self.assertIn("-1073741819", text)
            reporter.close(clean=True)

    def test_atomic_zip_exports_sync_a_writable_windows_handle(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in ("sorter/crash_reporter.py", "sorter/diagnostics.py"):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertIn('partial.open("w+b")', source)
            self.assertNotIn('partial.open("rb")', source)


if __name__ == "__main__":
    unittest.main()
