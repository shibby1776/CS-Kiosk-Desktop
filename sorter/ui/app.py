"""Main Tk application shell — notebook + status bar + cross-thread wiring."""
from __future__ import annotations
import copy

import itertools
import threading
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from .. import classifier, serial_broker
from ..camera import Camera
from ..config import Config
from ..events import EventBus
from ..led_worker import LedCommandWorker
from ..run_controller import RunController
from ..serial_emulator import EmulatorBroker, EMULATED_PORT
from ..version import (
    APP_VERSION,
    PUBLIC_VERSION,
)
from .tab_ai import AiTab
from .tab_camera import CameraTab
from .tab_community import CommunityTab
from .tab_imageproc import ImageProcTab
from .tab_models import ModelsTab
from .tab_server import ServerTab
from .tab_run import RunTab
from .tab_serial import SerialTab
from .tab_train import TrainTab
from .tab_web_access import WebAccessTab
from .kiosk import KioskDashboard
from .theme import PALETTE, apply_theme, paint_gradient
from .widgets import ScrollableFrame
from ..catch_all_report import CatchAllReport


PREVIEW_FPS = 10
HEADER_HEIGHT = 36


class MainWindow:
    def __init__(
        self,
        config: Config,
        db: Any | None = None,
        crash_reporter: Any | None = None,
        web_only: bool = False,
    ) -> None:
        self.config = config
        self.db = db if db is not None else getattr(config, "db", None)
        self.crash_reporter = crash_reporter
        self.web_only = bool(web_only)
        self.web_server = None
        self.api_server_runtime = None
        self._training_manager = None
        self.server_tab = None
        self._pending_server_model_id = None
        self.root = tk.Tk()
        self.root.report_callback_exception = self._on_tk_callback_exception
        self.bus = EventBus(error_handler=self._on_bus_handler_error)
        self.catch_all_report = CatchAllReport(self.bus)
        self.bus.subscribe("serial/disconnected", self._on_serial_disconnected)
        self.bus.subscribe("serial/recovering", self._on_serial_recovering)
        self.bus.subscribe("serial/recovered", self._on_serial_recovered)
        self._worker_tokens = itertools.count()
        self._feedback_prepare_pending = False
        self._error_prompt_scheduled = False
        self._error_prompt_deferred = False
        if self.crash_reporter is not None:
            self.crash_reporter.set_notification_callback(
                lambda: self.bus.post("crash/report_available", None)
            )
            self.bus.subscribe(
                "crash/report_available",
                lambda _payload: self._schedule_error_report_prompt(),
            )
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
        self._local_camera = None
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
        self._server_tab_container = _add_lazy_scrolled(ServerTab, "Server")
        self._web_access_tab_container = _add_lazy_scrolled(
            WebAccessTab, "LAN Access"
        )
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

        # Apply initial visibility. Remote runtime always hides training;
        # local runtime follows the saved Full / Classification Only profile.
        self._apply_ai_config_visibility()
        self._apply_training_visibility()
        self.bus.subscribe("mode/changed", lambda _payload: self._apply_mode_visibility())
        self.bus.subscribe("profile/changed", lambda _payload: self._apply_training_visibility())

        self.bus.subscribe("status", self.set_status)
        # Browser worker threads marshal hardware/UI-owner work through this
        # one queue.  The web layer never opens a second camera, serial port,
        # run controller, database, or training backend.
        self.bus.subscribe("web/action", self._complete_web_action)

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
        if (
            self.crash_reporter is not None
            and self.crash_reporter.pending_from_previous_session
        ):
            self.root.after(800, self._schedule_error_report_prompt)
        self.root.after(350, self._apply_web_interface_startup)
        # Repaint legacy Tk widgets after the complete tree exists. This is
        # required on Raspberry Pi OS, where native desktop defaults can leak
        # light backgrounds into Canvas/Text/Listbox controls.
        # Reassert a few times after startup so Openbox/native defaults cannot
        # repaint the window light. Do not bind this to <<ThemeChanged>>:
        # reapplying a ttk theme from that event can recursively emit the same
        # event and prevent the application from finishing startup.

    # ----- desktop shell ------------------------------------------------------

    def dispatch_web_action(self, fn: Callable[[], Any], *, timeout: float = 20.0) -> Any:
        """Run a web-requested owner action on the Tk/backend owner thread."""
        done = threading.Event()
        envelope: dict[str, Any] = {"fn": fn, "done": done}
        self.bus.post("web/action", envelope)
        if not done.wait(max(0.5, float(timeout))):
            raise RuntimeError("The Windows sorter backend did not complete the action.")
        error = envelope.get("error")
        if isinstance(error, BaseException):
            raise error
        return envelope.get("result")

    @staticmethod
    def _complete_web_action(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            fn = payload.get("fn")
            if not callable(fn):
                raise ValueError("Invalid Web Interface action.")
            payload["result"] = fn()
        except BaseException as exc:
            payload["error"] = exc
        finally:
            done = payload.get("done")
            if isinstance(done, threading.Event):
                done.set()

    def _apply_web_interface_startup(self) -> None:
        """Apply persisted Desktop/Web/Both presentation policy."""
        from ..web_settings import WebSettings

        settings = WebSettings.load(self.config.settings)
        should_start = bool(settings.enabled or self.web_only)
        if should_start:
            try:
                self.start_web_interface(settings)
            except Exception as exc:
                self.set_status(f"Web Interface unavailable: {exc}")
                # Explicit Web-only startup must fail visibly instead of
                # leaving an invisible backend with no usable presentation.
                self.root.deiconify()
                self.web_only = False
                return
        if should_start and (self.web_only or not settings.desktop_enabled):
            self.root.withdraw()
            self.web_only = True
            try:
                webbrowser.open(self.web_server.address)
            except Exception:
                pass

    def start_web_interface(self, settings=None) -> str:
        from ..web_interface import WebInterfaceServer
        from ..web_settings import WebSettings

        if self.web_server is not None:
            return self.web_server.address
        resolved = settings or WebSettings.load(self.config.settings)
        server = WebInterfaceServer(self, resolved)
        try:
            server.start()
        except Exception:
            server.stop()
            raise
        self.web_server = server
        self.set_status(f"Web Interface available at {server.address}")
        return server.address

    def stop_web_interface(self, *, force: bool = False) -> None:
        server = self.web_server
        if server is None:
            return
        if not force and server.operations.web_training_active:
            raise RuntimeError(
                "Finish or cancel browser-started training before disabling LAN Access."
            )
        self.web_server = None
        server.stop()

    def request_restart_in_desktop(self) -> None:
        """Restore the already-running Desktop presentation safely."""
        from ..web_settings import WebSettings

        current = WebSettings.load(self.config.settings)
        WebSettings(
            enabled=current.enabled,
            desktop_enabled=True,
            sorter_name=current.sorter_name,
            port=current.port,
        ).save(self.config.settings)
        self.web_only = False
        self.root.deiconify()
        self.root.lift()
        self.show_kiosk()

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
        view_menu.add_separator()
        view_menu.add_command(
            label="Enable Diagnostics",
            accelerator="Ctrl+D",
            command=self._toggle_diagnostics,
        )
        self._diagnostics_menu = view_menu
        self._diagnostics_menu_index = view_menu.index("end")
        menu.add_cascade(label="View", menu=view_menu)

        account_menu = tk.Menu(menu, tearoff=False)
        account_menu.add_command(label="Sign in / Sign out", command=self._on_signin_click)
        menu.add_cascade(label="Account", menu=account_menu)

        menu.add_command(label="About", command=self._show_about)

        self.root.configure(menu=menu)
        self.root.bind_all("<Control-o>", lambda _event: self.show_kiosk())
        self.root.bind_all("<Control-m>", lambda _event: self.show_maintenance())
        self.root.bind_all("<Control-q>", lambda _event: self._on_close())
        # Tk class bindings for an Entry/Text can consume Ctrl+D before the
        # normal ``all`` bindtag runs. Install an application bindtag first on
        # every mapped widget so Diagnostics remains available regardless of
        # focus. Keep bind_all as a fallback for unusual/custom widgets.
        self._diagnostics_shortcut_tag = "ShibbyDiagnosticsShortcut"
        self.root.bind_class(
            self._diagnostics_shortcut_tag,
            "<Control-KeyPress-d>",
            self._toggle_diagnostics,
        )
        self.root.bind_class(
            self._diagnostics_shortcut_tag,
            "<Control-KeyPress-D>",
            self._toggle_diagnostics,
        )
        self.root.bind_all(
            "<Map>", self._install_diagnostics_shortcut_tag, add="+"
        )
        self._install_diagnostics_shortcut_tag(self.root)
        self.root.bind_all("<Control-d>", self._toggle_diagnostics)
        self.root.bind_all("<F11>", self._toggle_fullscreen)
        self._desktop_fullscreen = False

    def _install_diagnostics_shortcut_tag(self, event_or_widget=None) -> None:
        """Give Ctrl+D priority over focused Entry/Text class bindings."""
        widget = getattr(event_or_widget, "widget", event_or_widget) or self.root
        try:
            tags = tuple(widget.bindtags())
            tag = self._diagnostics_shortcut_tag
            if tag not in tags:
                widget.bindtags((tag, *tags))
        except (AttributeError, tk.TclError):
            return

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
        self._diagnostics_menu.entryconfigure(
            self._diagnostics_menu_index, label="Disable Diagnostics"
        )
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
        self._diagnostics_menu.entryconfigure(
            self._diagnostics_menu_index, label="Enable Diagnostics"
        )
        self.set_status("Diagnostics disabled.")
        return True

    def _record_diagnostic(self, method: str, *args) -> None:
        collector = self.diagnostics
        if collector is not None:
            getattr(collector, method)(*args)

    def _record_runtime_event(self, event: str, detail: str = "") -> None:
        reporter = self.crash_reporter
        if reporter is not None:
            reporter.record_event(event, detail)

    def _record_runtime_exception(
        self,
        exc: BaseException,
        *,
        source: str,
        notify: bool,
    ) -> None:
        reporter = self.crash_reporter
        if reporter is not None:
            reporter.record_exception(
                type(exc), exc, exc.__traceback__, source=source, notify=notify
            )

    def _on_tk_callback_exception(
        self, exc_type: type[BaseException], exc_value: BaseException, tb: Any
    ) -> None:
        reporter = self.crash_reporter
        if reporter is not None:
            reporter.record_exception(
                exc_type, exc_value, tb, source="tk_callback", notify=True
            )
        traceback.print_exception(exc_type, exc_value, tb)

    def _on_bus_handler_error(self, topic: str, exc: Exception) -> None:
        self._record_runtime_exception(
            exc, source=f"event_bus:{topic}", notify=True
        )

    def _schedule_error_report_prompt(self) -> None:
        if self._error_prompt_scheduled or self._error_prompt_deferred:
            return
        reporter = self.crash_reporter
        if reporter is None or not reporter.has_pending_report:
            return
        self._error_prompt_scheduled = True
        self.root.after_idle(self._show_error_report_prompt)

    def _show_error_report_prompt(self) -> None:
        self._error_prompt_scheduled = False
        reporter = self.crash_reporter
        if reporter is None or not reporter.has_pending_report:
            return
        create = messagebox.askyesno(
            "Create Error Report",
            "The application detected an unexpected error or an unclean "
            "shutdown. Would you like to create a diagnostic ZIP for support?\n\n"
            "The report is saved locally and is not uploaded automatically.",
            parent=self.root,
        )
        if not create:
            self._error_prompt_deferred = True
            self._record_runtime_event("error_report_deferred")
            self.set_status("Error report available; it can be created after restart.")
            return

        default = f"ShibbyPrints-Error-{datetime.now():%Y-%m-%d-%H%M%S}.zip"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save Error Report",
            defaultextension=".zip",
            initialfile=default,
            filetypes=[("ZIP archive", "*.zip")],
        )
        if not path:
            self._error_prompt_deferred = True
            return
        try:
            camera_info = self._camera_diagnostic_info()
            if self.diagnostics is not None:
                exported = self.diagnostics.export_zip(
                    path,
                    camera_info=camera_info,
                    app_info=self._diagnostic_app_info(),
                    extra_members=reporter.export_members(),
                )
                reporter.clear_pending()
            else:
                exported = reporter.export_zip(
                    path,
                    camera_info=camera_info,
                    config_summary=self._redacted_configuration_summary(),
                )
        except Exception as exc:
            self._record_runtime_exception(
                exc, source="error_report_export", notify=False
            )
            messagebox.showerror(
                "Export failed", str(exc), parent=self.root
            )
            return
        messagebox.showinfo(
            "Error report created",
            f"Saved to:\n{exported}\n\nSend this ZIP to support for review.",
            parent=self.root,
        )
        self.set_status(f"Error report created: {exported.name}")

    def _camera_diagnostic_info(self) -> dict[str, Any]:
        try:
            return dict(self.camera.diagnostic_info())
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _diagnostic_app_info(self) -> dict[str, Any]:
        from ..transports.network import refresh_disconnected_report
        previous=refresh_disconnected_report(getattr(self,"_last_disconnect_transport",None))
        return {
            "build": f"{PUBLIC_VERSION}; application version {APP_VERSION}",
            "public_version": PUBLIC_VERSION,
            "app_version": APP_VERSION,
            "config_camera": dict(self.config.camera),
            "brightness_mapping": {
                "mode": "direct_raw_0_255",
                "startup_restore": True,
            },
            "inference_runtime": self._inference_runtime_diagnostic_info(),
            "integrated_api_server": self._api_server_diagnostic_info(),
            "lan_access": self._web_interface_diagnostic_info(),
            "connection_transport": self.broker.report() if hasattr(self.broker,"report") else ((previous or {"type":"serial"}) if self.broker is None else {"type":"serial","connected":bool(getattr(self.broker,"is_connected",False))}),
            "last_disconnect_transport": previous,
        }

    def _inference_runtime_diagnostic_info(self) -> dict[str, Any]:
        """Probe Torch only for an explicit diagnostic export."""
        try:
            import torch

            available = bool(torch.cuda.is_available())
            result: dict[str, Any] = {
                "torch_version": str(torch.__version__),
                "compiled_cuda": getattr(torch.version, "cuda", None),
                "cuda_available": available,
                "selected_device": "cuda" if available else "cpu",
            }
            if available:
                result["gpu_name"] = str(torch.cuda.get_device_name(0))
            return result
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _web_interface_diagnostic_info(self) -> dict[str, Any]:
        """Capture LAN state on demand without starting the Web Interface."""
        try:
            from ..network_info import local_ipv4_addresses
            from ..web_settings import WebSettings

            settings = WebSettings.load(self.config.settings)
            server = self.web_server
            advertiser = getattr(server, "_mdns", None) if server else None
            return {
                "enabled": settings.enabled,
                "sorter_name": settings.sorter_name,
                "port": settings.port,
                "runtime_constructed": server is not None,
                "runtime_alive": bool(
                    server
                    and getattr(server, "_thread", None)
                    and server._thread.is_alive()
                ),
                "friendly_url": (
                    f"http://{settings.sorter_name}.local:{settings.port}"
                ),
                "ip_urls": [
                    f"http://{address}:{settings.port}"
                    for address in local_ipv4_addresses()
                ],
                "mdns_active": bool(advertiser and advertiser.active),
                "mdns_error": str(getattr(advertiser, "error", "") or ""),
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _api_server_diagnostic_info(self) -> dict[str, Any]:
        """Build a redacted snapshot only when a support export is requested."""
        if self.db is None:
            return {"available": False}
        try:
            from ..api_server_settings import ApiServerSettingsRepo
            from ..repository import ApiModelAliasRepo, ModelRepo

            settings = ApiServerSettingsRepo(self.db).load()
            models = ModelRepo(self.db)
            assignments = []
            for item in ApiModelAliasRepo(self.db).list():
                model = models.get(item.model_id)
                assignments.append({
                    "alias": item.alias,
                    "model": model.name if model else "missing",
                    "preload": bool(item.preload),
                })
            runtime = (
                self.api_server_runtime.snapshot()
                if self.api_server_runtime is not None
                else {"state": "not_constructed"}
            )
            return {
                "api_key_required": bool(settings.api_key_hash),
                "host": settings.host,
                "port": settings.port,
                "on_demand_cache_slots": settings.on_demand_cache_slots,
                "remote_queue_limit": settings.remote_queue_limit,
                "assignments": assignments,
                "runtime": runtime,
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _redacted_configuration_summary(self) -> dict[str, Any]:
        api = self.config.api
        serial = self.config.serial
        camera = self.config.camera
        api_key = str(api.get("api_key") or "").strip()
        active_model_id = self.config.settings.get_active_model_id()
        active_model: dict[str, Any] | None = None
        if active_model_id is not None and self.db is not None:
            try:
                from ..repository import ModelRepo

                model = ModelRepo(self.db).get(active_model_id)
                if model is not None:
                    active_model = {
                        "name": model.name,
                        "model_mode": model.model_mode,
                        "model_type": model.model_type,
                        "model_version": model.model_version,
                        "checkpoint": "present" if model.model_path else "not set",
                    }
            except Exception as exc:
                active_model = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "operating_mode": "local" if active_model_id is not None else "remote_api",
            "active_model": active_model,
            "api": {
                "model": api.get("model"),
                "api_key": "set" if api_key and api_key.casefold() != "nokey" else "not set",
            },
            "serial": {
                "port": serial.get("port"),
                "baud": serial.get("baud"),
                "slot_quantity": serial.get("slot_quantity"),
                "init_on_startup": serial.get("init_on_startup"),
            },
            "camera": {
                "device_index": camera.get("device_index"),
                "device_chosen": camera.get("device_chosen"),
                "width": camera.get("width"),
                "height": camera.get("height"),
            },
            "run_options": {
                "confidence_floor": self.config.run_confidence_floor,
                "store_images": self.config.run_store_images,
                "package_mode": self.config.run_package_mode,
                "package_size": self.config.run_package_size,
                "auto_select_trays": self.config.run_auto_select_trays,
            },
            "integrated_api_server": self._api_server_diagnostic_info(),
        }

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
            text=PUBLIC_VERSION,
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
        self._record_runtime_event("slot_assignments_cleared")
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
        if not classifier.uses_local_backend(self.db) and (
            not self.config.api.get("model")
        ):
            messagebox.showerror(
                "AI not configured",
                "Set endpoint and model on the AI Config tab first; enter an API key if your server requires one.",
                parent=self.root,
            )
            return
        if controller.is_running:
            controller.stop()
        else:
            problem = classifier.checkpoint_problem(self.db)
            if problem is not None:
                messagebox.showerror("Model not ready", problem, parent=self.root)
                return
            self._prepare_feedback_then(
                controller,
                lambda: controller.start(),
                action="start sorting",
            )

    def _prepare_feedback_then(
        self,
        controller: RunController,
        callback: Callable[[], None],
        *,
        action: str,
    ) -> None:
        """Refresh community feedback policy without blocking the Tk thread."""
        if self._feedback_prepare_pending:
            self.set_status("Community feedback settings are already refreshing…")
            return
        self._feedback_prepare_pending = True
        self.set_status("Refreshing community feedback settings…")

        def done(settings: Any) -> None:
            self._feedback_prepare_pending = False
            if controller is not self.run_controller:
                return
            if settings is not None and getattr(settings, "blocked", False):
                self.set_status(
                    "Feedback capture is paused by the community server; "
                    f"continuing to {action}."
                )
            callback()

        def failed(_exc: Exception) -> None:
            self._feedback_prepare_pending = False
            if controller is self.run_controller:
                controller.clear_community_feedback()
                callback()

        self.run_worker(
            lambda: controller.refresh_community_feedback(auth=self.auth),
            on_done=done,
            on_error=failed,
        )

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
        if not classifier.uses_local_backend(self.db) and (
            not self.config.api.get("model")
        ):
            messagebox.showerror(
                "AI not configured",
                "Set endpoint and model on the AI Config tab first; enter an API key if your server requires one.",
                parent=self.root,
            )
            return
        problem = classifier.checkpoint_problem(self.db)
        if problem is not None:
            messagebox.showerror("Model not ready", problem, parent=self.root)
            return
        self._prepare_feedback_then(
            controller,
            lambda: self.run_worker(controller.cycle_once),
            action="feed one case",
        )

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
        self.kiosk._on_assignment_changed(force=True)
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
            if (
                self.run_tab is not None
                and tab_id == str(self._run_tab_container)
            ):
                self.run_tab.refresh_if_dirty()
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
        elif label == "Server":
            self.server_tab = tab
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

    def _apply_mode_visibility(self) -> None:
        self._apply_ai_config_visibility()
        self._apply_training_visibility()

    def _apply_training_visibility(self) -> None:
        """Show Train only for a local model in the Full software profile."""
        visible = bool(self.config.training_tools_visible)
        try:
            if visible:
                self.notebook.add(self._train_tab_container, text="Train")
                # Keep Train immediately after Models.
                tabs = self.notebook.tabs()
                model_index = next(
                    (i for i, tid in enumerate(tabs)
                     if self.notebook.tab(tid, "text") == "Models"),
                    None,
                )
                if model_index is not None:
                    self.notebook.insert(model_index + 1, self._train_tab_container)
            else:
                self.notebook.hide(self._train_tab_container)
        except tk.TclError:
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
        """Compatibility hook used by model-selection surfaces."""
        self.config.set_software_profile("full" if enabled else "classification_only")
        self._apply_training_visibility()

    # ----- integrated inference API server ----------------------------------

    def get_training_manager(self):
        """Return the one application-owned training subprocess manager.

        It remains unconstructed until either the desktop Train tab or the
        browser Train page is actually used.  Both interfaces therefore share
        one subprocess owner and cannot start competing training jobs.
        """
        if self._training_manager is None:
            from ..training.manager import TrainingManager

            self._training_manager = TrainingManager(self.bus)
        return self._training_manager

    def get_api_server_runtime(self):
        if self.api_server_runtime is None:
            from ..api_server import ApiServerRuntime
            self.api_server_runtime = ApiServerRuntime(
                self.db,
                bus=self.bus,
                auth_provider=lambda: self.auth,
                crash_reporter=self.crash_reporter,
            )
        return self.api_server_runtime

    def show_server_tab(self, model_id: int | None = None) -> None:
        """Open Maintenance → Server and optionally begin assigning a model."""
        already_constructed = self.server_tab is not None
        self._pending_server_model_id = model_id
        self.show_maintenance()
        self.notebook.select(self._server_tab_container)
        self._on_notebook_tab_changed()
        if already_constructed and self.server_tab is not None and model_id is not None:
            self._pending_server_model_id = None
            self.server_tab.assign_model(model_id)

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

    def detect_cameras(self):
        from ..transports.network import NetworkBroker
        if isinstance(self.broker, NetworkBroker):
            info=self.camera.diagnostic_info()
            return [{'index':0,'name':'Sorter camera','resolutions':[(info['width'],info['height'])], 'vid':'','pid':''}]
        if str(self.config.serial.get('port','')).lower().startswith(('http://','https://')):
            return [{'index':0,'name':'Sorter camera','resolutions':[(1280,720)],'vid':'','pid':''}]
        from ..camera import list_cameras_with_metadata
        return list_cameras_with_metadata()

    def import_connection_settings(self, data):
        if self.broker is not None and getattr(self.broker,'is_connected',False):
            raise RuntimeError('Disconnect before importing a connection profile.')
        from ..transports.migration import import_profile
        result=import_profile(self.config,data)
        self._sync_connection_forms()
        return result

    def _sync_connection_forms(self):
        self.serial_tab.load_connection_form()
        led=self.saved_camera_led()
        self.serial_tab.init_widgets['cameraledlevel'].set(led)
        self.ai_tab.endpoint_var.set(self.config.api['endpoint_url'])
        self.ai_tab.model_var.set(self.config.api['model']);self.ai_tab.apikey_var.set(self.config.api.get('api_key',''))
        self.ai_tab._refresh_list()
        if self.imageproc_tab is not None:
            ip=self.config.image_proc;tab=self.imageproc_tab
            tab._saved_led_raw=led;tab._current_led_raw=led
            tab.led_scale.set(led);tab.led_value_var.set(f'{led}  [saved]')
            tab.primer_mode_var.set(ip['primer_mode']);tab.primer_radius.set(ip['primer_radius'])
            for key,attr in [('dp','hough_dp'),('min_dist','hough_min_dist'),('param1','hough_p1'),('param2','hough_p2'),('min_radius','hough_min_r'),('max_radius','hough_max_r')]:getattr(tab,attr).set(ip['hough'][key])
        self.bus.post('mode/changed',{'active_model_id':self.config.settings.get_active_model_id()})
        self.bus.post('run/headstamps_synced',{})
        self.bus.post('run/assignment_changed',{'full_refresh':True})
    def ensure_connection_idle(self):
        tab=getattr(self,'diagnostics_tab',None)
        if tab is not None and getattr(tab,'sensor_test_active',False):
            raise RuntimeError('Finish or cancel the sensor diagnostic before changing sorter connections.')
        operations=getattr(getattr(self,'web_server',None),'operations',None)
        diagnostic=getattr(operations,'_web_diagnostics',None)
        if diagnostic is not None and (diagnostic.busy or diagnostic.phase=='awaiting_cases'):
            raise RuntimeError('Finish or cancel the sensor diagnostic before changing sorter connections.')
        controller=self.run_controller
        if controller is not None and (controller.is_running or controller.operation_busy):
            raise RuntimeError('Stop the current operation before changing sorter connections.')
        broker=self.broker
        if broker is not None and getattr(broker,'run_active',False):
            raise RuntimeError('Wait for the current controller operation to finish.')

    def select_sorter_profile(self,name):
        self.ensure_connection_idle()
        from ..sorter_profiles import SorterProfiles
        profiles=SorterProfiles(self.config)
        if name not in profiles.names(): raise ValueError('Saved sorter profile not found.')
        self.disconnect_serial()
        self.camera.stop()
        self.run_controller=None
        profiles.select(name)
        self._sync_connection_forms()
        self.serial_tab.profile_var.set(name)
        self.reset_run_counters()
        self.set_status('Sorter profile loaded. Connect and prepare the machine before sorting.')

    def start_camera(self) -> None:
        if self.broker is None and str(self.config.serial.get('port','')).lower().startswith(('http://','https://')):
            self._set_camera_indicator('Camera: connect sorter first',connected=False);return
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
                self._record_runtime_event(
                    "camera_started",
                    (
                        f"backend={camera_info.get('backend_name')};"
                        f"mode={camera_info.get('width')}x{camera_info.get('height')};"
                        f"fps={camera_info.get('fps')};"
                        f"format={camera_info.get('fourcc')}"
                    ),
                )
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
            self._record_runtime_exception(
                exc, source="camera_start", notify=False
            )
            self.set_status(f"Camera error: {exc}")
            self._set_camera_indicator("Camera: error", connected=False)

    def stop_camera(self) -> None:
        self.camera.stop()
        self._record_runtime_event("camera_stopped")
        self.set_status("Camera stopped.")
        self._set_camera_indicator("Camera: disconnected", connected=False)

    def restart_camera(
        self,
        device_index: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Recreate the camera through the connected transport."""
        from ..transports.network import NetworkBroker, NetworkCamera, validate_camera_mode
        if isinstance(self.broker, NetworkBroker):
            validate_camera_mode(self.broker, width, height)
            self.camera.stop()
            self.camera = NetworkCamera(self.broker,settle_ms=int(self.config.image_proc.get("settle_ms",150)))
            self.start_camera()
            if self.run_controller is not None:self.run_controller.camera = self.camera
            return
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
        if self.web_only:
            # Web Interface fetches the authoritative latest frame only while
            # a browser preview is visible. Do not copy frames for the hidden
            # Desktop presentation.
            self.root.after(1000, self._refresh_preview)
            return
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
        if saved_port.lower().startswith(('http://','https://')):
            self.connect_serial();return
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
                broker = serial_broker.create_broker(
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
        on_disconnect = getattr(broker, "on_disconnect", None)
        if isinstance(on_disconnect, list):
            on_disconnect.append(
                lambda reason: self.bus.post("serial/disconnected", reason)
            )
        on_recovering = getattr(broker, "on_recovering", None)
        if isinstance(on_recovering, list):
            on_recovering.append(lambda reason: self.bus.post("serial/recovering", reason))
        on_recovered = getattr(broker, "on_recovered", None)
        if isinstance(on_recovered, list):
            on_recovered.append(lambda result: self.bus.post("serial/recovered", result))
        broker.on_received.append(
            lambda line: self._record_diagnostic("record_serial", "RX", line)
        )
        broker.on_sent.append(
            lambda line: self._record_diagnostic("record_serial", "TX", line)
        )
        self.broker = broker
        remote=str(port).lower().startswith(('http://','https://'))
        self.config.serial['wifi_enabled']=remote
        if remote:self.config.serial['wifi_address']=port
        else:self.config.serial['usb_port']=port
        self.config.save()
        self.serial_tab.load_connection_form()
        from ..transports.network import NetworkBroker, NetworkCamera
        if isinstance(broker, NetworkBroker):
            if not isinstance(self.camera, NetworkCamera):
                self.camera.stop();self._local_camera = self.camera
            self.camera = NetworkCamera(broker,settle_ms=int(self.config.image_proc.get("settle_ms",150)))
            self.config.camera.update(device_index=0,width=self.camera.width,height=self.camera.height,device_chosen=True)
            self.config.save()
            self.camera.start_preview()
        elif isinstance(self.camera, NetworkCamera):
            self.camera.stop()
            self.camera = self._local_camera or Camera()
            self._local_camera = None
            self.start_camera()
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
        self._record_runtime_event(
            "serial_connected",
            f"source={source};port={port};firmware={broker.firmware_version}",
        )
        self._rebuild_run_controller()

        restart_notice=str(getattr(broker,'restart_notice','') or '')
        if restart_notice:
            self._record_runtime_event('esp_restart_detected',restart_notice)

        # Match the known-good MSI behavior: the saved camera LED value is a
        # hardware setting and must be restored immediately after every board
        # connection/reset. This is independent of the optional full init push.
        saved_led = self.saved_camera_led()
        self._record_diagnostic(
            "record_led_request", saved_led, None, "serial_connect_restore"
        )
        broker.send_command(f"cameraledlevel:{saved_led}")
        if restart_notice:
            self.set_status(restart_notice+'; connection restored. Inspect the machine before sorting.')
        else:
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

    def _on_serial_recovering(self, reason: Any = None) -> None:
        self._set_serial_indicator("Serial: USB bridge recovering", connected=False)
        self.set_status("USB bridge recovering — current operation will not be retried.")
        self._record_runtime_event("usb_bridge_recovering", str(reason or ""))

    def _on_serial_recovered(self, result: Any = None) -> None:
        port=str(getattr(self.broker,"port","") or self.config.serial.get("port") or "")
        self._set_serial_indicator(f"Serial: connected ({port})", connected=True)
        self.set_status("USB bridge recovered. Inspect the machine before restarting sorting.")
        self._record_runtime_event("usb_bridge_recovered", str(result or ""))

    def _on_serial_disconnected(self, reason: Any = None) -> None:
        """Stop safely when a connected board disappears unexpectedly."""
        detail = str(reason or "link lost")
        port = str(
            getattr(self.broker, "port", "")
            or self.config.serial.get("port")
            or ""
        )
        controller, self.run_controller = self.run_controller, None
        was_running = bool(controller is not None and controller.is_running)
        if controller is not None:
            controller.stop()
        broker, self.broker = self.broker, None
        if broker is not None:
            if hasattr(broker,"report"):
                try:
                    # Freeze before stop/Session.close; retained after reconnect
                    # and included in the application's normal diagnostic ZIP.
                    self._last_disconnect_transport=copy.deepcopy(broker.report())
                except Exception as exc:
                    self._last_disconnect_transport={"type":"esp","error":detail,"collection_error":str(exc)}
            try:
                broker.stop()
            except Exception:
                pass
        self._set_serial_indicator(
            f"Serial: disconnected ({port})" if port else "Serial: disconnected",
            connected=False,
        )
        self._record_runtime_event("serial_link_lost", detail)
        self.set_status(f"Serial disconnected — {detail}")
        if was_running:
            self.root.after(
                0,
                lambda: messagebox.showerror(
                    "Serial disconnected",
                    "The board stopped responding, so sorting was stopped.\n\n"
                    "Check the cable and board power, then reconnect before "
                    "starting another run.",
                    parent=self.root,
                ),
            )

    def connect_serial(self, port: str | None = None) -> None:
        """Open a single, explicit port. If port is None, use the saved value.

        Run on the Tk main thread; the open is synchronous because the user
        clicked Connect and is waiting for the result.
        """
        try:self.ensure_connection_idle()
        except RuntimeError as exc:self.set_status(str(exc));return
        if self.run_controller is not None and (self.run_controller.is_running or getattr(self.run_controller,'operation_busy',False)):
            self.set_status('Stop the current operation before changing the connection.');return
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
            broker = serial_broker.create_broker(
                port=port,
                baud=int(self.config.serial.get("baud", 9600)),
                require_serial_ready=True,
            )
            if not broker.try_open():
                self.set_status(getattr(broker,"error","") or f"Serial: failed to open {port}.")
                self._set_serial_indicator("Serial: disconnected", connected=False)
                return
            broker.start()

        self._after_connect(broker, port, source="manual")

    def disconnect_serial(self) -> None:
        try:self.ensure_connection_idle()
        except RuntimeError as exc:self.set_status(str(exc));return
        if self.run_controller is not None and (self.run_controller.is_running or getattr(self.run_controller,'operation_busy',False)):
            self.set_status('Stop the current operation before changing the connection.');return
        if self.broker is not None:
            try:
                self.broker.stop()
            except Exception:
                pass
            self.broker = None
        self._set_serial_indicator("Serial: disconnected", connected=False)
        self._record_runtime_event("serial_disconnected")
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
            crash_reporter=self.crash_reporter,
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

        # Never key replies on id(fn): CPython can reuse a freed function's
        # address, delivering a later result to an old callback.  Monotonic
        # tokens plus one-shot subscriptions make each worker independent.
        token = next(self._worker_tokens)
        topic_done = f"worker/done/{token}"
        topic_err = f"worker/err/{token}"

        def _unsubscribe() -> None:
            self.bus.unsubscribe(topic_done, _deliver_done)
            self.bus.unsubscribe(topic_err, _deliver_err)

        def _deliver_done(payload: Any) -> None:
            _unsubscribe()
            if on_done is not None:
                on_done(payload)

        def _deliver_err(exc: Any) -> None:
            _unsubscribe()
            if on_error is not None:
                on_error(exc)

        self.bus.subscribe(topic_done, _deliver_done)
        self.bus.subscribe(topic_err, _deliver_err)

        def _run() -> None:
            try:
                result = fn()
                self.bus.post(topic_done, result)
            except Exception as exc:
                if not getattr(exc,'expected_disconnect',False):
                    traceback.print_exc()
                    self._record_runtime_exception(
                        exc,
                        source=f"worker:{getattr(fn, '__name__', type(fn).__name__)}",
                        notify=on_error is None,
                    )
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
            manager = self._training_manager
            if manager is not None and manager.is_running:
                manager.cancel()
                manager.wait(timeout=6.0)
        except Exception:
            pass
        try:
            self.stop_web_interface(force=True)
        except Exception:
            pass
        try:
            if self.api_server_runtime is not None:
                self.api_server_runtime.stop()
        except Exception:
            pass
        try:
            from .. import local_inference
            local_inference.shutdown()
        except Exception:
            pass
        try:
            self.camera.stop()
        except Exception:
            pass
        self.root.destroy()
