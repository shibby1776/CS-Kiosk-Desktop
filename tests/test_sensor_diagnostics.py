from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import sorter.diagnostics as diagnostics_module
from sorter.diagnostics import DiagnosticCollector
from sorter.sensor_diagnostics import SensorDiagnosticRunner
from sorter.serial_emulator import EmulatorBroker


class FeedOvertravelEmulator(EmulatorBroker):
    def _fire_response_for(self, cmd: str) -> None:
        if cmd.casefold().startswith("xf:"):
            self._dispatch("error:feed overtravel detected")
            return
        super()._fire_response_for(cmd)


class OneShotSuspectRunner(SensorDiagnosticRunner):
    def __init__(self, broker) -> None:
        super().__init__(broker)
        self._timing_decisions = 0

    def classify_classifier_timing(self, *_args, **_kwargs):
        self._timing_decisions += 1
        if self._timing_decisions == 1:
            return "stuck_active", {"decision": "initial_suspect"}
        return "compensated", {"decision": "confirmation_clear"}


class SensorDiagnosticTimingTests(unittest.TestCase):
    def test_classifier_compensation_is_a_pass(self) -> None:
        decision, metrics = SensorDiagnosticRunner.classify_classifier_timing(
            310.0,
            304.0,
            315.0,
            original_steps=60,
            shortened_steps=15,
            feed_speed=90,
        )
        self.assertEqual(decision, "compensated")
        self.assertLess(metrics["shortening_ms"], metrics["decision_threshold_ms"])

    def test_classifier_early_completion_detects_stuck_active(self) -> None:
        decision, metrics = SensorDiagnosticRunner.classify_classifier_timing(
            310.0,
            120.0,
            315.0,
            original_steps=60,
            shortened_steps=15,
            feed_speed=90,
        )
        self.assertEqual(decision, "stuck_active")
        self.assertGreaterEqual(
            metrics["shortening_ms"], metrics["decision_threshold_ms"]
        )

    def test_sorter_compensation_is_a_pass(self) -> None:
        decision, _metrics = SensorDiagnosticRunner.classify_sorter_timing(
            420.0,
            405.0,
            original_steps=20,
            shortened_steps=10,
        )
        self.assertEqual(decision, "compensated")

    def test_sorter_early_completion_detects_stuck_active(self) -> None:
        decision, metrics = SensorDiagnosticRunner.classify_sorter_timing(
            420.0,
            185.0,
            original_steps=20,
            shortened_steps=10,
        )
        self.assertEqual(decision, "stuck_active")
        self.assertGreaterEqual(
            metrics["observed_time_saved_ms"],
            metrics["decision_threshold_ms"],
        )


class SensorDiagnosticSequenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = EmulatorBroker(response_delay_s=0.003)
        self.assertTrue(self.broker.try_open())
        self.progress: list[str] = []
        self.runner = SensorDiagnosticRunner(
            self.broker, progress=self.progress.append
        )

    def tearDown(self) -> None:
        self.broker.stop()

    def test_classifier_sequence_checks_clear_state_and_restores_settings(self) -> None:
        clear_result = self.runner.run_proximity_clear_check()
        self.assertEqual(clear_result["status"], "pass")
        self.assertIn("waiting for brass", clear_result["summary"])
        preflight_commands = [
            step["command"] for step in clear_result["steps"]
        ]
        self.assertIn("stop;getconfig", preflight_commands)
        self.assertIn("sortto:0", preflight_commands)
        self.assertIn("xf:0", preflight_commands)
        self.assertIn("sortto:5", preflight_commands)

        result = self.runner.run_classifier_test()
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["settings_restored"])
        self.assertEqual(self.broker._config["FeedCycleSteps"], 60)
        self.assertEqual(self.broker._config["FeedMotorSpeed"], 90)
        commands = [step["command"] for step in result["steps"]]
        self.assertIn("stop;getconfig", commands)
        self.assertIn("feedspeed:50", commands)
        self.assertIn("feedspeed:90", commands)
        normal_cycles = [
            step for step in result["steps"]
            if step["name"].startswith("Normal classifier cycle")
        ]
        homing_probes = [
            step for step in result["steps"]
            if step["name"].startswith("Classifier homing probe")
        ]
        self.assertEqual(len(normal_cycles), 3)
        self.assertEqual(len(homing_probes), 3)

    def test_classifier_overtravel_is_reported_and_settings_are_restored(self) -> None:
        broker = FeedOvertravelEmulator(response_delay_s=0.003)
        broker.try_open()
        try:
            result = SensorDiagnosticRunner(broker).run_classifier_test()
        finally:
            broker.stop()
        self.assertEqual(result["status"], "failed")
        self.assertIn("stayed inactive", result["summary"])
        self.assertTrue(result["settings_restored"])
        self.assertEqual(broker._config["FeedCycleSteps"], 60)
        self.assertEqual(broker._config["FeedMotorSpeed"], 90)

    def test_classifier_suspect_requires_confirmation_before_failure(self) -> None:
        result = OneShotSuspectRunner(self.broker).run_classifier_test()
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("did not repeat", result["summary"])
        self.assertIn("confirmation", result["metrics"])
        confirmation = [
            step for step in result["steps"]
            if step["name"] == "Classifier homing confirmation probe"
        ]
        self.assertEqual(len(confirmation), 1)
        self.assertTrue(result["settings_restored"])
        self.assertEqual(self.broker._config["FeedCycleSteps"], 60)
        self.assertEqual(self.broker._config["FeedMotorSpeed"], 90)

    def test_sorter_sequence_restores_settings(self) -> None:
        result = self.runner.run_sorter_test()
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["settings_restored"])
        self.assertEqual(self.broker._config["SortSteps"], 20)
        commands = [step["command"] for step in result["steps"]]
        self.assertIn("stop;getconfig", commands)
        self.assertIn("sortto:0", commands)
        self.assertIn("sortto:5", commands)
        self.assertIn("sortsteps:10", commands)
        self.assertIn("sortsteps:20", commands)


class SensorDiagnosticExportTests(unittest.TestCase):
    def test_export_contains_bounded_sensor_results(self) -> None:
        collector = DiagnosticCollector()
        collector.record_sensor_test(
            {
                "test": "sorter_home",
                "status": "pass",
                "summary": "Sorter home compensation observed.",
                "settings_restored": True,
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "diagnostics.zip"
            with patch.object(
                diagnostics_module.cv2, "__version__", "test", create=True
            ):
                collector.export_zip(
                    destination, camera_info={}, app_info={}
                )
            with zipfile.ZipFile(destination) as archive:
                sensor_results = json.loads(
                    archive.read("sensor_tests.json").decode("utf-8")
                )
                report = json.loads(
                    archive.read("diagnostic_report.json").decode("utf-8")
                )
        self.assertEqual(len(sensor_results), 1)
        self.assertEqual(sensor_results[0]["test"], "sorter_home")
        self.assertEqual(report["sensor_test_count"], 1)


if __name__ == "__main__":
    unittest.main()
