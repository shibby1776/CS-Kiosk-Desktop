"""Image processing tab — Hough configuration + primer mask + live test.

Note: the line-scan crop strategy is currently hidden in the UI but still
present in sorter.image_proc (LineScanParams, linescan_crop). To bring it
back, restore the strategy radio + line-scan parameters section here and
uncomment the dispatch in sorter.image_proc.crop_headstamp.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import image_proc
from ..events import EventBus
from .widgets import ImagePanel, NumericField, build_button_row


class ImageProcTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        self.config = config
        self.bus = bus
        self.app = app
        ip = config.image_proc

        # ----- HoughCircles configuration ------------------------------------
        hough = ttk.LabelFrame(self, text="Configuration")
        hough.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        h = ip.get("hough", {})

        self.hough_dp = NumericField(
            hough, "Accumulator scale (dp)", from_=1, to=10, increment=0.5,
            initial=float(h.get("dp", 2.0)), is_float=True,
        )
        self.hough_min_dist = NumericField(
            hough, "Min center separation (px)", from_=1, to=4000,
            initial=int(h.get("min_dist", 500)),
        )
        self.hough_p1 = NumericField(
            hough, "Edge strength (param1)", from_=1, to=500,
            initial=int(h.get("param1", 100)),
        )
        self.hough_p2 = NumericField(
            hough, "Detection threshold (param2)", from_=1, to=500,
            initial=int(h.get("param2", 60)),
        )
        self.hough_min_r = NumericField(
            hough, "Min case radius (px)", from_=1, to=4000,
            initial=int(h.get("min_radius", 150)),
        )
        self.hough_max_r = NumericField(
            hough, "Max case radius (px)", from_=1, to=4000,
            initial=int(h.get("max_radius", 250)),
        )
        for idx, w in enumerate((
            self.hough_dp, self.hough_min_dist, self.hough_p1,
            self.hough_p2, self.hough_min_r, self.hough_max_r,
        )):
            w.grid(row=idx // 3, column=idx % 3, padx=6, pady=6, sticky=tk.W)

        # ----- Primer mask ---------------------------------------------------
        primer = ttk.LabelFrame(self, text="Primer mask")
        primer.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        self.primer_mode_var = tk.StringVar(value=ip.get("primer_mode", "hide"))
        ttk.Radiobutton(
            primer, text="None", variable=self.primer_mode_var, value="none",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        ttk.Radiobutton(
            primer, text="Keep primer area only", variable=self.primer_mode_var, value="use",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        ttk.Radiobutton(
            primer, text="Hide primer", variable=self.primer_mode_var, value="hide",
        ).pack(side=tk.LEFT, padx=8, pady=4)
        self.primer_radius = NumericField(
            primer, "Primer radius (px)", from_=1, to=240,
            initial=int(ip.get("primer_radius", 135)),
        )
        self.primer_radius.pack(side=tk.LEFT, padx=12, pady=4)

        # ----- Camera LED brightness (MSI-style direct raw control) -----------
        led_box = ttk.LabelFrame(self, text="Camera LED brightness")
        led_box.pack(side=tk.TOP, fill=tk.X, padx=8, pady=8)
        init_settings = config.serial.get("init_settings", {})
        initial_raw = max(0, min(255, int(init_settings.get("cameraledlevel", 15))))
        self._saved_led_raw = initial_raw
        self._current_led_raw = initial_raw
        self.led_value_var = tk.StringVar(value=f"{initial_raw}")
        self.led_scale = ttk.Scale(
            led_box, from_=0, to=255, orient=tk.HORIZONTAL, length=360,
            command=self._on_led_changed,
        )
        self.led_scale.bind("<ButtonRelease-1>", self._on_led_released)
        self.led_scale.set(initial_raw)
        self.led_scale.pack(side=tk.TOP, anchor=tk.W, padx=8, pady=(8, 2))
        row = ttk.Frame(led_box); row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))
        ttk.Label(row, text="Raw LED level").pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self.led_value_var, style="Accent.TLabel").pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="Direct controller value (0–255)", style="Subtle.TLabel").pack(side=tk.LEFT, padx=10)
        ttk.Button(row, text="Revert Lighting", command=self._revert_led).pack(side=tk.RIGHT, padx=4)

        # ----- Actions + previews -------------------------------------------
        build_button_row(self, [
            ("Save", self.save),
            ("Capture Image", self.test_on_frame),
        ], primary="Capture Image").pack(side=tk.TOP, anchor=tk.W, padx=8, pady=4)

        preview_row = ttk.Frame(self)
        preview_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        # Don't name these `before`/`after` — `after` is a tk.Widget method
        # used for scheduling (we rely on it for the LED debounce below).
        self.before_panel = ImagePanel(preview_row, width=320, height=240)
        self.before_panel.pack(side=tk.LEFT, padx=8)
        ttk.Label(preview_row, text="→").pack(side=tk.LEFT)
        self.after_panel = ImagePanel(preview_row, width=320, height=320)
        self.after_panel.pack(side=tk.LEFT, padx=8)

    # ----- handlers ----------------------------------------------------------

    def save(self, persist_lighting: bool = True) -> None:
        # Strategy is locked to "hough" while the line-scan UI is hidden.
        self.config.image_proc["strategy"] = "hough"
        self.config.image_proc["primer_mode"] = self.primer_mode_var.get()
        self.config.image_proc["primer_radius"] = int(self.primer_radius.get())
        self.config.image_proc["hough"] = {
            "dp": float(self.hough_dp.get()),
            "min_dist": int(self.hough_min_dist.get()),
            "param1": float(self.hough_p1.get()),
            "param2": float(self.hough_p2.get()),
            "min_radius": int(self.hough_min_r.get()),
            "max_radius": int(self.hough_max_r.get()),
        }
        # Preserve line-scan params in config even though the UI doesn't expose
        # them, so they survive a save+reload if we ever bring the UI back.
        self.config.save()
        if persist_lighting:
            # Lighting preview changes remain temporary until the explicit Save button.
            self._saved_led_raw = self._current_led_raw
            self.app.set_camera_led(self._saved_led_raw, source="image_processing", persist=True)
            self.led_value_var.set(f"{self._saved_led_raw}  [saved]")
            self.app.set_status(f"Image-processing and lighting settings saved (raw {self._saved_led_raw}).")
        else:
            self.app.set_status("Image-processing settings applied for capture; lighting remains unsaved.")

    def test_on_frame(self) -> None:
        frame = self.app.capture_frame()
        if frame is None:
            self.app.set_status("No camera frame available.")
            return
        self.save(persist_lighting=False)
        cfg = self.config.image_proc
        # Overlay the detected circle on the source preview so the operator
        # can see exactly what got picked while tuning the parameters.
        detection = image_proc.hough_detect(
            frame, image_proc.HoughParams.from_dict(cfg.get("hough", {}))
        )
        preview = image_proc.overlay_detection(frame, detection)
        if detection is None:
            self.app.set_status("No circle detected within radius bounds.")
        else:
            cx, cy, r = detection
            self.app.set_status(f"Detected circle: r={r:.0f} px at ({cx:.0f}, {cy:.0f}).")
        cropped = image_proc.crop_headstamp(frame, cfg)
        cropped = image_proc.apply_primer_mask(
            cropped, self.primer_mode_var.get(), int(self.primer_radius.get())
        )
        self.before_panel.show_bgr(preview)
        self.after_panel.show_bgr(cropped)

    # ----- Camera LED brightness: direct raw controller value ----------------

    def _on_led_changed(self, value: str) -> None:
        try:
            raw = max(0, min(255, int(round(float(value)))))
        except (TypeError, ValueError):
            return
        self._current_led_raw = raw
        suffix = "  [saved]" if raw == self._saved_led_raw else "  [unsaved]"
        self.led_value_var.set(f"{raw}{suffix}")

    def _on_led_released(self, _event=None) -> None:
        try:
            raw = max(0, min(255, int(round(float(self.led_scale.get())))))
        except (TypeError, ValueError, tk.TclError):
            return
        self._current_led_raw = raw
        self.led_value_var.set(f"{raw}" + ("  [saved]" if raw == self._saved_led_raw else "  [unsaved]"))
        self.app.set_camera_led(raw, source="image_processing")

    def _revert_led(self) -> None:
        self._current_led_raw = self._saved_led_raw
        self.led_scale.set(self._saved_led_raw)
        self.led_value_var.set(f"{self._saved_led_raw}  [saved]")
        self.app.set_camera_led(self._saved_led_raw, source="image_processing_revert")
        self.app.set_status(f"Lighting reverted to saved level {self._saved_led_raw}.")

