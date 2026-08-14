"""Diagnostics tab for camera and precision LED investigation."""
from __future__ import annotations

import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from ..brightness import DEFAULT_GAMMA, DEFAULT_RAW_MAX, percent_to_raw, raw_to_percent
from ..sensor_diagnostics import SensorDiagnosticRunner
from ..version import (
    DESKTOP_RELEASE_VERSION,
    INTERNAL_VERSION,
    PUBLIC_VERSION,
    RELEASE_CHANNEL,
    RELEASE_LABEL,
)
from .widgets import ImagePanel


class DiagnosticsTab(ttk.Frame):
    def __init__(self, parent, *, config, bus, app) -> None:
        super().__init__(parent)
        self.app = app
        self.bus = bus
        self.config = config
        self._led_after_id: str | None = None
        self._preview_after_id: str | None = None
        self._refresh_after_id: str | None = None
        self._sensor_runner: SensorDiagnosticRunner | None = None
        self._sensor_busy = False
        self._sensor_progress_topic = f"diagnostic/sensor_progress/{id(self)}"
        self._sensor_result_topic = f"diagnostic/sensor_result/{id(self)}"
        self._sensor_error_topic = f"diagnostic/sensor_error/{id(self)}"
        self.bus.subscribe(self._sensor_progress_topic, self._on_sensor_progress)
        self.bus.subscribe(self._sensor_result_topic, self._on_sensor_worker_done)
        self.bus.subscribe(self._sensor_error_topic, self._on_sensor_worker_error)
        self.vars = {name: tk.StringVar(value="—") for name in (
            "backend", "mode", "instances", "frames", "lum", "variation",
            "interval", "led", "response", "requested_percent", "requested_raw",
            "serial_latency", "test_percent", "test_raw",
        )}

        sensors = ttk.LabelFrame(self, text="Classifier and sorter sensor tests")
        sensors.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(
            sensors,
            text=(
                "Technician-only motion tests. Stop normal operation and keep hands "
                "clear. Each test first stops pending work and establishes a known "
                "home state. The classifier test checks the proximity gate, completes "
                "three normal cycles, and verifies classifier-home compensation at "
                "a controlled speed. The sorter test moves 0 → 5 → 0 and verifies "
                "sorter-home compensation."
            ),
            wraplength=980,
        ).pack(anchor=tk.W, padx=10, pady=(8, 4))
        sensor_buttons = ttk.Frame(sensors)
        sensor_buttons.pack(fill=tk.X, padx=10, pady=4)
        self.classifier_sensor_button = ttk.Button(
            sensor_buttons,
            text="Test Classifier Sensors",
            command=self._start_classifier_sensor_test,
        )
        self.classifier_sensor_button.pack(side=tk.LEFT)
        self.sorter_sensor_button = ttk.Button(
            sensor_buttons,
            text="Test Sorter Homing",
            command=self._start_sorter_sensor_test,
        )
        self.sorter_sensor_button.pack(side=tk.LEFT, padx=(8, 0))
        self.sensor_status_var = tk.StringVar(value="No sensor test has been run.")
        ttk.Label(
            sensors,
            textvariable=self.sensor_status_var,
            style="Accent.TLabel",
            wraplength=980,
        ).pack(anchor=tk.W, padx=10, pady=(4, 4))
        self.sensor_log = tk.Text(
            sensors, height=5, wrap=tk.WORD, state=tk.DISABLED
        )
        self.sensor_log.pack(fill=tk.X, padx=10, pady=(0, 8))

        intro = ttk.LabelFrame(self, text="Camera / LED diagnostic capture")
        intro.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(intro, text=(
            "Test the live camera and precision dimmer together. The normal control uses a "
            "gamma curve for fine low-light adjustment; Advanced Raw Override addresses the "
            "controller directly."
        ), wraplength=980).pack(anchor=tk.W, padx=10, pady=8)

        test_area = ttk.LabelFrame(self, text="Integrated camera and precision dimmer test")
        test_area.pack(fill=tk.X, padx=8, pady=8)
        self.preview = ImagePanel(test_area, width=480, height=360)
        self.preview.pack(side=tk.LEFT, padx=10, pady=10)

        controls = ttk.Frame(test_area)
        controls.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        initial_raw = max(0, min(DEFAULT_RAW_MAX, int(config.serial.get("init_settings", {}).get("cameraledlevel", 15))))
        self._saved_led_raw = initial_raw
        self._current_led_raw = initial_raw
        initial_pct = raw_to_percent(initial_raw)
        self.vars["test_percent"].set(f"{initial_pct:.1f}%")
        self.vars["test_raw"].set(str(initial_raw))

        ttk.Label(controls, text="Precision brightness").pack(anchor=tk.W)
        self.percent_scale = ttk.Scale(controls, from_=0, to=100, orient=tk.HORIZONTAL, length=400, command=self._on_percent_changed)
        self.percent_scale.set(initial_pct)
        self.percent_scale.pack(anchor=tk.W, pady=(8, 2))
        self.percent_scale.bind("<ButtonRelease-1>", self._on_percent_released)

        readout = ttk.Frame(controls)
        readout.pack(anchor=tk.W, pady=(2, 6))
        ttk.Label(readout, text="Brightness:").pack(side=tk.LEFT)
        ttk.Label(readout, textvariable=self.vars["test_percent"], style="Accent.TLabel").pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(readout, text="Mapped raw:").pack(side=tk.LEFT)
        ttk.Label(readout, textvariable=self.vars["test_raw"], style="Accent.TLabel").pack(side=tk.LEFT, padx=4)

        ttk.Label(controls, text=(
            f"Gamma {DEFAULT_GAMMA:g} mapping, protected raw ceiling {DEFAULT_RAW_MAX}. "
            "Most slider travel is devoted to low-light levels."
        ), wraplength=420, style="Subtle.TLabel").pack(anchor=tk.W)

        quick = ttk.Frame(controls)
        quick.pack(anchor=tk.W, pady=(10, 6))
        for pct in (0, 10, 20, 35, 50, 70, 100):
            ttk.Button(quick, text=f"{pct}%", width=5, command=lambda v=pct: self._set_percent(v)).pack(side=tk.LEFT, padx=(0, 4))

        advanced = ttk.LabelFrame(controls, text="Advanced Raw Override")
        advanced.pack(fill=tk.X, pady=(8, 0))
        self.raw_var = tk.IntVar(value=initial_raw)
        self.raw_spin = ttk.Spinbox(advanced, from_=0, to=255, increment=1, textvariable=self.raw_var, width=7)
        self.raw_spin.pack(side=tk.LEFT, padx=8, pady=8)
        ttk.Button(advanced, text="−1", width=4, command=lambda: self._nudge_raw(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(advanced, text="+1", width=4, command=lambda: self._nudge_raw(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(advanced, text="Send Exact Raw", command=self._send_exact_raw).pack(side=tk.LEFT, padx=8)
        ttk.Label(advanced, text="0–255; use deliberately", style="Subtle.TLabel").pack(side=tk.LEFT, padx=4)

        persistence = ttk.Frame(controls)
        persistence.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(persistence, text="Save Current Lighting", command=self._save_current_lighting).pack(side=tk.LEFT)
        ttk.Button(persistence, text="Revert to Saved", command=self._revert_saved_lighting).pack(side=tk.LEFT, padx=8)
        self.saved_var = tk.StringVar(value=f"Saved raw level: {initial_raw}")
        ttk.Label(persistence, textvariable=self.saved_var, style="Accent.TLabel").pack(side=tk.LEFT, padx=12)

        stats = ttk.LabelFrame(self, text="Live telemetry")
        stats.pack(fill=tk.X, padx=8, pady=8)
        rows = [
            ("Camera backend", "backend"), ("Negotiated mode", "mode"),
            ("Active Camera objects", "instances"), ("Frames recorded", "frames"),
            ("Current luminance", "lum"), ("Brightness variation", "variation"),
            ("Mean frame interval", "interval"), ("Requested brightness", "requested_percent"),
            ("Requested raw level", "requested_raw"), ("Last LED command", "led"),
            ("Last firmware response", "response"), ("Last serial latency", "serial_latency"),
        ]
        for idx, (label, key) in enumerate(rows):
            col = 0 if idx < 6 else 2
            row = idx if idx < 6 else idx - 6
            ttk.Label(stats, text=label).grid(row=row, column=col, sticky=tk.W, padx=8, pady=3)
            ttk.Label(stats, textvariable=self.vars[key], style="Accent.TLabel").grid(row=row, column=col+1, sticky=tk.W, padx=8, pady=3)

        log_box = ttk.LabelFrame(self, text="Recent serial traffic")
        log_box.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.log = tk.Text(log_box, height=9, wrap=tk.NONE, state=tk.DISABLED)
        self.log.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        buttons = ttk.Frame(self)
        buttons.pack(fill=tk.X, padx=8, pady=8)
        ttk.Button(buttons, text="Mark event", command=self._mark).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Export Diagnostic Report", command=self._export).pack(side=tk.RIGHT)
        self._preview_after_id = self.after(100, self._refresh_preview)
        self._refresh_after_id = self.after(500, self._refresh)

    def shutdown(self) -> None:
        """Cancel all diagnostic callbacks before the panel is destroyed."""
        self.bus.unsubscribe(
            self._sensor_progress_topic, self._on_sensor_progress
        )
        self.bus.unsubscribe(
            self._sensor_result_topic, self._on_sensor_worker_done
        )
        self.bus.unsubscribe(
            self._sensor_error_topic, self._on_sensor_worker_error
        )
        runner = self._sensor_runner
        self._sensor_runner = None
        if runner is not None:
            runner.cancel()
        for after_id in (
            self._led_after_id,
            self._preview_after_id,
            self._refresh_after_id,
        ):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._led_after_id = None
        self._preview_after_id = None
        self._refresh_after_id = None

    def _tab_alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    @property
    def sensor_test_active(self) -> bool:
        return self._sensor_busy

    def request_sensor_test_cancel(self) -> None:
        runner = self._sensor_runner
        if runner is None:
            return
        runner.cancel()
        self.sensor_status_var.set(
            "Cancellation requested; waiting for safe setting restoration…"
        )
        self._append_sensor_log(
            "Cancellation requested; waiting for the active command and "
            "setting restoration."
        )

    def _sensor_preconditions_met(self) -> bool:
        if self._sensor_busy:
            messagebox.showwarning(
                "Sensor test already running",
                "Wait for the active sensor test to finish.",
                parent=self,
            )
            return False
        broker = self.app.broker
        if broker is None or not getattr(broker, "is_connected", False):
            messagebox.showerror(
                "Controller not connected",
                "Connect the Arduino controller before running a sensor test.",
                parent=self,
            )
            return False
        controller = self.app.run_controller
        if controller is not None and controller.is_running:
            messagebox.showerror(
                "Stop normal operation",
                "Stop the current run before starting a sensor test.",
                parent=self,
            )
            return False
        return True

    def _new_sensor_runner(self) -> SensorDiagnosticRunner:
        runner = SensorDiagnosticRunner(
            self.app.broker,
            progress=lambda message: self.bus.post(
                self._sensor_progress_topic, message
            ),
        )
        self._sensor_runner = runner
        return runner

    def _set_sensor_busy(self, busy: bool) -> None:
        self._sensor_busy = bool(busy)
        state = tk.DISABLED if busy else tk.NORMAL
        self.classifier_sensor_button.configure(state=state)
        self.sorter_sensor_button.configure(state=state)

    def _on_sensor_progress(self, message: str) -> None:
        if not self._tab_alive():
            return
        self.sensor_status_var.set(str(message))
        self._append_sensor_log(str(message))

    def _append_sensor_log(self, message: str) -> None:
        if not self._tab_alive():
            return
        self.sensor_log.configure(state=tk.NORMAL)
        self.sensor_log.insert(
            tk.END, f"{datetime.now():%H:%M:%S}  {message}\n"
        )
        self.sensor_log.see(tk.END)
        self.sensor_log.configure(state=tk.DISABLED)

    def _record_sensor_result(self, result: dict) -> None:
        self.app._record_diagnostic("record_sensor_test", result)

    def _run_sensor_worker(self, phase: str, function) -> None:
        """Run exactly one requested motion test without retained callbacks."""
        result_topic = self._sensor_result_topic
        error_topic = self._sensor_error_topic

        def _run() -> None:
            try:
                self.bus.post(result_topic, (phase, function()))
            except Exception as exc:
                self.bus.post(error_topic, (phase, exc))

        threading.Thread(
            target=_run,
            name=f"SensorDiagnostic-{phase}",
            daemon=True,
        ).start()

    def _on_sensor_worker_done(self, payload) -> None:
        phase, result = payload
        if phase == "proximity":
            self._on_proximity_clear_done(result)
        else:
            self._finish_sensor_test(result)

    def _start_classifier_sensor_test(self) -> None:
        if not self._sensor_preconditions_met():
            return
        confirmed = messagebox.askyesno(
            "Classifier sensor test",
            (
                "This test moves the classifier and sorter.\n\n"
                "1. Stop the machine.\n"
                "2. Remove all cases from the classifier.\n"
                "3. Keep hands and tools clear.\n\n"
                "The software will stop pending work, establish sorter home, "
                "complete one empty feeder cycle through home, and then confirm "
                "that the empty proximity sensor reports 'waiting for brass'. "
                "Continue?"
            ),
            parent=self,
        )
        if not confirmed:
            return
        self._set_sensor_busy(True)
        self.sensor_status_var.set("Checking the empty classifier…")
        self._append_sensor_log("Classifier sensor test started.")
        runner = self._new_sensor_runner()
        self._run_sensor_worker(
            "proximity", runner.run_proximity_clear_check
        )

    def _on_proximity_clear_done(self, result: dict) -> None:
        if not self._tab_alive():
            return
        self._append_sensor_log(
            f"{str(result.get('status', 'failed')).upper()}: "
            f"{result.get('summary', '')}"
        )
        if result.get("status") != "pass":
            self._finish_sensor_test(result)
            return
        self._record_sensor_result(result)
        ready = messagebox.askokcancel(
            "Load test cases",
            (
                "The empty proximity state was confirmed.\n\n"
                "Load at least 3 cases into the classifier, keep hands clear, "
                "then press OK. The software will run three normal classifier "
                "cycles followed by three isolated homing probes at a controlled "
                "speed. An additional confirmation probe runs only if timing "
                "initially suggests a stuck-active sensor."
            ),
            parent=self,
        )
        if not ready:
            cancelled = {
                "test": "classifier_sensors",
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "status": "cancelled",
                "summary": "Cancelled before loaded-case cycles.",
                "steps": [],
                "settings_restored": True,
            }
            self._finish_sensor_test(cancelled, show_dialog=False)
            return
        runner = self._sensor_runner
        if runner is None:
            self._finish_sensor_test(
                {
                    "test": "classifier_sensors",
                    "status": "cancelled",
                    "summary": "Diagnostics were disabled before the test continued.",
                    "settings_restored": True,
                },
                show_dialog=False,
            )
            return
        self.sensor_status_var.set("Running three normal classifier cycles…")
        self._run_sensor_worker("classifier", runner.run_classifier_test)

    def _start_sorter_sensor_test(self) -> None:
        if not self._sensor_preconditions_met():
            return
        confirmed = messagebox.askyesno(
            "Sorter homing test",
            (
                "This test moves the lower sorter from slot 0 to slot 5 and back "
                "to slot 0 using an intentional return undershoot. It first stops "
                "pending work and establishes slot 0 home.\n\n"
                "Stop the machine, clear the mechanism, and keep hands and tools "
                "away. If the home sensor fails, the sorter may stop away from "
                "home and require inspection or a controller power-cycle.\n\n"
                "Continue?"
            ),
            parent=self,
        )
        if not confirmed:
            return
        self._set_sensor_busy(True)
        self.sensor_status_var.set("Starting sorter homing test…")
        self._append_sensor_log("Sorter homing test started.")
        runner = self._new_sensor_runner()
        self._run_sensor_worker("sorter", runner.run_sorter_test)

    def _finish_sensor_test(
        self, result: dict, *, show_dialog: bool = True
    ) -> None:
        if not self._tab_alive():
            return
        self._record_sensor_result(result)
        self._sensor_runner = None
        self._set_sensor_busy(False)
        status = str(result.get("status", "failed")).casefold()
        summary = str(result.get("summary", "No result was returned."))
        restored = bool(result.get("settings_restored", False))
        display = f"{status.upper()}: {summary}"
        self.sensor_status_var.set(display)
        self._append_sensor_log(display)
        if not show_dialog:
            return
        if status == "pass" and restored:
            messagebox.showinfo("Sensor test passed", summary, parent=self)
        elif status in {"inconclusive", "cancelled"} and restored:
            messagebox.showwarning(
                "Sensor test not conclusive", summary, parent=self
            )
        else:
            messagebox.showerror("Sensor test failed", summary, parent=self)

    def _on_sensor_worker_error(self, payload) -> None:
        if not self._tab_alive():
            return
        _phase, exc = payload
        self._sensor_runner = None
        self._set_sensor_busy(False)
        summary = str(exc) or exc.__class__.__name__
        self.sensor_status_var.set(f"FAILED: {summary}")
        self._append_sensor_log(f"FAILED: {summary}")
        messagebox.showerror("Sensor test failed", summary, parent=self)

    def _set_percent(self, value: float) -> None:
        self.percent_scale.set(float(value))
        self._queue_percent(float(value))

    def _on_percent_changed(self, value: str) -> None:
        try: pct = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError): return
        raw = percent_to_raw(pct)
        self.vars["test_percent"].set(f"{pct:.1f}%")
        self.vars["test_raw"].set(str(raw))
        self.raw_var.set(raw)
        if self._led_after_id is not None:
            try: self.after_cancel(self._led_after_id)
            except tk.TclError: pass
        self._led_after_id = self.after(180, lambda p=pct: self._queue_percent(p))

    def _on_percent_released(self, _event=None) -> None:
        try: pct = float(self.percent_scale.get())
        except (TypeError, ValueError, tk.TclError): return
        if self._led_after_id is not None:
            try: self.after_cancel(self._led_after_id)
            except tk.TclError: pass
            self._led_after_id = None
        self._queue_percent(pct)

    def _queue_percent(self, pct: float) -> None:
        self._led_after_id = None
        pct = max(0.0, min(100.0, float(pct)))
        raw = percent_to_raw(pct)
        self.vars["test_percent"].set(f"{pct:.1f}%")
        self.vars["test_raw"].set(str(raw))
        self.raw_var.set(raw)
        self._current_led_raw = raw
        self.app.set_camera_led(raw, percent=pct, source="diagnostic_percent")

    def _nudge_raw(self, delta: int) -> None:
        try: raw = int(self.raw_var.get()) + int(delta)
        except (TypeError, ValueError, tk.TclError): return
        self.raw_var.set(max(0, min(255, raw)))
        self._send_exact_raw()

    def _send_exact_raw(self) -> None:
        try: raw = max(0, min(255, int(self.raw_var.get())))
        except (TypeError, ValueError, tk.TclError): return
        self.raw_var.set(raw)
        self.vars["test_raw"].set(str(raw))
        self.vars["test_percent"].set("raw override")
        self._current_led_raw = raw
        self.app.set_camera_led(raw, percent=None, source="diagnostic_raw_override")

    def _save_current_lighting(self) -> None:
        raw = max(0, min(255, int(self._current_led_raw)))
        pct = raw_to_percent(raw) if raw <= DEFAULT_RAW_MAX else None
        self._saved_led_raw = raw
        self.saved_var.set(f"Saved raw level: {raw}")
        self.app.set_camera_led(raw, percent=pct, source="diagnostic", persist=True)
        self.app.set_status(f"Lighting level {raw} saved for future startups.")

    def _revert_saved_lighting(self) -> None:
        raw = self._saved_led_raw
        self._current_led_raw = raw
        self.raw_var.set(raw)
        self.vars["test_raw"].set(str(raw))
        if raw <= DEFAULT_RAW_MAX:
            pct = raw_to_percent(raw)
            self.percent_scale.set(pct)
            self.vars["test_percent"].set(f"{pct:.1f}%")
        else:
            pct = None
            self.vars["test_percent"].set("raw override")
        self.app.set_camera_led(raw, percent=pct, source="diagnostic_revert")
        self.app.set_status(f"Lighting reverted to saved level {raw}.")

    def _is_visible(self) -> bool:
        try:
            selected = self.app.notebook.select()
            return bool(selected) and self.app.notebook.tab(selected, "text") == "Diagnostics"
        except (AttributeError, tk.TclError):
            return False

    def _refresh_preview(self) -> None:
        try:
            # Avoid a second 1080p frame copy and resize while this tab is hidden.
            if self._is_visible():
                frame = self.app.camera.latest_frame()
                if frame is not None:
                    self.app.diagnostics.record_frame(frame)
                    self.preview.show_bgr(frame)
        finally:
            if self.winfo_exists():
                self._preview_after_id = self.after(250, self._refresh_preview)

    def _mark(self) -> None:
        self.app.diagnostics.record_event("user_mark", "Manual diagnostic event")
        self.app.set_status("Diagnostic marker recorded.")

    def _refresh(self) -> None:
        try:
            if not self._is_visible():
                return
            info = self.app.camera.diagnostic_info(); stats = self.app.diagnostics.live_stats()
            self.vars["backend"].set(str(info.get("backend_name", "unknown")))
            mode_suffix = " (compatibility)" if info.get("compatibility_mode") else ""
            self.vars["mode"].set(f"{info.get('width', 0)}×{info.get('height', 0)} @ {info.get('fps', 0):.2f} FPS, {info.get('fourcc', '')}{mode_suffix}")
            self.vars["instances"].set(str(info.get("active_instances", "unknown")))
            self.vars["frames"].set(str(stats.get("frame_count", 0)))
            lum = stats.get("current_luminance"); self.vars["lum"].set("—" if lum is None else f"{lum:.2f}")
            lo, hi, sd = stats.get("luminance_min"), stats.get("luminance_max"), stats.get("luminance_stdev")
            self.vars["variation"].set("—" if lo is None else f"{lo:.2f}–{hi:.2f} (σ {sd:.2f})")
            interval = stats.get("mean_interval_ms"); self.vars["interval"].set("—" if interval is None else f"{interval:.2f} ms")
            pct = stats.get("requested_brightness_percent"); self.vars["requested_percent"].set("raw override" if pct is None and stats.get("requested_led_raw") is not None else ("—" if pct is None else f"{pct:.1f}%"))
            req_raw = stats.get("requested_led_raw"); self.vars["requested_raw"].set("—" if req_raw is None else str(req_raw))
            led = stats.get("last_led_level"); self.vars["led"].set("—" if led is None else f"cameraledlevel:{led}")
            self.vars["response"].set(stats.get("last_firmware_response") or "—")
            latency = stats.get("last_serial_latency_ms"); self.vars["serial_latency"].set("—" if latency is None else f"{latency:.1f} ms")
            lines = [f"{r['timestamp']} {r['direction']:<2} {r['line']}" for r in stats.get("recent_serial", [])]
            self.log.configure(state=tk.NORMAL); self.log.delete("1.0", tk.END); self.log.insert(tk.END, "\n".join(lines)); self.log.configure(state=tk.DISABLED)
        finally:
            if self.winfo_exists():
                self._refresh_after_id = self.after(500, self._refresh)

    def _export(self) -> None:
        default = f"ShibbyPrints-Diagnostics-{datetime.now():%Y-%m-%d-%H%M%S}.zip"
        path = filedialog.asksaveasfilename(parent=self, title="Export Diagnostic Report", defaultextension=".zip", initialfile=default, filetypes=[("ZIP archive", "*.zip")])
        if not path: return
        try:
            exported = self.app.diagnostics.export_zip(
                path,
                camera_info=self.app.camera.diagnostic_info(),
                app_info={
                    "build": (
                        f"{PUBLIC_VERSION}; Desktop v{DESKTOP_RELEASE_VERSION}; "
                        f"{RELEASE_LABEL}; channel={RELEASE_CHANNEL}"
                    ),
                    "public_version": PUBLIC_VERSION,
                    "desktop_release_version": DESKTOP_RELEASE_VERSION,
                    "internal_version": INTERNAL_VERSION,
                    "release_channel": RELEASE_CHANNEL,
                    "release_label": RELEASE_LABEL,
                    "config_camera": dict(self.config.camera),
                    "brightness_mapping": {
                        "mode": "direct_raw_0_255",
                        "startup_restore": True,
                    },
                },
            )
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc), parent=self); return
        messagebox.showinfo("Diagnostic report exported", f"Saved to:\n{exported}", parent=self)
        self.app.set_status(f"Diagnostic report exported: {os.path.basename(exported)}")
