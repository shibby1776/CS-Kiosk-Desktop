"""Main Tk application shell — notebook + status bar + cross-thread wiring."""
from __future__ import annotations

import threading
import tkinter as tk
import traceback
import webbrowser
from tkinter import messagebox, ttk
from typing import Any, Callable

from .. import serial_broker
from ..camera import Camera
from ..config import Config
from ..events import EventBus
from ..led_worker import LedCommandWorker
from ..run_controller import RunController
from ..serial_emulator import EmulatorBroker, EMULATED_PORT
from ..version import PUBLIC_VERSION_NUMBER
from .tab_ai import AiTab
from .tab_camera import CameraTab
from .tab_community import CommunityTab
from .tab_imageproc import ImageProcTab
from .tab_models import ModelsTab
from .tab_run import RunTab
from .tab_serial import SerialTab
from .tab_train import TrainTab
from .kiosk import KioskDashboard
from .theme import PALETTE, apply_theme, paint_gradient
from .widgets import ScrollableFrame


PREVIEW_FPS = 10
HEADER_HEIGHT = 36


class MainWindow:
    def __init__(self, config: Config, db: Any | None = None) -> None:
        self.config = config
        self.db = db if db is not None else getattr(config, "db", None)
        self.bus = EventBus()
        self.root = tk.Tk()
        self.root.title("ShibbyPrints Kiosk Sorter — Desktop")
        self.root.geometry("1280x820")
        self.root.minsize(1024, 700)
        self.root.configure(cursor="")
        self._maintenance_mode = False
        self._build_desktop_menu()

        self.fonts = apply_theme(self.root)

        self.broker: Any | None = None
        self.diagnostics = None
        self.diagnostics_tab = None
        self._diagnostics_container = None
        self.led_worker = LedCommandWorker(app=self, config=config, bus=self.bus)
        self.camera = Camera(
            device_index=int(config.camera.get("device_index", 0)),
            width=int(config.camera.get("width", 1920)),
            height=int(config.camera.get("height", 1080)),
        )
        self._shared_camera_warning_shown = False
        self.run_controller: RunController | None = None

        # Gradient title bar at the top — the visible "gradient background"
        # of the modernised look. Painted on a Canvas because Tk widgets
        # can't render gradients directly.
        self.header_canvas = tk.Canvas(
            self.root,
            height=HEADER_HEIGHT,
            bg=PALETTE["bg_window"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.header_canvas.pack(side=tk.TOP, fill=tk.X)
        self.header_canvas.bind("<Configure>", self._repaint_header)

        # Status bar (must exist before tabs that call set_status).
        self.status_var = tk.StringVar(value="Idle.")
        self.serial_status_var = tk.StringVar(value="Serial: disconnected")
        self.camera_status_var = tk.StringVar(value="Camera: disconnected")
        status_bar = ttk.Frame(self.root, style="StatusBar.TFrame")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(status_bar, orient=tk.HORIZONTAL).pack(side=tk.TOP, fill=tk.X)
        ttk.Label(
            status_bar, textvariable=self.status_var,
            anchor=tk.W, style="Status.TLabel",
        ).pack(side=tk.LEFT, padx=12, pady=6)
        # Sign-in / sign-out button on the right side of the status bar.
        self.signin_var = tk.StringVar(value="Sign in")
        self.signin_button = ttk.Button(
            status_bar, textvariable=self.signin_var, command=self._on_signin_click,
        )
        self.signin_button.pack(side=tk.RIGHT, padx=12, pady=4)
        # Pack serial first so it ends up rightmost; camera sits to its left.
        # Each indicator is a [dot][text] pair grouped in a sub-frame so the
        # dot stays glued to its label when the bar resizes.
        self.serial_dot = self._build_status_indicator(status_bar, self.serial_status_var)
        self.camera_dot = self._build_status_indicator(status_bar, self.camera_status_var)

        self.content_host = ttk.Frame(self.root, style="Kiosk.TFrame")
        self.content_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.maintenance_host = ttk.Frame(self.content_host, style="Window.TFrame")
        maintenance_bar = ttk.Frame(self.maintenance_host, style="KioskCard.TFrame", padding=(18, 12))
        maintenance_bar.pack(side=tk.TOP, fill=tk.X, padx=12, pady=(10, 0))
        ttk.Button(
            maintenance_bar,
            text="RETURN TO OPERATOR",
            style="KioskSecondary.TButton",
            command=self.show_kiosk,
        ).pack(side=tk.RIGHT)
        notebook = ttk.Notebook(self.maintenance_host)
        notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=(8, 0))
        # Expose the notebook BEFORE constructing tabs so that tabs that
        # want to bind <<NotebookTabChanged>> on it (e.g. TrainTab) can
        # find it via `app.notebook`.
        self.notebook = notebook

        # Each tab sits inside a ScrollableFrame so that on small displays
        # (e.g. 1280x720) the content can scroll vertically rather than
        # being clipped beyond the window edge.
        def _add_scrolled(tab_cls, label):
            container = ScrollableFrame(notebook)
            tab = tab_cls(container.body, config=config, bus=self.bus, app=self)
            tab.pack(fill=tk.BOTH, expand=True)
            notebook.add(container, text=label)
            return tab

        self._lazy_tabs: dict[str, tuple[ScrollableFrame, type]] = {}

        def _add_lazy_scrolled(tab_cls, label):
            """Add an empty tab and construct its contents on first selection."""
            container = ScrollableFrame(notebook)
            notebook.add(container, text=label)
            self._lazy_tabs[str(container)] = (container, tab_cls)
            return container

        self._add_lazy_scrolled = _add_lazy_scrolled
        self.run_tab = None
        self._run_tab_container = _add_lazy_scrolled(RunTab, "Run")
        self.models_tab = None
        self._models_tab_container = _add_lazy_scrolled(ModelsTab, "Models")
        self.train_tab = None
        self._train_tab_container = _add_lazy_scrolled(TrainTab, "Train")
        # AI Config tab — always created, but its visibility tracks the
        # active runtime mode. The Notebook's `hide()` / `add()` methods
        # let us toggle the tab in/out of the tab bar without destroying it.
        self._ai_tab_container = ScrollableFrame(notebook)
        self.ai_tab = AiTab(self._ai_tab_container.body, config=config, bus=self.bus, app=self)
        self.ai_tab.pack(fill=tk.BOTH, expand=True)
        notebook.add(self._ai_tab_container, text="AI Config")
        self.imageproc_tab = None
        self._imageproc_tab_container = _add_lazy_scrolled(
            ImageProcTab, "Image Processing"
        )
        self.serial_tab = _add_scrolled(SerialTab, "Serial Config")
        self.camera_tab = _add_scrolled(CameraTab, "Camera")
        # Community tab is hidden until the user signs in.
        self.community_tab: CommunityTab | None = None
        self._community_container: ttk.Frame | None = None
        self._add_scrolled = _add_scrolled
        from ..auth import AuthManager
        try:
            self.auth: AuthManager | None = AuthManager()
        except Exception:
            self.auth = None
        if self.auth is not None and self.auth.is_authenticated():
            self._mount_community_tab()

        notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed, add="+")

        # Apply initial visibility for the AI Config tab. The Training tab is
        # always available in Maintenance Mode on the Desktop Edition.
        self._apply_ai_config_visibility()
        self.bus.subscribe("mode/changed", lambda _payload: self._apply_ai_config_visibility())

        self.bus.subscribe("status", self.set_status)

        self.kiosk = KioskDashboard(self.content_host, app=self)
        self.kiosk.pack(fill=tk.BOTH, expand=True)
        self.header_canvas.pack_forget()
        status_bar.pack_forget()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(50, self._drain_bus)
        self.root.after(int(1000 / PREVIEW_FPS), self._refresh_preview)

        # The Camera tab runs a full device/resolution probe on startup and
        # starts the preview once it picks the best resolution (see
        # CameraTab._auto_detect_on_startup). Starting it here too would
        # race with that probe trying to open the same device.
        # Auto-connect to the board once the UI is on screen.
        self.root.after(200, self._auto_connect_serial)
        # Repaint legacy Tk widgets after the complete tree exists. This is
        # required on Raspberry Pi OS, where native desktop defaults can leak
        # light backgrounds into Canvas/Text/Listbox controls.
        # Reassert a few times after startup so Openbox/native defaults cannot
        # repaint the window light. Do not bind this to <<ThemeChanged>>:
        # reapplying a ttk theme from that event can recursively emit the same
        # event and prevent the application from finishing startup.

    # ----- desktop shell ------------------------------------------------------

    def _build_desktop_menu(self) -> None:
        menu = tk.Menu(self.root)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Operator Mode", accelerator="Ctrl+O", command=self.show_kiosk)
        file_menu.add_command(label="Maintenance Mode", accelerator="Ctrl+M", command=self.show_maintenance)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", accelerator="Ctrl+Q", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_command(label="Operator Mode", command=self.show_kiosk)
        view_menu.add_command(label="Maintenance Mode", command=self.show_maintenance)
        menu.add_cascade(label="View", menu=view_menu)

        account_menu = tk.Menu(menu, tearoff=False)
        account_menu.add_command(label="Sign in / Sign out", command=self._on_signin_click)
        menu.add_cascade(label="Account", menu=account_menu)

        menu.add_command(label="About", command=self._show_about)

        self.root.configure(menu=menu)
        self.root.bind_all("<Control-o>", lambda _event: self.show_kiosk())
        self.root.bind_all("<Control-m>", lambda _event: self.show_maintenance())
        self.root.bind_all("<Control-q>", lambda _event: self._on_close())
        self.root.bind_all("<Control-d>", self._toggle_diagnostics)
        self.root.bind_all("<F11>", self._toggle_fullscreen)
        self._desktop_fullscreen = False

    def _toggle_diagnostics(self, _event=None) -> str:
        if self.diagnostics is None:
            self._enable_diagnostics()
        else:
            self._disable_diagnostics()
        return "break"

    def _enable_diagnostics(self) -> None:
        """Lazily create diagnostics only after the operator requests them."""
        from ..diagnostics import DiagnosticCollector
        from .tab_diagnostics import DiagnosticsTab

        self.diagnostics = DiagnosticCollector()
        container = ScrollableFrame(self.notebook)
        tab = DiagnosticsTab(
            container.body, config=self.config, bus=self.bus, app=self
        )
        tab.pack(fill=tk.BOTH, expand=True)
        self.notebook.add(container, text="Diagnostics")
        self._diagnostics_container = container
        self.diagnostics_tab = tab
        if self.run_controller is not None:
            self.run_controller.diagnostics = self.diagnostics
        self.show_maintenance()
        self.notebook.select(container)
        self.set_status("Diagnostics enabled.")

    def _disable_diagnostics(self) -> bool:
        """Stop collection and release every lazily-created diagnostic resource."""
        tab = self.diagnostics_tab
        if tab is not None and getattr(tab, "sensor_test_active", False):
            tab.request_sensor_test_cancel()
            messagebox.showwarning(
                "Sensor test stopping",
                (
                    "The active motion test must reach a safe command boundary "
                    "and restore its temporary controller setting before "
                    "Diagnostics can close. Wait for the result, then press "
                    "Ctrl+D again."
                ),
                parent=self.root,
            )
            return False
        if self.run_controller is not None:
            self.run_controller.diagnostics = None
        container = self._diagnostics_container
        self.diagnostics_tab = None
        self._diagnostics_container = None
        if tab is not None:
            tab.shutdown()
        if container is not None:
            try:
                self.notebook.forget(container)
            except tk.TclError:
                pass
            container.destroy()
        self.diagnostics = None
        self.set_status("Diagnostics disabled.")
        return True

    def _record_diagnostic(self, method: str, *args) -> None:
        collector = self.diagnostics
        if collector is not None:
            getattr(collector, method)(*args)

    def _toggle_fullscreen(self, _event=None) -> None:
        self._desktop_fullscreen = not self._desktop_fullscreen
        self.root.attributes("-fullscreen", self._desktop_fullscreen)

    def _show_about(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("About ShibbyPrints Kiosk Sorter")
        window.transient(self.root)
        window.resizable(False, False)
        body = ttk.Frame(window, padding=24)
        body.pack(fill=tk.BOTH, expand=True)

        pages = ttk.Notebook(body)
        pages.pack(fill=tk.BOTH, expand=True)
        about_page = ttk.Frame(pages, padding=18)
        hidden_page = ttk.Frame(pages, padding=18)
        pages.add(about_page, text="About")
        pages.add(hidden_page, text="Hidden Features")

        ttk.Label(
            about_page,
            text="ShibbyPrints Kiosk Sorter Software",
            style="Title.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            about_page,
            text=f"Kiosk {PUBLIC_VERSION_NUMBER}",
            style="Accent.TLabel",
        ).pack(anchor=tk.W, pady=(2, 16))
        ttk.Label(
            about_page,
            text=(
                "The ShibbyPrints Kiosk Edition is based on software "
                "originally created by SJSeth Solutions and licensed under the "
                "GNU General Public License, version 3 or later "
                "(GPL-3.0-or-later). This software is provided without warranty. "
                "The complete license terms are included in the LICENSE file, "
                "and corresponding source code is provided with each official "
                "release.\n\n"
                "ShibbyPrints independently maintains this Kiosk Edition as an "
                "optional companion to the "
                "software and hardware ecosystem created by SJSeth Solutions. "
                "This edition is not an official SJSeth Solutions product. No "
                "sponsorship or endorsement by SJSeth Solutions is claimed or "
                "implied. Support for this Kiosk Edition is provided by "
                "ShibbyPrints."
            ),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        ttk.Label(
            about_page,
            text="Original source code:",
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        self._about_link(
            about_page, "https://github.com/sjseth/AI-Case-Sorter-Py"
        )
        ttk.Label(
            about_page,
            text=(
                "SJSeth provides electronics kits and components for compatible "
                "sorter systems:"
            ),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        self._about_link(about_page, "https://shop.sjseth.com/")
        ttk.Label(
            about_page,
            text=(
                "ShibbyPrints provides printed-parts kits and fully assembled "
                "CS7.2 sorter units:"
            ),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        self._about_link(about_page, "https://www.shibbyprints.com/")
        ttk.Label(
            about_page,
            text=(
                "Looking for the community Discord? A link is available on the "
                "SJSeth software website:"
            ),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))
        self._about_link(
            about_page, "https://www.reloadingrecipes.com/HeadstampSorter"
        )
        ttk.Label(
            hidden_page, text="Hidden Features", style="Title.TLabel"
        ).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(
            hidden_page,
            text=(
                "These controls are intentionally kept out of the normal "
                "Operator interface."
            ),
            style="Muted.TLabel",
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 14))
        hidden_features = (
            (
                "Ctrl + D",
                "Enable hidden diagnostics. Press it again to stop diagnostics "
                "and release the diagnostic panel, timers, histories, and buffers.",
            ),
            ("Ctrl + O", "Return directly to Operator Mode."),
            ("Ctrl + M", "Open Maintenance Mode directly."),
            ("Ctrl + Q", "Close the application safely."),
            ("F11", "Toggle desktop fullscreen mode."),
            (
                "Operator logo: five taps",
                "Enter Maintenance Mode after the intentional five-second "
                "transition screen.",
            ),
        )
        for control, description in hidden_features:
            row = ttk.Frame(hidden_page)
            row.pack(fill=tk.X, anchor=tk.W, pady=5)
            ttk.Label(
                row, text=control, style="Accent.TLabel", width=28, anchor=tk.W
            ).pack(side=tk.LEFT, anchor=tk.N)
            ttk.Label(
                row,
                text=description,
                wraplength=350,
                justify=tk.LEFT,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(body, text="Close", command=window.destroy).pack(
            anchor=tk.E, pady=(18, 0)
        )
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.grab_set()
        window.wait_visibility()
        window.focus_set()

    @staticmethod
    def _about_link(parent, url: str) -> None:
        link = tk.Label(
            parent,
            text=url,
            fg="#4da3ff",
            cursor="hand2",
            bg=PALETTE["bg_window"],
        )
        link.pack(anchor=tk.W, pady=(4, 0))
        link.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target))

    def confirm_clear_slots(self) -> bool:
        """Confirm and clear all routing assignments for the active context."""
        confirmed = messagebox.askyesno(
            "Clear Slots",
            "Clear every slot assignment and reset all counters?\n\n"
            "Headstamp names will remain available for remapping. This action "
            "cannot restore the previous slot assignments.",
            parent=self.root,
            icon=messagebox.WARNING,
        )
        if not confirmed:
            return False
        self.config.clear_slot_assignments()
        self.reset_run_counters()
        self.bus.post("run/assignment_changed", {"source": "clear_slots"})
        self.set_status("All slot assignments and counters cleared.")
        return True

    def toggle_run(self) -> None:
        """Start or stop sorting without requiring the Maintenance Run UI."""
        controller = self.run_controller
        if controller is None:
            messagebox.showerror(
                "Not ready", "Connect to the board first.", parent=self.root
            )
            return
        if not self.config.api.get("api_key") or not self.config.api.get("model"):
            messagebox.showerror(
                "AI not configured",
                "Set endpoint, API key and model on the AI Config tab first.",
                parent=self.root,
            )
            return
        if controller.is_running:
            controller.stop()
        else:
            controller.start()

    def manual_feed(self) -> None:
        """Run one classify-and-sort cycle without requiring Maintenance UI."""
        controller = self.run_controller
        if controller is None:
            messagebox.showerror(
                "Not ready", "Connect to the board first.", parent=self.root
            )
            return
        if controller.is_running:
            messagebox.showerror(
                "Run in progress",
                "Stop the continuous run before triggering a manual feed.",
                parent=self.root,
            )
            return
        if not self.config.api.get("api_key") or not self.config.api.get("model"):
            messagebox.showerror(
                "AI not configured",
                "Set endpoint, API key and model on the AI Config tab first.",
                parent=self.root,
            )
            return
        self.run_worker(controller.cycle_once)

    def reset_run_counters(self) -> None:
        """Reset controller and visible counters through the shared event bus."""
        if self.run_tab is not None:
            self.run_tab.reset_counter_display()
        controller = self.run_controller
        if controller is not None and hasattr(controller, "reset_package_counts"):
            controller.reset_package_counts()
        self.bus.post("run/counters_reset", None)
        self.set_status("Counters reset.")

    def refresh_saved_bins_runtime(self) -> None:
        """Synchronously expose a verified Saved Bins load to the Run surfaces."""
        self.reset_run_counters()
        if self.run_tab is not None:
            self.run_tab.refresh_saved_bins_runtime()
        self.kiosk.refresh_saved_bins_runtime()
        self.ai_tab.refresh_saved_bins_runtime()
        self._apply_ai_config_visibility()

    # ----- kiosk / maintenance -----------------------------------------------

    def request_maintenance(self) -> None:
        """Enter maintenance after the kiosk's intentional 5-second transition."""
        if self._maintenance_mode:
            return
        self.kiosk.show_maintenance_transition(self.show_maintenance)

    def show_maintenance(self) -> None:
        self._maintenance_mode = True
        if self.run_tab is not None:
            self.run_tab.sync_from_operator(self.kiosk)
        self.root.configure(cursor="")
        self.kiosk.pack_forget()
        self.maintenance_host.pack(fill=tk.BOTH, expand=True)
        self.header_canvas.pack(side=tk.TOP, fill=tk.X, before=self.content_host)
        self._on_notebook_tab_changed()
        if self.run_tab is not None:
            self.run_tab._schedule_v_sash_adjust()

    def show_kiosk(self) -> None:
        self._maintenance_mode = False
        if hasattr(self.kiosk, "sync_auto_select_from_config"):
            self.kiosk.sync_auto_select_from_config()
        self.kiosk._on_assignment_changed()
        self._apply_cursor_policy()
        self.maintenance_host.pack_forget()
        self.header_canvas.pack_forget()
        self.kiosk.pack(fill=tk.BOTH, expand=True)

    def _apply_cursor_policy(self) -> None:
        """Desktop Edition always uses the normal operating-system cursor."""
        self.root.configure(cursor="")

    # ----- status -------------------------------------------------------------

    # ----- AI Config tab visibility (mode-gated) ------------------------------

    def _on_notebook_tab_changed(self, _event=None) -> None:
        """Construct a deferred Maintenance tab once, when first selected."""
        if not self._maintenance_mode:
            return
        try:
            tab_id = self.notebook.select()
        except tk.TclError:
            return
        pending = self._lazy_tabs.pop(tab_id, None)
        if pending is None:
            return
        container, tab_cls = pending
        tab = tab_cls(
            container.body, config=self.config, bus=self.bus, app=self
        )
        tab.pack(fill=tk.BOTH, expand=True)
        label = self.notebook.tab(container, "text")
        if label == "Run":
            self.run_tab = tab
            tab.sync_from_operator(self.kiosk)
        elif label == "Models":
            self.models_tab = tab
        elif label == "Train":
            self.train_tab = tab
        elif label == "Image Processing":
            self.imageproc_tab = tab
        elif label == "Community":
            self.community_tab = tab

    def _apply_ai_config_visibility(self) -> None:
        """Show the AI Config tab in AI-Config mode; hide it when a local model is active."""
        if self.db is None:
            return
        from ..repository import SettingsRepo
        active_id = SettingsRepo(self.db).get_active_model_id()
        try:
            if active_id is None:
                self.notebook.add(self._ai_tab_container, text="AI Config")
                # Re-add appends to the end; re-order so it lands after Train.
                self._restore_ai_config_position()
            else:
                self.notebook.hide(self._ai_tab_container)
        except tk.TclError:
            # `hide` on an already-hidden tab or `add` on an unknown widget
            # both raise — both are safe no-ops here.
            pass

    def _restore_ai_config_position(self) -> None:
        """Keep the AI Config tab between Train and Image Processing."""
        try:
            target_index = self.notebook.index(self._ai_tab_container)
            # Find the Train tab index; AI Config should follow it.
            tabs = self.notebook.tabs()
            train_index = None
            for i, tid in enumerate(tabs):
                if self.notebook.tab(tid, "text") == "Train":
                    train_index = i
                    break
            if train_index is not None and target_index != train_index + 1:
                self.notebook.insert(train_index + 1, self._ai_tab_container)
        except tk.TclError:
            pass

    def set_training_enabled(self, enabled: bool) -> None:
        """Compatibility hook; desktop training is always visible."""
        return None

    # ----- community tab (auth-gated) -----------------------------------------

    def _mount_community_tab(self) -> None:
        """Add the Community tab if not already present."""
        if self.community_tab is not None or self._community_container is not None:
            return
        self._community_container = self._add_lazy_scrolled(
            CommunityTab, "Community"
        )
        self.signin_var.set("Sign out")

    def _unmount_community_tab(self) -> None:
        """Remove the Community tab (called on logout)."""
        if self.community_tab is None:
            if self._community_container is None:
                return
        for tab_id in self.notebook.tabs():
            if self.notebook.tab(tab_id, "text") == "Community":
                self.notebook.forget(tab_id)
                break
        if self._community_container is not None:
            self._lazy_tabs.pop(str(self._community_container), None)
            self._community_container.destroy()
            self._community_container = None
        self.community_tab = None
        self.signin_var.set("Sign in")

    def _on_signin_click(self) -> None:
        if self.auth is None:
            self.set_status("Authentication unavailable.")
            return
        if self.auth.is_authenticated():
            try:
                self.auth.logout()
            except Exception as exc:
                self.set_status(f"Sign-out failed: {exc}")
                return
            self._unmount_community_tab()
            self.set_status("Signed out.")
            return
        from .dialog_login import LoginDialog
        LoginDialog(self.root, self)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _build_status_indicator(
        self, parent: tk.Misc, text_var: tk.StringVar,
    ) -> tk.Label:
        """[●][text] pair on the right side of the status bar.

        Returns the dot Label so callers can recolour it (green/red) when
        the underlying connection state flips.
        """
        frame = ttk.Frame(parent, style="StatusBar.TFrame")
        frame.pack(side=tk.RIGHT, padx=12, pady=6)
        dot = tk.Label(
            frame,
            text="●",  # BLACK CIRCLE
            background=PALETTE["bg_window"],
            foreground=PALETTE["error"],
            font=self.fonts["small"],
        )
        dot.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(frame, textvariable=text_var, style="Status.TLabel").pack(
            side=tk.LEFT,
        )
        return dot

    def _set_camera_indicator(self, message: str, *, connected: bool) -> None:
        self.camera_status_var.set(message)
        self.camera_dot.config(
            foreground=PALETTE["success" if connected else "error"],
        )

    def _set_serial_indicator(self, message: str, *, connected: bool) -> None:
        self.serial_status_var.set(message)
        self.serial_dot.config(
            foreground=PALETTE["success" if connected else "error"],
        )

    # ----- header gradient ----------------------------------------------------

    def _repaint_header(self, _event=None) -> None:
        """Repaint the bounded-item gradient immediately on resize."""
        canvas = self.header_canvas
        paint_gradient(
            canvas,
            color_a=PALETTE["bg_gradient_a"],
            color_b=PALETTE["bg_gradient_b"],
            direction="horizontal",
        )
        canvas.delete("title")
        canvas.create_text(
            18, HEADER_HEIGHT // 2,
            anchor=tk.W,
            text="AI Case Sorter",
            fill=PALETTE["text"],
            font=self.fonts["title"],
            tags="title",
        )
        # Place the subtitle to the right of the actual rendered title text.

    # ----- camera -------------------------------------------------------------

    def start_camera(self) -> None:
        try:
            ok = self.camera.start_preview()
            if not ok:
                self.set_status("Camera failed to start. Check device index in Camera tab.")
                self._set_camera_indicator("Camera: failed to start", connected=False)
            else:
                self._set_camera_indicator(
                    f"Camera: connected ({self.camera.width}x{self.camera.height})",
                    connected=True,
                )
                camera_info = self.camera.diagnostic_info()
                if (
                    camera_info.get("compatibility_mode")
                    and not self._shared_camera_warning_shown
                ):
                    self._shared_camera_warning_shown = True
                    source_format = str(
                        camera_info.get("source_format", "NV12/YUY2")
                    )
                    messagebox.showwarning(
                        "Windows camera setting",
                        "Windows shared-camera mode appears to be enabled. "
                        "Disable “Allow multiple apps to use camera at the "
                        "same time,” then restart the application.\n\n"
                        f"The camera is currently running in {source_format} "
                        "compatibility mode.",
                        parent=self.root,
                    )
        except Exception as exc:
            self.set_status(f"Camera error: {exc}")
            self._set_camera_indicator("Camera: error", connected=False)

    def stop_camera(self) -> None:
        self.camera.stop()
        self.set_status("Camera stopped.")
        self._set_camera_indicator("Camera: disconnected", connected=False)

    def restart_camera(
        self,
        device_index: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Recreate the Camera with the given (or saved) device + size."""
        self.camera.stop()
        cam_cfg = self.config.camera
        self.camera = Camera(
            device_index=int(device_index if device_index is not None
                             else cam_cfg.get("device_index", 0)),
            width=int(width if width is not None else cam_cfg.get("width", 1920)),
            height=int(height if height is not None else cam_cfg.get("height", 1080)),
        )
        self.start_camera()
        if self.run_controller is not None:
            self.run_controller.camera = self.camera

    def capture_frame(self):
        return self.camera.capture_frame()

    def set_camera_led(self, value: int, *, percent: float | None = None, source: str = "", persist: bool = False) -> None:
        """Queue a non-blocking LED preview or explicit persisted update."""
        value = max(0, min(255, int(value)))
        self._record_diagnostic(
            "record_led_request",
            value,
            percent,
            source + ("_saved" if persist else ""),
        )
        self.led_worker.submit(value, persist=persist)

    def saved_camera_led(self) -> int:
        """Return the persisted LED level used for startup initialization."""
        return max(0, min(255, int(self.config.serial.get("init_settings", {}).get("cameraledlevel", 15))))

    def _refresh_preview(self) -> None:
        """Render only the currently visible maintenance preview at 10 FPS.

        Capture remains at the camera's native 30 FPS and continuously replaces
        the one-slot latest frame. Hidden tabs no longer resize, colour-convert,
        and allocate Tk images for frames the operator cannot see.
        """
        frame = self.camera.latest_frame()
        if frame is not None:
            if self._maintenance_mode:
                try:
                    selected_text = self.notebook.tab(self.notebook.select(), "text")
                except (tk.TclError, AttributeError):
                    selected_text = ""
                if selected_text == "AI Config":
                    self.ai_tab.update_preview(frame)
                elif selected_text == "Camera":
                    self.camera_tab.refresh_preview(frame)
        self.root.after(int(1000 / PREVIEW_FPS), self._refresh_preview)

    # ----- serial -------------------------------------------------------------

    def _auto_connect_serial(self) -> None:
        """Try the saved port first, then walk available ports until one handshakes.

        Falls through to alternate ports if the saved port is present but
        unresponsive.
        """
        saved_port = (self.config.serial.get("port") or "").strip()
        if saved_port == EMULATED_PORT:
            self.connect_serial()
            return

        available = serial_broker.list_serial_ports()
        candidates: list[str] = []
        if saved_port and saved_port in available:
            candidates.append(saved_port)
        for port in available:
            if port not in candidates:
                candidates.append(port)

        if not candidates:
            self.set_status("Serial: no serial ports detected.")
            return

        baud = int(self.config.serial.get("baud", 9600))
        probe_timeout = float(
            self.config.serial.get(
                "handshake_timeout_s", serial_broker.HANDSHAKE_READ_TIMEOUT_S
            )
        )

        def _probe() -> tuple[object, str] | tuple[None, None]:
            for port in candidates:
                self.bus.post("status", f"Auto-connect: probing {port}…")
                # Opening the port asserts DTR which resets the Arduino, and
                # the board needs ~1-2 s to boot before it can answer
                # `version`. Probe timeout is configurable in Serial Config.
                broker = serial_broker.SerialBroker(
                    port=port,
                    baud=baud,
                    require_serial_ready=True,
                    handshake_timeout_s=probe_timeout,
                )
                if broker.try_open():
                    broker.start()
                    return broker, port
            return None, None

        self.set_status(
            f"Auto-connecting to serial — {len(candidates)} port(s) to try…"
        )
        self.run_worker(
            _probe,
            on_done=self._finalize_auto_connect,
            on_error=lambda exc: self.set_status(f"Auto-connect error: {exc}"),
        )

    def _finalize_auto_connect(self, result) -> None:
        broker, port = result
        if broker is None:
            self.set_status("Serial: no board responded on any port.")
            self._set_serial_indicator("Serial: disconnected", connected=False)
            return
        self._after_connect(broker, port, source="auto")

    def _after_connect(self, broker, port: str, *, source: str) -> None:
        """Wire callbacks, persist the port, optionally push init settings.

        Shared by auto-connect and the manual Connect button.
        """
        broker.on_received.append(lambda line: self.bus.post("serial/rx", line))
        broker.on_sent.append(lambda line: self.bus.post("serial/tx", line))
        broker.on_received.append(
            lambda line: self._record_diagnostic("record_serial", "RX", line)
        )
        broker.on_sent.append(
            lambda line: self._record_diagnostic("record_serial", "TX", line)
        )
        self.broker = broker
        if port != (self.config.serial.get("port") or ""):
            self.config.serial["port"] = port
            self.config.save()
        self._set_serial_indicator(
            f"Serial: connected ({port}) — {broker.firmware_version}",
            connected=True,
        )
        self.set_status(
            f"{'Auto-connected' if source == 'auto' else 'Connected'} to {port}."
        )
        self._rebuild_run_controller()

        # Match the known-good MSI behavior: the saved camera LED value is a
        # hardware setting and must be restored immediately after every board
        # connection/reset. This is independent of the optional full init push.
        saved_led = self.saved_camera_led()
        self._record_diagnostic(
            "record_led_request", saved_led, None, "serial_connect_restore"
        )
        broker.send_command(f"cameraledlevel:{saved_led}")
        self.set_status(f"Connected to {port}. Camera LED restored to {saved_led}.")

        if self.config.serial.get("init_on_startup", False):
            self.set_status(f"Connected to {port}. Pushing init settings…")
            settings = dict(self.config.serial.get("init_settings", {}))
            self.run_worker(
                lambda: broker.update_init_settings(settings),
                on_done=lambda _r: self.set_status(
                    f"Connected to {port}. Init settings pushed."
                ),
                on_error=lambda err: self.set_status(f"Init push failed: {err}"),
            )

    def connect_serial(self, port: str | None = None) -> None:
        """Open a single, explicit port. If port is None, use the saved value.

        Run on the Tk main thread; the open is synchronous because the user
        clicked Connect and is waiting for the result.
        """
        if self.broker is not None:
            try:
                self.broker.stop()
            except Exception:
                pass
            self.broker = None

        if port is None:
            port = (self.config.serial.get("port") or "").strip()
        if not port:
            self.set_status("Serial: no port selected.")
            self._set_serial_indicator("Serial: disconnected", connected=False)
            return

        if port == EMULATED_PORT:
            broker = EmulatorBroker()
            broker.try_open()
        else:
            broker = serial_broker.SerialBroker(
                port=port,
                baud=int(self.config.serial.get("baud", 9600)),
                require_serial_ready=True,
            )
            if not broker.try_open():
                self.set_status(f"Serial: failed to open {port}.")
                self._set_serial_indicator("Serial: disconnected", connected=False)
                return
            broker.start()

        self._after_connect(broker, port, source="manual")

    def disconnect_serial(self) -> None:
        if self.broker is not None:
            try:
                self.broker.stop()
            except Exception:
                pass
            self.broker = None
        self._set_serial_indicator("Serial: disconnected", connected=False)
        self.set_status("Serial disconnected.")

    def _rebuild_run_controller(self) -> None:
        if self.broker is None:
            return
        self.run_controller = RunController(
            config=self.config,
            broker=self.broker,
            camera=self.camera,
            bus=self.bus,
            db=self.db,
            diagnostics=self.diagnostics,
        )

    # ----- worker dispatch ----------------------------------------------------

    def run_worker(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `fn` in a daemon thread and post the result back via the bus."""

        topic_done = f"worker/done/{id(fn)}"
        topic_err = f"worker/err/{id(fn)}"

        if on_done is not None:
            self.bus.subscribe(topic_done, on_done)
        if on_error is not None:
            self.bus.subscribe(topic_err, on_error)

        def _run() -> None:
            try:
                result = fn()
                self.bus.post(topic_done, result)
            except Exception as exc:
                traceback.print_exc()
                self.bus.post(topic_err, exc)

        threading.Thread(target=_run, daemon=True).start()

    # ----- bus drain loop -----------------------------------------------------

    def _drain_bus(self) -> None:
        self.bus.drain(max_items=128)
        # Pump serial-log events into the serial tab.
        self.root.after(50, self._drain_bus)

    # ----- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        # Wire serial-log topics to the Serial tab now that the bus is alive.
        self.bus.subscribe("serial/rx", lambda line: self.serial_tab.append_log(f"<- {line}"))
        self.bus.subscribe("serial/tx", lambda line: self.serial_tab.append_log(f"-> {line}"))
        self.root.mainloop()

    def _on_close(self) -> None:
        if self.diagnostics is not None:
            if not self._disable_diagnostics():
                return
        try:
            if self.run_controller is not None:
                self.run_controller.stop()
        except Exception:
            pass
        try:
            if self.broker is not None:
                self.broker.stop()
        except Exception:
            pass
        try:
            self.led_worker.stop()
        except Exception:
            pass
        try:
            self.camera.stop()
        except Exception:
            pass
        self.root.destroy()
