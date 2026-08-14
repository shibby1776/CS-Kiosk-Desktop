"""Dark, touch-friendly operator dashboard for appliance/kiosk mode."""
from __future__ import annotations

import tkinter as tk
from collections import defaultdict
from pathlib import Path
from tkinter import ttk

from sorter.version import PUBLIC_VERSION

from PIL import Image, ImageTk

from ..events import post_assignment_changed
from .tab_run import FlowGrid, SlotCard, SlotDetailsPanel
from .theme import PALETTE
from .widgets import ImagePanel


class KioskDashboard(ttk.Frame):
    """Front-facing operator view backed by the existing RunTab/controller."""

    def __init__(self, parent, *, app) -> None:
        super().__init__(parent, style="Kiosk.TFrame", padding=20)
        self.app = app
        self.config = app.config
        self.state_var = tk.StringVar(value="INITIALIZING")
        self.detail_var = tk.StringVar(value="Connecting to camera and sorter…")
        self.label_var = tk.StringVar(value="—")
        self.confidence_var = tk.StringVar(value="—")
        self.destination_var = tk.StringVar(value="—")
        self.count_var = tk.StringVar(value="0")
        self.labels_var = tk.StringVar(value="Labels: loading")
        self._auto_select_var = tk.BooleanVar(value=self.config.run_auto_select_trays)
        self._running = False
        self._count = 0
        self._slot_counts: dict[int, int] = defaultdict(int)
        self._slot_cards: list[SlotCard] = []
        self._logo_taps = 0
        self._logo_reset = None
        self._transitioning = False
        self._logo_source = None
        self._logo_photo = None
        self._logo_render_size: tuple[int, int] | None = None

        self.columnconfigure(0, weight=5)
        self.columnconfigure(1, weight=6)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, style="Kiosk.TFrame")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self.logo_label = ttk.Label(header, style="KioskTitle.TLabel")
        self.logo_label.pack(side=tk.LEFT)
        self.logo_label.bind("<Button-1>", self._tap_logo)
        self._load_logo()
        ttk.Label(header, textvariable=self.state_var, style="KioskState.TLabel").pack(side=tk.RIGHT)

        # Left: current processing image, result, controls and automatic tray selection.
        left = ttk.Frame(self, style="Kiosk.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        preview_card = ttk.Frame(left, style="KioskCard.TFrame", padding=16)
        preview_card.grid(row=0, column=0, sticky="nsew")
        preview_card.columnconfigure(0, weight=1)
        preview_card.rowconfigure(1, weight=1)
        ttk.Label(preview_card, text="CURRENT PROCESSING IMAGE", style="KioskCaption.TLabel").grid(sticky="w")
        self.processing_image = ImagePanel(preview_card, width=520, height=390)
        self.processing_image.grid(row=1, column=0, sticky="nsew", pady=(10, 12))

        result = ttk.Frame(preview_card, style="KioskCard.TFrame")
        result.grid(row=2, column=0, sticky="ew")
        result.columnconfigure(0, weight=1)
        ttk.Label(result, textvariable=self.label_var, style="KioskResult.TLabel", anchor="center").grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Label(result, textvariable=self.destination_var, style="KioskDestination.TLabel", anchor="center").grid(
            row=1, column=0, sticky="ew", pady=(4, 0)
        )
        ttk.Label(result, textvariable=self.confidence_var, style="KioskMuted.TLabel", anchor="center").grid(
            row=2, column=0, sticky="ew", pady=(4, 0)
        )

        status_card = ttk.Frame(left, style="KioskCard.TFrame", padding=14)
        status_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        status_card.columnconfigure(0, weight=1)
        count_row = ttk.Frame(status_card, style="KioskCard.TFrame")
        count_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        count_row.columnconfigure(0, weight=1)
        ttk.Label(count_row, text="MASTER COUNT", style="KioskCaption.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(count_row, textvariable=self.count_var, style="KioskMetric.TLabel").grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )
        ttk.Separator(status_card, orient=tk.HORIZONTAL).grid(
            row=1, column=0, sticky="ew", pady=(0, 8)
        )
        ttk.Label(status_card, textvariable=self.detail_var, style="KioskBody.TLabel", wraplength=520).grid(
            row=2, column=0, sticky="w"
        )
        ttk.Label(status_card, textvariable=self.labels_var, style="KioskMuted.TLabel").grid(
            row=3, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(
            status_card,
            text="Automatically sort tray",
            variable=self._auto_select_var,
            command=self._on_toggle_auto_select,
            style="Kiosk.TCheckbutton",
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))

        controls = ttk.Frame(left, style="Kiosk.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        controls.columnconfigure((0, 1, 2), weight=1)
        self.start_button = ttk.Button(
            controls, text="START SORTING", style="KioskPrimary.TButton", command=self._toggle
        )
        self.start_button.grid(
            row=0, column=0, columnspan=3, sticky="ew", ipady=16
        )
        ttk.Button(
            controls, text="FEED ONE", style="KioskSecondary.TButton", command=self._manual
        ).grid(row=1, column=0, sticky="ew", ipady=10, padx=(0, 5), pady=(8, 0))
        ttk.Button(
            controls, text="RESET COUNTERS", style="KioskSecondary.TButton", command=self._reset_counters
        ).grid(row=1, column=1, sticky="ew", ipady=10, padx=5, pady=(8, 0))
        ttk.Button(
            controls,
            text="CLEAR SLOTS",
            style="KioskSecondary.TButton",
            command=self.app.confirm_clear_slots,
        ).grid(row=1, column=2, sticky="ew", ipady=10, padx=(5, 0), pady=(8, 0))

        # Right: all slots plus a routine operator assignment panel.
        right = ttk.Frame(self, style="Kiosk.TFrame")
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        slots_card = ttk.Frame(right, style="KioskCard.TFrame", padding=12)
        slots_card.grid(row=0, column=0, sticky="ew")
        slots_card.columnconfigure(0, weight=1)
        title_row = ttk.Frame(slots_card, style="KioskCard.TFrame")
        title_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(title_row, text="AVAILABLE SLOTS", style="KioskCaption.TLabel").pack(side=tk.LEFT)
        ttk.Label(title_row, text="Tap a slot to assign headstamps", style="KioskMuted.TLabel").pack(side=tk.RIGHT)
        self.slot_grid = FlowGrid(slots_card, cell_width=180, gutter=8, expand_cells=True)
        self.slot_grid.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.details = SlotDetailsPanel(
            right,
            config=self.config,
            on_assignment_change=self._notify_assignment_changed,
        )
        self.details.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        self._build_slot_cards()
        self._refresh_card_headstamps()
        self._select_slot(0)

        bus = app.bus
        bus.subscribe("run/started", lambda _p: self._set_running(True))
        bus.subscribe("run/stopped", lambda _p: self._set_running(False))
        bus.subscribe("run/error", self._on_error)
        bus.subscribe("run/status", self._on_status)
        bus.subscribe("run/cropped", self.processing_image.show_bgr)
        bus.subscribe("run/classified", self._on_classified)
        bus.subscribe("run/result", self._on_result)
        bus.subscribe("run/assignment_changed", lambda _p: self._on_assignment_changed())
        bus.subscribe("run/headstamps_synced", self._on_labels)
        bus.subscribe("run/headstamps_sync_warning", self._on_label_warning)
        bus.subscribe("run/counters_reset", lambda _p: self._clear_counters())
        bus.subscribe("mode/changed", lambda _p: self._on_assignment_changed())

    def _load_logo(self) -> None:
        """Load the approved transparent logo and trim only transparent padding."""
        logo_path = Path(__file__).resolve().parents[2] / "assets" / "shibbyprints_logo.png"
        image = Image.open(logo_path).convert("RGBA")
        # The supplied PNG contains broad transparent padding and a few very
        # low-alpha edge pixels. Crop to meaningful visible artwork so the
        # logo scales as operators expect, without changing its aspect ratio.
        alpha = image.getchannel("A").point(lambda value: 255 if value > 10 else 0)
        visible_box = alpha.getbbox()
        self._logo_source = image.crop(visible_box) if visible_box else image
        self._set_logo_size(240, 49)

    def _set_logo_size(self, max_width: int, max_height: int) -> None:
        if self._logo_source is None:
            return
        target = (max(1, max_width), max(1, max_height))
        if target == self._logo_render_size:
            return
        image = self._logo_source.copy()
        image.thumbnail(target, Image.Resampling.LANCZOS)
        self._logo_photo = ImageTk.PhotoImage(image)
        self.logo_label.configure(image=self._logo_photo)
        self._logo_render_size = target

    def _build_slot_cards(self) -> None:
        slot_count = int(self.config.serial.get("slot_quantity", 8))
        for slot_num in range(max(1, slot_count)):
            card = SlotCard(self.slot_grid, slot_number=slot_num, on_click=self._select_slot)
            self.slot_grid.add(card)
            self._slot_cards.append(card)

    def _refresh_card_headstamps(self) -> None:
        slot_map: dict[int, list[str]] = defaultdict(list)
        if self.config.run_package_mode:
            for slot, names in self.config.package_slot_map().items():
                if slot > 0 and names:
                    slot_map[slot].extend(names)
        elif (
            self.config.model_has_parents()
            and self.config.use_parent_classifications
        ):
            for parent in self.config.parents_with_slots():
                slot = int(parent["slot"])
                if slot > 0:
                    slot_map[slot].append(parent["name"])
            for headstamp in self.config.headstamps_with_parents():
                slot = int(headstamp["slot"])
                if headstamp["parent_id"] is None and slot > 0:
                    slot_map[slot].append(headstamp["name"])
        else:
            for entry in self.config.headstamps:
                name = entry.get("name")
                slot = int(entry.get("slot", 0))
                if name and slot > 0:
                    slot_map[slot].append(name)
        for card in self._slot_cards:
            card.set_headstamps(sorted(slot_map.get(card.slot_number, []), key=str.casefold))
            card.set_count(self._slot_counts[card.slot_number])

    def _select_slot(self, slot_num: int) -> None:
        for card in self._slot_cards:
            card.set_selected(card.slot_number == slot_num)
        self.details.show_slot(slot_num)

    def _on_assignment_changed(self) -> None:
        self._refresh_card_headstamps()
        if self.details.current_slot is not None:
            self.details.show_slot(self.details.current_slot)

    def _notify_assignment_changed(self) -> None:
        """Publish a manual Operator assignment through the shared event bus."""
        post_assignment_changed(self.app.bus, "operator_manual")

    def refresh_saved_bins_runtime(self) -> None:
        """Synchronously redraw routing after a verified Saved Bins load."""
        self._clear_counters()
        self._on_assignment_changed()

    def sync_auto_select_from_config(self) -> None:
        """Refresh the operator toggle from the persisted shared setting."""
        self._auto_select_var.set(bool(self.config.run_auto_select_trays))

    def _on_toggle_auto_select(self) -> None:
        enabled = bool(self._auto_select_var.get())
        self.config.set_run_auto_select_trays(enabled)
        # Keep the maintenance Run tab synchronized immediately.
        if self.app.run_tab is not None and hasattr(
            self.app.run_tab, "sync_auto_select_from_config"
        ):
            self.app.run_tab.sync_auto_select_from_config()
        elif self.app.run_tab is not None and hasattr(
            self.app.run_tab, "_auto_select_var"
        ):
            self.app.run_tab._auto_select_var.set(enabled)
        self.detail_var.set("Automatic tray sorting enabled." if enabled else "Automatic tray sorting disabled.")

    @staticmethod
    def _preview_display_size(container_width: int, container_height: int) -> tuple[int, int]:
        """Return a display-only image size that fits inside the operator card.

        The camera frame retained by :class:`ImagePanel` is not modified; only
        the canvas used to show it is resized.  Keep enough vertical space for
        the caption, classification result, destination, confidence and card
        padding so small kiosk displays never clip the bottom of the preview.
        """
        available_width = max(1, container_width - 36)
        available_height = max(1, container_height - 190)
        width = max(1, available_width)
        height = max(1, min(available_height, int(width * 0.75)))
        return width, height

    def _toggle(self) -> None:
        self.app.toggle_run()

    def _manual(self) -> None:
        self.app.manual_feed()

    def _reset_counters(self) -> None:
        """Use the same reset path as the maintenance Run screen."""
        self.app.reset_run_counters()

    def _clear_counters(self) -> None:
        self._count = 0
        self.count_var.set("0")
        self._slot_counts.clear()
        for card in self._slot_cards:
            card.set_count(0)
        self.details.reset_counters()
        self.detail_var.set("Counters reset.")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.state_var.set("RUNNING" if running else "READY")
        self.start_button.configure(
            text="STOP SORTING" if running else "START SORTING",
            style="KioskStop.TButton" if running else "KioskPrimary.TButton",
        )
        if not running and self.detail_var.get().startswith("Sorting"):
            self.detail_var.set("Sorter ready.")

    def _on_status(self, message) -> None:
        self.detail_var.set(str(message))

    def _on_labels(self, payload) -> None:
        received = int((payload or {}).get("received", 0))
        added = int((payload or {}).get("added", 0))
        self.labels_var.set(f"Labels: {received} loaded ({added} new)")
        self._on_assignment_changed()
        if not self._running:
            self.state_var.set("READY")

    def _on_label_warning(self, error) -> None:
        self.labels_var.set("Labels: saved list in use")
        self.detail_var.set(f"Remote refresh unavailable: {error}")
        self.state_var.set("ATTENTION")

    def _on_classified(self, payload) -> None:
        payload = payload or {}
        label = payload.get("label") or "UNKNOWN"
        confidence = float(payload.get("confidence", 0) or 0)
        slot = int(payload.get("slot") or 0)
        self.label_var.set(str(label).upper())
        self.confidence_var.set(f"{confidence:.1f}% confidence")
        self.destination_var.set("CATCH-ALL" if slot == 0 else f"SLOT {slot}")

    def _on_result(self, result) -> None:
        if not (result or {}).get("ok"):
            return
        slot = int((result or {}).get("slot") or 0)
        label = str((result or {}).get("label") or "")
        self._count += 1
        self.count_var.set(str(self._count))
        self._slot_counts[slot] += 1
        for card in self._slot_cards:
            if card.slot_number == slot:
                card.set_count(self._slot_counts[slot])
                break
        if label:
            self.details.increment_headstamp(slot, label)

    def _on_error(self, message) -> None:
        self.state_var.set("FAULT")
        self.detail_var.set(str(message))
        self._set_running(False)

    def _tap_logo(self, _event=None) -> None:
        self._logo_taps += 1
        if self._logo_reset is not None:
            self.after_cancel(self._logo_reset)
        if self._logo_taps >= 5:
            self._logo_taps = 0
            self.app.request_maintenance()
            return
        self._logo_reset = self.after(3000, self._reset_logo_taps)

    def show_maintenance_transition(self, on_complete) -> None:
        """Show the approved, non-blocking five-second maintenance transition."""
        if self._transitioning:
            return
        self._transitioning = True
        overlay = tk.Frame(self, bg=PALETTE["bg_window"])
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.columnconfigure(0, weight=1)
        overlay.rowconfigure(0, weight=1)

        panel = ttk.Frame(overlay, style="KioskCard.TFrame", padding=48)
        panel.grid(row=0, column=0)

        # Maintenance splash branding: 40% smaller than the v16 splash
        # (30% of the uploaded image's native dimensions), preserving aspect ratio.
        splash_path = Path(__file__).resolve().parents[2] / "assets" / "shibbyprints_logo.png"
        splash_image = Image.open(splash_path).convert("RGBA")
        splash_target = (max(1, round(splash_image.width * 0.30)), max(1, round(splash_image.height * 0.30)))
        splash_alpha = splash_image.getchannel("A").point(lambda value: 255 if value > 10 else 0)
        splash_box = splash_alpha.getbbox()
        if splash_box:
            splash_image = splash_image.crop(splash_box)
        splash_image.thumbnail(splash_target, Image.Resampling.LANCZOS)
        overlay._splash_logo = ImageTk.PhotoImage(splash_image)
        ttk.Label(panel, image=overlay._splash_logo, style="KioskCard.TLabel").pack(pady=(0, 18))
        ttk.Label(panel, text="Entering Maintenance Mode", style="KioskTitle.TLabel").pack(pady=(0, 24))
        ttk.Label(
            panel,
            text=(
                "Based on GPL-licensed source code originally\n"
                "created by SJSeth Solutions.\n\n"
                "Kiosk Edition independently developed and\n"
                "maintained by ShibbyPrints.\n\n"
                "No endorsement is claimed or implied."
            ),
            style="KioskBody.TLabel",
            justify=tk.CENTER,
        ).pack()
        ttk.Label(
            overlay,
            text=PUBLIC_VERSION,
            style="KioskMuted.TLabel",
        ).place(relx=1.0, rely=1.0, x=-24, y=-20, anchor="se")

        def finish() -> None:
            try:
                overlay.destroy()
            finally:
                self._transitioning = False
                on_complete()

        self.after(5000, finish)

    def _reset_logo_taps(self) -> None:
        self._logo_taps = 0
        self._logo_reset = None
