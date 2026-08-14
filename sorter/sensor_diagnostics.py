"""Explicit, software-only classifier and sorter sensor diagnostics.

The CS7.2 firmware does not expose raw sensor pins. These tests therefore use
controlled movement differences to prove that the firmware's homing routines
compensate for an intentional undershoot. They exist only while hidden
diagnostics are enabled and perform no background work.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


ProgressCallback = Callable[[str], None]


class SensorTestCancelled(RuntimeError):
    pass


@dataclass
class SerialStep:
    name: str
    command: str
    outcome: str
    elapsed_ms: float
    lines: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "outcome": self.outcome,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "lines": list(self.lines),
        }


class SensorDiagnosticRunner:
    """Run one explicit diagnostic sequence against a connected broker."""

    CLASSIFIER_SLOT = 5
    COMMAND_TIMEOUT_S = 3.0
    CONFIG_TIMEOUT_S = 5.0
    RECOVERY_TIMEOUT_S = 12.0
    CLASSIFIER_CYCLE_TIMEOUT_S = 20.0
    SORT_MOVE_TIMEOUT_S = 20.0
    CLASSIFIER_TEST_SPEED = 50

    def __init__(
        self,
        broker,
        *,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.broker = broker
        self.progress = progress or (lambda _message: None)
        self._cancelled = threading.Event()
        self._restoring = threading.Event()
        self._wait_lock = threading.Lock()
        self._active_wait: threading.Event | None = None

    def cancel(self) -> None:
        """Request cancellation after the active firmware command settles.

        Waking the serial wait immediately could queue a restore command while
        the firmware is still moving and unable to read serial input. Let the
        current event complete or time out, then the normal cleanup path
        restores and verifies temporary settings.
        """
        self._cancelled.set()
        try:
            self.broker.stop_run()
        except Exception:
            pass

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise SensorTestCancelled("Sensor diagnostic cancelled.")

    def _set_keepalive_suspended(self, suspended: bool) -> None:
        method = getattr(self.broker, "set_keepalive_suspended", None)
        if callable(method):
            method(bool(suspended))

    def _progress(self, message: str) -> None:
        self.progress(message)

    def _exchange(
        self,
        name: str,
        command: str,
        *,
        completion: str,
        timeout_s: float,
        ignore_cancel: bool = False,
    ) -> tuple[SerialStep, Any | None]:
        """Send one command and wait for a distinct firmware response."""
        if not ignore_cancel:
            self._check_cancelled()
        wake = threading.Event()
        lines: list[str] = []
        payload: Any | None = None
        outcome = ""
        line_lock = threading.Lock()

        def _received(line: str) -> None:
            nonlocal outcome, payload
            text = str(line).strip()
            lower = text.casefold()
            with line_lock:
                lines.append(text)
            if "waiting for brass" in lower:
                self._progress("Classifier is waiting for brass.")
            if "error" in lower:
                outcome = "error"
                payload = text
                wake.set()
                return
            if completion == "ok" and lower == "ok":
                outcome = "ok"
                payload = text
                wake.set()
            elif completion == "done" and "done" in lower:
                outcome = "done"
                payload = text
                wake.set()
            elif completion == "waiting_or_done":
                if "waiting for brass" in lower:
                    outcome = "waiting"
                    payload = text
                    wake.set()
                elif "done" in lower:
                    outcome = "done"
                    payload = text
                    wake.set()
            elif completion == "json":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return
                if isinstance(parsed, dict):
                    outcome = "json"
                    payload = parsed
                    wake.set()

        self.broker.on_received.append(_received)
        started = time.monotonic()
        with self._wait_lock:
            self._active_wait = wake
        try:
            self.broker.send_command(command)
            signalled = wake.wait(timeout=timeout_s)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            with self._wait_lock:
                if self._active_wait is wake:
                    self._active_wait = None
            try:
                self.broker.on_received.remove(_received)
            except ValueError:
                pass
        if not ignore_cancel:
            self._check_cancelled()
        if not signalled:
            outcome = "timeout"
        with line_lock:
            captured = list(lines)
        return (
            SerialStep(
                name=name,
                command=command,
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                lines=captured,
            ),
            payload,
        )

    def _get_config(
        self,
        name: str = "Read controller configuration",
        *,
        ignore_cancel: bool = False,
        timeout_s: float | None = None,
    ) -> tuple[SerialStep, dict[str, Any]]:
        step, payload = self._exchange(
            name,
            "getconfig",
            completion="json",
            timeout_s=(
                self.CONFIG_TIMEOUT_S if timeout_s is None else float(timeout_s)
            ),
            ignore_cancel=ignore_cancel,
        )
        if step.outcome != "json" or not isinstance(payload, dict):
            raise RuntimeError("Controller did not return its configuration.")
        return step, payload

    def _stop_and_get_config(
        self, name: str
    ) -> tuple[SerialStep, dict[str, Any]]:
        """Clear a pending run and wait until firmware accepts a config request."""
        self._check_cancelled()
        started = time.monotonic()
        self.broker.send_command("stop")
        try:
            barrier, config = self._get_config(
                f"{name} idle barrier",
                timeout_s=self.RECOVERY_TIMEOUT_S,
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "Controller could not reach a known idle state. Power-cycle "
                "the controller and retry the sensor test."
            ) from exc
        return (
            SerialStep(
                name=name,
                command="stop;getconfig",
                outcome="idle",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                lines=barrier.lines,
            ),
            config,
        )

    def _set_value(
        self,
        name: str,
        key: str,
        value: int,
        *,
        ignore_cancel: bool = False,
    ) -> SerialStep:
        step, _payload = self._exchange(
            name,
            f"{key}:{int(value)}",
            completion="ok",
            timeout_s=self.COMMAND_TIMEOUT_S,
            ignore_cancel=ignore_cancel,
        )
        if step.outcome != "ok":
            raise RuntimeError(
                f"Controller did not acknowledge {key}:{int(value)}."
            )
        return step

    def _move_sorter(self, slot: int, name: str) -> tuple[SerialStep, dict[str, Any]]:
        started = time.monotonic()
        ack, _payload = self._exchange(
            f"{name} command",
            f"sortto:{int(slot)}",
            completion="ok",
            timeout_s=self.COMMAND_TIMEOUT_S,
        )
        if ack.outcome != "ok":
            raise RuntimeError(f"Sorter did not acknowledge movement to slot {slot}.")
        try:
            barrier, config = self._get_config(f"{name} completion barrier")
        except RuntimeError as exc:
            raise RuntimeError(
                f"{name} to slot {slot} did not complete before timeout."
            ) from exc
        combined = SerialStep(
            name=name,
            command=f"sortto:{int(slot)}",
            outcome="done",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            lines=ack.lines + barrier.lines,
        )
        return combined, config

    @staticmethod
    def _config_int(config: dict[str, Any], key: str) -> int:
        try:
            return int(config[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Controller configuration is missing {key}.") from exc

    @staticmethod
    def _feed_step_delay_us(speed: int) -> int:
        if speed < 1 or speed > 100:
            return 500
        return 1060 - (
            int(((float(speed - 1) / 99.0) * (1000 - 60)) + 60)
        )

    @classmethod
    def classify_classifier_timing(
        cls,
        normal_first_ms: float,
        shortened_ms: float,
        normal_last_ms: float,
        *,
        original_steps: int,
        shortened_steps: int,
        feed_speed: int,
    ) -> tuple[str, dict[str, float]]:
        baseline_ms = (float(normal_first_ms) + float(normal_last_ms)) / 2.0
        repeat_spread_ms = abs(float(normal_first_ms) - float(normal_last_ms))
        shortening_ms = baseline_ms - float(shortened_ms)
        step_difference = max(0, int(original_steps) - int(shortened_steps))
        expected_stuck_shortening_ms = (
            step_difference
            * 16
            * (cls._feed_step_delay_us(int(feed_speed)) + 3)
            / 1000.0
        )
        threshold_ms = max(
            25.0,
            expected_stuck_shortening_ms * 0.50,
            repeat_spread_ms * 2.0 + 5.0,
        )
        metrics = {
            "baseline_ms": round(baseline_ms, 3),
            "shortened_cycle_ms": round(float(shortened_ms), 3),
            "shortening_ms": round(shortening_ms, 3),
            "repeat_spread_ms": round(repeat_spread_ms, 3),
            "expected_stuck_shortening_ms": round(
                expected_stuck_shortening_ms, 3
            ),
            "decision_threshold_ms": round(threshold_ms, 3),
        }
        if shortening_ms >= threshold_ms:
            return "stuck_active", metrics
        if repeat_spread_ms > max(40.0, expected_stuck_shortening_ms * 0.75):
            return "inconclusive", metrics
        return "compensated", metrics

    @staticmethod
    def classify_sorter_timing(
        outbound_ms: float,
        return_ms: float,
        *,
        original_steps: int,
        shortened_steps: int,
    ) -> tuple[str, dict[str, float]]:
        outbound_ms = float(outbound_ms)
        return_ms = float(return_ms)
        ratio = return_ms / outbound_ms if outbound_ms > 0 else 0.0
        commanded_ratio = (
            float(shortened_steps) / float(original_steps)
            if original_steps > 0
            else 0.0
        )
        expected_saved_ms = outbound_ms * max(0.0, 1.0 - commanded_ratio)
        observed_saved_ms = outbound_ms - return_ms
        failure_threshold_ms = max(30.0, expected_saved_ms * 0.50)
        metrics = {
            "outbound_ms": round(outbound_ms, 3),
            "return_ms": round(return_ms, 3),
            "return_ratio": round(ratio, 4),
            "commanded_ratio": round(commanded_ratio, 4),
            "expected_stuck_time_saved_ms": round(expected_saved_ms, 3),
            "observed_time_saved_ms": round(observed_saved_ms, 3),
            "decision_threshold_ms": round(failure_threshold_ms, 3),
        }
        if observed_saved_ms >= failure_threshold_ms and ratio < 0.75:
            return "stuck_active", metrics
        if ratio < 0.75:
            return "inconclusive", metrics
        return "compensated", metrics

    def run_proximity_clear_check(self) -> dict[str, Any]:
        """Require an empty classifier to produce ``waiting for brass``."""
        result: dict[str, Any] = {
            "test": "classifier_proximity_clear",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "failed",
            "summary": "",
            "steps": [],
            "settings_restored": True,
        }
        self._set_keepalive_suspended(True)
        try:
            self._progress("Stopping pending motion and synchronizing controller…")
            sync_step, _config = self._stop_and_get_config(
                "Reset controller before classifier test"
            )
            result["steps"].append(sync_step.to_dict())
            self._progress("Establishing sorter home at slot 0…")
            home, _config = self._move_sorter(
                0, "Establish sorter home before classifier test"
            )
            result["steps"].append(home.to_dict())
            self._progress(
                "Completing an empty feeder cycle to establish feeder home…"
            )
            feeder_home, _payload = self._exchange(
                "Establish feeder home with completed empty cycle",
                "xf:0",
                completion="done",
                timeout_s=self.CLASSIFIER_CYCLE_TIMEOUT_S,
            )
            result["steps"].append(feeder_home.to_dict())
            if feeder_home.outcome == "error":
                if any(
                    "feed overtravel detected" in line.casefold()
                    for line in feeder_home.lines
                ):
                    result["summary"] = (
                        "Feeder could not establish home; firmware reported "
                        "feed overtravel."
                    )
                else:
                    result["summary"] = (
                        "Controller reported an error while establishing feeder home."
                    )
                return result
            if feeder_home.outcome != "done":
                result["summary"] = (
                    "Feeder did not complete its initial homing cycle. "
                    "Power-cycle the controller and retry."
                )
                return result
            self._progress("Positioning sorter at slot 5…")
            move, _config = self._move_sorter(
                self.CLASSIFIER_SLOT, "Position sorter for classifier test"
            )
            result["steps"].append(move.to_dict())
            prepare = getattr(self.broker, "prepare_sensor_diagnostic", None)
            if callable(prepare):
                prepare()
            self._progress(
                "Checking the empty classifier for a clear proximity state…"
            )
            step, _payload = self._exchange(
                "Empty classifier proximity check",
                str(self.CLASSIFIER_SLOT),
                completion="waiting_or_done",
                timeout_s=6.0,
            )
            result["steps"].append(step.to_dict())
            if step.outcome == "waiting":
                self.broker.send_command("stop")
                barrier, _config = self._get_config(
                    "Confirm empty classifier check stopped"
                )
                result["steps"].append(barrier.to_dict())
                result["status"] = "pass"
                result["summary"] = (
                    "Clear proximity state confirmed by 'waiting for brass'."
                )
            elif step.outcome == "done":
                result["summary"] = (
                    "Classifier moved while confirmed empty; proximity input "
                    "appears active or brass was still present."
                )
            elif step.outcome == "error":
                result["summary"] = (
                    "Controller reported an error during the empty classifier check."
                )
            else:
                result["summary"] = (
                    "No proximity response was received before timeout."
                )
        except SensorTestCancelled:
            result["status"] = "cancelled"
            result["summary"] = "Classifier proximity check cancelled."
        except Exception as exc:
            result["summary"] = str(exc) or exc.__class__.__name__
        finally:
            self._set_keepalive_suspended(False)
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    def run_classifier_test(self) -> dict[str, Any]:
        """Run three normal cycles, then an isolated A-B-A home compensation test."""
        result: dict[str, Any] = {
            "test": "classifier_sensors",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "failed",
            "summary": "",
            "steps": [],
            "metrics": {},
            "settings_restored": False,
        }
        original_steps: int | None = None
        original_speed: int | None = None
        shortened_steps: int | None = None
        test_speed: int | None = None
        self._set_keepalive_suspended(True)
        try:
            config_step, config = self._stop_and_get_config(
                "Reset controller before loaded classifier cycles"
            )
            result["steps"].append(config_step.to_dict())
            original_steps = self._config_int(config, "FeedCycleSteps")
            original_speed = self._config_int(config, "FeedMotorSpeed")
            if original_steps < 4:
                raise RuntimeError(
                    "FeedCycleSteps is too small for a differential homing test."
                )
            if original_speed < 1 or original_speed > 100:
                raise RuntimeError(
                    "FeedMotorSpeed must be between 1 and 100 for the homing test."
                )
            shortened_steps = max(1, original_steps // 4)
            test_speed = min(original_speed, self.CLASSIFIER_TEST_SPEED)
            result["original_settings"] = {
                "FeedCycleSteps": original_steps,
                "FeedMotorSpeed": original_speed,
            }
            result["temporary_settings"] = {
                "FeedCycleSteps": shortened_steps,
                "FeedMotorSpeed": test_speed,
            }
            move, _config = self._move_sorter(
                self.CLASSIFIER_SLOT, "Position sorter at slot 5"
            )
            result["steps"].append(move.to_dict())

            # Three normal commands exercise the proximity gate and feed-home
            # overtravel protection with real brass.
            for cycle in range(1, 4):
                self._progress(
                    f"Normal classifier cycle {cycle}/3 — ensure brass is available."
                )
                step, _payload = self._exchange(
                    f"Normal classifier cycle {cycle}",
                    str(self.CLASSIFIER_SLOT),
                    completion="done",
                    timeout_s=self.CLASSIFIER_CYCLE_TIMEOUT_S,
                )
                result["steps"].append(step.to_dict())
                if step.outcome == "error":
                    if any(
                        "feed overtravel detected" in line.casefold()
                        for line in step.lines
                    ):
                        result["summary"] = (
                            "Classifier home sensor was not detected; firmware "
                            "reported feed overtravel."
                        )
                    else:
                        result["summary"] = (
                            f"Controller error during normal cycle {cycle}."
                        )
                    return result
                if step.outcome != "done":
                    result["summary"] = (
                        f"Normal classifier cycle {cycle} did not complete."
                    )
                    return result

            if test_speed != original_speed:
                speed_setting = self._set_value(
                    "Set controlled classifier diagnostic speed",
                    "feedspeed",
                    test_speed,
                )
                result["steps"].append(speed_setting.to_dict())

            # Forced cycles bypass proximity so only feed homing affects timing.
            timings: list[float] = []
            for index, steps_value in enumerate(
                (original_steps, shortened_steps, original_steps), start=1
            ):
                setting = self._set_value(
                    f"Set classifier travel for homing probe {index}",
                    "feedsteps",
                    steps_value,
                )
                result["steps"].append(setting.to_dict())
                self._progress(f"Classifier homing probe {index}/3…")
                cycle, _payload = self._exchange(
                    f"Classifier homing probe {index}",
                    f"xf:{self.CLASSIFIER_SLOT}",
                    completion="done",
                    timeout_s=self.CLASSIFIER_CYCLE_TIMEOUT_S,
                )
                result["steps"].append(cycle.to_dict())
                if cycle.outcome == "error":
                    if any(
                        "feed overtravel detected" in line.casefold()
                        for line in cycle.lines
                    ):
                        result["summary"] = (
                            "Classifier home sensor stayed inactive; firmware "
                            "reported feed overtravel."
                        )
                    else:
                        result["summary"] = (
                            f"Controller error during homing probe {index}."
                        )
                    return result
                if cycle.outcome != "done":
                    result["summary"] = (
                        f"Classifier homing probe {index} timed out."
                    )
                    return result
                timings.append(cycle.elapsed_ms)

            decision, metrics = self.classify_classifier_timing(
                timings[0],
                timings[1],
                timings[2],
                original_steps=original_steps,
                shortened_steps=shortened_steps,
                feed_speed=test_speed,
            )
            result["metrics"] = {"initial": metrics}
            if decision == "stuck_active":
                confirmation_setting = self._set_value(
                    "Set classifier travel for confirmation probe",
                    "feedsteps",
                    shortened_steps,
                )
                result["steps"].append(confirmation_setting.to_dict())
                self._progress(
                    "Confirming the apparent stuck-active classifier result…"
                )
                confirmation, _payload = self._exchange(
                    "Classifier homing confirmation probe",
                    f"xf:{self.CLASSIFIER_SLOT}",
                    completion="done",
                    timeout_s=self.CLASSIFIER_CYCLE_TIMEOUT_S,
                )
                result["steps"].append(confirmation.to_dict())
                if confirmation.outcome == "error":
                    if any(
                        "feed overtravel detected" in line.casefold()
                        for line in confirmation.lines
                    ):
                        result["summary"] = (
                            "Classifier home sensor stayed inactive during the "
                            "confirmation probe; firmware reported feed overtravel."
                        )
                    else:
                        result["summary"] = (
                            "Controller error during classifier confirmation probe."
                        )
                    return result
                if confirmation.outcome != "done":
                    result["summary"] = (
                        "Classifier homing confirmation probe timed out."
                    )
                    return result
                confirmation_decision, confirmation_metrics = (
                    self.classify_classifier_timing(
                        timings[0],
                        confirmation.elapsed_ms,
                        timings[2],
                        original_steps=original_steps,
                        shortened_steps=shortened_steps,
                        feed_speed=test_speed,
                    )
                )
                result["metrics"]["confirmation"] = confirmation_metrics
                if confirmation_decision == "stuck_active":
                    result["summary"] = (
                        "Classifier home sensor appears stuck active: two "
                        "shortened probes completed without the expected homing "
                        "compensation."
                    )
                else:
                    result["status"] = "inconclusive"
                    result["summary"] = (
                        "The initial timing suggested a stuck-active classifier "
                        "home sensor, but the confirmation probe did not repeat "
                        "that result."
                    )
            elif decision == "inconclusive":
                result["status"] = "inconclusive"
                result["summary"] = (
                    "Classifier cycles completed, but timing variation prevented "
                    "a reliable stuck-active decision."
                )
            else:
                result["status"] = "pass"
                result["summary"] = (
                    "Proximity-controlled cycles completed and classifier home "
                    "compensation was observed."
                )
        except SensorTestCancelled:
            result["status"] = "cancelled"
            result["summary"] = "Classifier sensor test cancelled."
        except Exception as exc:
            result["summary"] = str(exc) or exc.__class__.__name__
        finally:
            if original_steps is not None and original_speed is not None:
                self._restoring.set()
                restoration_errors: list[str] = []
                try:
                    try:
                        restore_steps = self._set_value(
                            "Restore classifier travel",
                            "feedsteps",
                            original_steps,
                            ignore_cancel=True,
                        )
                        result["steps"].append(restore_steps.to_dict())
                    except Exception as exc:
                        restoration_errors.append(str(exc))
                    try:
                        restore_speed = self._set_value(
                            "Restore classifier speed",
                            "feedspeed",
                            original_speed,
                            ignore_cancel=True,
                        )
                        result["steps"].append(restore_speed.to_dict())
                    except Exception as exc:
                        restoration_errors.append(str(exc))
                    try:
                        verify_step, verify = self._get_config(
                            "Verify classifier setting restoration",
                            ignore_cancel=True,
                        )
                        result["steps"].append(verify_step.to_dict())
                        result["settings_restored"] = (
                            self._config_int(verify, "FeedCycleSteps")
                            == original_steps
                            and self._config_int(verify, "FeedMotorSpeed")
                            == original_speed
                        )
                    except Exception as exc:
                        restoration_errors.append(str(exc))
                        result["settings_restored"] = False
                    if restoration_errors:
                        result["restoration_error"] = "; ".join(
                            restoration_errors
                        )
                finally:
                    self._restoring.clear()
            else:
                # No controller value was read, so no temporary setting could
                # have been applied by this test.
                result["settings_restored"] = True
            self._set_keepalive_suspended(False)
        if not result["settings_restored"]:
            result["status"] = "failed"
            result["summary"] = (
                f"{result['summary']} Original classifier settings could not "
                "be verified; power-cycle the controller before operating."
            ).strip()
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return result

    def run_sorter_test(self) -> dict[str, Any]:
        """Move 0→5→0 with an undershot return that must use the home sensor."""
        result: dict[str, Any] = {
            "test": "sorter_home",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "failed",
            "summary": "",
            "steps": [],
            "metrics": {},
            "settings_restored": False,
        }
        original_steps: int | None = None
        shortened_steps: int | None = None
        self._set_keepalive_suspended(True)
        try:
            self._progress("Stopping pending motion and synchronizing controller…")
            config_step, config = self._stop_and_get_config(
                "Reset controller before sorter test"
            )
            result["steps"].append(config_step.to_dict())
            original_steps = self._config_int(config, "SortSteps")
            if original_steps < 4:
                raise RuntimeError(
                    "SortSteps is too small for a differential homing test."
                )
            shortened_steps = max(1, original_steps // 2)
            result["original_settings"] = {"SortSteps": original_steps}
            result["temporary_settings"] = {"SortSteps": shortened_steps}

            self._progress("Confirming sorter home at slot 0…")
            home, _config = self._move_sorter(0, "Home sorter at slot 0")
            result["steps"].append(home.to_dict())

            self._progress("Moving sorter from slot 0 to slot 5…")
            outbound, _config = self._move_sorter(5, "Move sorter to slot 5")
            result["steps"].append(outbound.to_dict())

            setting = self._set_value(
                "Set sorter undershoot",
                "sortsteps",
                shortened_steps,
            )
            result["steps"].append(setting.to_dict())
            self._progress(
                "Returning sorter to slot 0; home sensor must compensate…"
            )
            returned, _config = self._move_sorter(
                0, "Return sorter to sensor home"
            )
            result["steps"].append(returned.to_dict())

            decision, metrics = self.classify_sorter_timing(
                outbound.elapsed_ms,
                returned.elapsed_ms,
                original_steps=original_steps,
                shortened_steps=shortened_steps,
            )
            result["metrics"] = metrics
            if decision == "stuck_active":
                result["summary"] = (
                    "Sorter home sensor appears stuck active: the undershot "
                    "return completed too early to have reached physical home."
                )
            elif decision == "inconclusive":
                result["status"] = "inconclusive"
                result["summary"] = (
                    "Sorter returned a completion barrier, but timing was too "
                    "short for a reliable home-sensor pass."
                )
            else:
                result["status"] = "pass"
                result["summary"] = (
                    "Sorter completed 0 → 5 → 0 and compensated for the "
                    "intentional return undershoot."
                )
        except SensorTestCancelled:
            result["status"] = "cancelled"
            result["summary"] = "Sorter homing test cancelled."
        except Exception as exc:
            result["summary"] = str(exc) or exc.__class__.__name__
            if "configuration" in result["summary"].casefold():
                pass
            elif "slot" in result["summary"].casefold():
                result["summary"] += (
                    " The sorter may be waiting for its home sensor; power-cycle "
                    "the controller before further operation."
                )
        finally:
            if original_steps is not None:
                self._restoring.set()
                try:
                    restore = self._set_value(
                        "Restore sorter travel",
                        "sortsteps",
                        original_steps,
                        ignore_cancel=True,
                    )
                    result["steps"].append(restore.to_dict())
                    verify_step, verify = self._get_config(
                        "Verify sorter setting restoration",
                        ignore_cancel=True,
                    )
                    result["steps"].append(verify_step.to_dict())
                    result["settings_restored"] = (
                        self._config_int(verify, "SortSteps") == original_steps
                    )
                except Exception as exc:
                    result["settings_restored"] = False
                    result["restoration_error"] = str(exc)
                finally:
                    self._restoring.clear()
            else:
                # No controller value was read, so no temporary setting could
                # have been applied by this test.
                result["settings_restored"] = True
            self._set_keepalive_suspended(False)
        if not result["settings_restored"]:
            result["status"] = "failed"
            result["summary"] = (
                f"{result['summary']} Original sorter settings could not be "
                "verified; power-cycle the controller before operating."
            ).strip()
        result["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return result
