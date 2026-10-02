"""AI Config tab — split layout.

Left column:  server connection settings + prompt + image encoding + Save.
Top right:    read-only server-provided headstamp list and reload.
Bottom:       single-shot test (Feed -> capture -> crop -> classify), reusing
              the same bus events the old Test tab subscribed to.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .. import api_client
from ..events import EventBus
from .widgets import ImagePanel, NumericField, build_button_row, build_labeled_entry


class AiTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, config, bus: EventBus, app):
        super().__init__(parent)
        self.config = config
        self.bus = bus
        self.app = app
        api_cfg = config.api

        # =====================================================================
        # Top half: config (left) + headstamps (right)
        # =====================================================================
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)

        # ----- LEFT: server + prompt + image ---------------------------------
        left = ttk.Frame(top)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

        conn = ttk.LabelFrame(left, text="Server")
        conn.pack(side=tk.TOP, fill=tk.X, pady=4)

        row1, self.endpoint_var = build_labeled_entry(conn, "Endpoint URL", width=40)
        self.endpoint_var.set(api_cfg.get("endpoint_url", ""))
        row1.pack(side=tk.TOP, fill=tk.X, padx=8, pady=2)

        row2, self.apikey_var = build_labeled_entry(conn, "API key", show="*", width=40)
        self.apikey_var.set(api_cfg.get("api_key", ""))
        row2.pack(side=tk.TOP, fill=tk.X, padx=8, pady=2)

        row3 = ttk.Frame(conn)
        ttk.Label(row3, text="Model", width=18, anchor=tk.W).pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=api_cfg.get("model", ""))
        self.model_combo = ttk.Combobox(
            row3, textvariable=self.model_var, width=28, state="normal",
        )
        self.model_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            row3,
            text="Connect and Find Models",
            command=self.find_models,
        ).pack(side=tk.LEFT, padx=(6, 0))
        row3.pack(side=tk.TOP, fill=tk.X, padx=8, pady=2)

        prompt = ttk.LabelFrame(left, text="Prompt (use {{headstamps}} to inject the list)")
        prompt.pack(side=tk.TOP, fill=tk.X, pady=4)
        self.prompt_text = tk.Text(prompt, height=4, wrap=tk.WORD)
        self.prompt_text.insert("1.0", api_cfg.get("prompt", ""))
        self.prompt_text.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)

        params = ttk.LabelFrame(left, text="Image encoding")
        params.pack(side=tk.TOP, fill=tk.X, pady=4)
        self.quality_field = NumericField(
            params, "JPEG quality", from_=10, to=100,
            initial=int(api_cfg.get("image_quality", 100)),
        )
        self.quality_field.pack(side=tk.LEFT, padx=8, pady=6)
        self.scale_field = NumericField(
            params, "Scale %", from_=10, to=200,
            initial=int(api_cfg.get("image_scale", 100)),
        )
        self.scale_field.pack(side=tk.LEFT, padx=8, pady=6)

        build_button_row(left, [("Save", self.save)], primary="Save")\
            .pack(side=tk.TOP, anchor=tk.W, pady=4)

        # ----- RIGHT: headstamps ---------------------------------------------
        right = ttk.LabelFrame(top, text="Headstamps")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

        add_row = ttk.Frame(right)
        add_row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        ttk.Label(add_row, text="Classifications are managed by the AI server.",
                  style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Button(add_row, text="Load from server", command=self.load_headstamps)\
            .pack(side=tk.RIGHT, padx=2)

        list_row = ttk.Frame(right)
        list_row.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=6)

        list_frame = ttk.Frame(list_row)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.listbox = tk.Listbox(
            list_frame,
            yscrollcommand=scroll.set,
            activestyle="none",
            height=8,
            exportselection=False,
        )
        scroll.config(command=self.listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        side = ttk.Frame(list_row)
        side.pack(side=tk.LEFT, fill=tk.Y, padx=10)
        self.count_var = tk.StringVar(value="0 headstamps")
        ttk.Label(side, textvariable=self.count_var, style="Muted.TLabel")\
            .pack(side=tk.TOP, anchor=tk.W)

        self._refresh_list()

        # =====================================================================
        # Bottom half: integrated test (feed -> capture -> crop -> classify)
        # =====================================================================
        test_box = ttk.LabelFrame(self, text="Test (single-shot feed → classify)")
        test_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(2, 8))

        ttk.Label(test_box, text="Live preview", style="Muted.TLabel")\
            .grid(row=0, column=0, sticky=tk.W, padx=8)
        ttk.Label(test_box, text="Last cropped", style="Muted.TLabel")\
            .grid(row=0, column=1, sticky=tk.W, padx=8)

        self.preview = ImagePanel(test_box, width=320, height=240)
        self.preview.grid(row=1, column=0, padx=8, pady=4, sticky=tk.NW)

        self.cropped = ImagePanel(test_box, width=320, height=320)
        self.cropped.grid(row=1, column=1, padx=8, pady=4, sticky=tk.NW)

        controls = ttk.Frame(test_box)
        controls.grid(row=1, column=2, padx=12, pady=4, sticky=tk.NW)
        self.feed_btn = ttk.Button(
            controls, text="Feed One", command=self.feed, style="Accent.TButton",
        )
        self.feed_btn.pack(side=tk.TOP, fill=tk.X, pady=4)
        ttk.Separator(controls, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X, pady=8)
        ttk.Label(controls, text="Result", style="Header.TLabel")\
            .pack(side=tk.TOP, anchor=tk.W)
        self.label_var = tk.StringVar(value="—")
        self.confidence_var = tk.StringVar(value="—")
        row = ttk.Frame(controls)
        row.pack(side=tk.TOP, fill=tk.X, pady=2)
        ttk.Label(row, text="Label", style="Muted.TLabel", width=12, anchor=tk.W)\
            .pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self.label_var, style="Accent.TLabel")\
            .pack(side=tk.LEFT)
        row = ttk.Frame(controls)
        row.pack(side=tk.TOP, fill=tk.X, pady=2)
        ttk.Label(row, text="Confidence", style="Muted.TLabel", width=12, anchor=tk.W)\
            .pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self.confidence_var).pack(side=tk.LEFT)

        # Bus subscriptions reused from the old Test tab.
        bus.subscribe("test/status", self.app.set_status)
        bus.subscribe("test/cropped", self.cropped.show_bgr)
        bus.subscribe("test/classified", self._on_classified)
        bus.subscribe("test/error", self._on_test_error)
        # Re-read headstamps whenever the active model changes — config.headstamps
        # is scoped to active_model_id, so the listbox would otherwise still
        # display the previously-active model's labels after a switch.
        bus.subscribe("mode/changed", lambda _payload: self._refresh_list())

    # ----- live preview hook (called by MainWindow._refresh_preview) ---------

    def update_preview(self, frame_bgr) -> None:
        self.preview.show_bgr(frame_bgr)

    # ----- headstamp list helpers --------------------------------------------

    def _names(self) -> list[str]:
        return [e.get("name", "") for e in self.config.headstamps if e.get("name")]

    def _refresh_list(self) -> None:
        self.listbox.delete(0, tk.END)
        names = sorted(self._names(), key=str.casefold)
        for name in names:
            self.listbox.insert(tk.END, name)
        self.count_var.set(
            f"{len(names)} headstamp" + ("" if len(names) == 1 else "s")
        )

    def refresh_saved_bins_runtime(self) -> None:
        """Refresh the remote-label list after a Saved Bins load."""
        self._refresh_list()

    # ----- Save / Load --------------------------------------------------------

    def save(self) -> None:
        self.config.api["endpoint_url"] = self.endpoint_var.get().strip()
        self.config.api["api_key"] = self.apikey_var.get()
        self.config.api["model"] = self.model_var.get().strip()
        self.config.api["prompt"] = self.prompt_text.get("1.0", tk.END).rstrip("\n")
        self.config.api["image_quality"] = int(self.quality_field.get())
        self.config.api["image_scale"] = int(self.scale_field.get())
        self.config.save()
        self.app.set_status("AI settings saved.")

    def load_headstamps(self) -> None:
        endpoint = self.endpoint_var.get().strip()
        model = self.model_var.get().strip()
        api_key = self.apikey_var.get().strip()
        if not endpoint or not model:
            messagebox.showerror("Missing values", "Endpoint URL and model are required.")
            return
        self.app.set_status("Loading headstamps from server…")
        self.app.run_worker(
            lambda: api_client.get_headstamps(endpoint, model, api_key),
            on_done=self._on_headstamps_loaded,
            on_error=lambda err: messagebox.showerror("Server error", str(err)),
        )

    def find_models(self) -> None:
        endpoint = self.endpoint_var.get().strip()
        api_key = self.apikey_var.get().strip()
        if not endpoint:
            messagebox.showerror("Missing value", "Endpoint URL is required.")
            return
        self.app.set_status("Finding available server models…")
        self.app.run_worker(
            lambda: api_client.list_models(endpoint, api_key),
            on_done=self._on_models_loaded,
            on_error=lambda err: messagebox.showerror("Server error", str(err)),
        )

    def _on_models_loaded(self, names) -> None:
        available = list(names)
        self.model_combo["values"] = available
        current = self.model_var.get().strip()
        if not available:
            self.app.set_status("The server is available but advertises no models.")
            messagebox.showinfo(
                "No models available",
                "The server is running but no models are currently available.",
            )
            return
        if not current:
            self.model_var.set(available[0])
        elif current not in available:
            messagebox.showwarning(
                "Configured model not advertised",
                f'The server did not advertise the currently configured model "{current}". '
                "It was not changed. Select an available model and press Save if you want "
                "to switch.",
                parent=self,
            )
        self.app.set_status(
            f"Found {len(available)} server model(s). Select one and press Save."
        )

    def _on_headstamps_loaded(self, names) -> None:
        result = self.config.synchronize_remote_headstamps(list(names))
        self._refresh_list()
        self.app.set_status(
            f"Loaded {result['received']} remote headstamp(s) "
            f"({result['added']} new, {result['removed']} removed)."
        )

    # ----- Test cycle ---------------------------------------------------------

    def feed(self) -> None:
        broker = self.app.broker
        if broker is None or not broker.is_connected:
            messagebox.showerror("Not connected", "Connect to the board first.")
            return
        if not self.config.api.get("api_key") or not self.config.api.get("model"):
            messagebox.showerror(
                "AI not configured",
                "Endpoint, API key and model must be set above first.",
            )
            return
        controller = self.app.run_controller
        if controller is None:
            messagebox.showerror("Not ready", "Run controller is not initialised.")
            return
        self._set_busy(True)
        self.app.set_status("Feeding and classifying…")
        self.app.run_worker(
            controller.test_once,
            on_done=lambda _r: self._set_busy(False),
            on_error=self._on_worker_error,
        )

    def _on_classified(self, payload: dict) -> None:
        label = payload.get("label") or "(empty)"
        confidence = float(payload.get("confidence", 0) or 0)
        self.label_var.set(label)
        self.confidence_var.set(f"{confidence:.2f}%")
        self.app.set_status(f"Classified as {label} ({confidence:.2f}%).")

    def _on_test_error(self, msg: str) -> None:
        self._set_busy(False)
        self.app.set_status(f"Test error: {msg}")
        messagebox.showerror("Test failed", str(msg))

    def _on_worker_error(self, exc: Exception) -> None:
        self._set_busy(False)
        self.app.set_status(f"Test error: {exc}")
        messagebox.showerror("Test failed", str(exc))

    def _set_busy(self, busy: bool) -> None:
        try:
            self.feed_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        except tk.TclError:
            pass
