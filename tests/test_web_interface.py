import hashlib
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from sorter.network_info import primary_local_ipv4_address
from sorter.web_settings import WebSettings, normalize_sorter_name
from sorter.windows_usb import removable_usb_roots


ROOT = Path(__file__).resolve().parents[1]


class _Settings:
    def __init__(self) -> None:
        self.value = None

    def get(self, _key, default=None):
        return self.value if self.value is not None else default

    def set(self, _key, value) -> None:
        self.value = value


class WebSettingsTests(unittest.TestCase):
    def test_disabled_defaults_create_no_worker(self) -> None:
        repo = _Settings()
        before = {thread.ident for thread in threading.enumerate()}
        settings = WebSettings.load(repo)
        after = {thread.ident for thread in threading.enumerate()}

        self.assertFalse(settings.enabled)
        self.assertTrue(settings.desktop_enabled)
        self.assertEqual(before, after)

    def test_legacy_password_fields_are_not_preserved(self) -> None:
        repo = _Settings()
        repo.value = {
            "enabled": True,
            "sorter_name": "sorter-2",
            "port": 8080,
            "password_salt": "legacy",
            "password_hash": "legacy",
        }
        loaded = WebSettings.load(repo)
        loaded.save(repo)

        self.assertNotIn("password_salt", repo.value)
        self.assertNotIn("password_hash", repo.value)

    def test_sorter_name_is_dns_safe(self) -> None:
        self.assertEqual("sorter-2", normalize_sorter_name(" Sorter-2 "))
        for invalid in ("sorter name", "-sorter", "sorter-", "a" * 64):
            with self.assertRaises(ValueError):
                normalize_sorter_name(invalid)

    def test_usb_discovery_is_on_demand_and_non_windows_safe(self) -> None:
        # The test host is Linux. The Windows API is not imported or called.
        self.assertEqual([], removable_usb_roots())

    def test_primary_lan_address_uses_default_route_without_subprocess(self) -> None:
        fake_socket = mock.Mock()
        fake_socket.getsockname.return_value = ("192.168.4.51", 50231)
        with mock.patch("sorter.network_info.socket.socket", return_value=fake_socket):
            self.assertEqual("192.168.4.51", primary_local_ipv4_address())
        fake_socket.connect.assert_called_once_with(("192.0.2.1", 9))
        fake_socket.close.assert_called_once()


class WebIntegrationPolicyTests(unittest.TestCase):
    def test_native_camera_implementation_is_unchanged(self) -> None:
        digest = hashlib.sha256((ROOT / "sorter" / "camera.py").read_bytes()).hexdigest()
        self.assertEqual(
            "a0d6218e02779517ddbd6c35519c3b8b7249e73f698139519c5c316e9770fe9e",
            digest,
        )

    def test_web_server_is_lazy_and_bounded(self) -> None:
        app = (ROOT / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")

        self.assertNotIn("from ..web_interface import WebInterfaceServer\n", app[:8000])
        self.assertIn("MAX_SESSIONS = 16", web)
        self.assertIn("MAX_SSE_CLIENTS = 8", web)
        self.assertIn("queue.Queue(maxsize=32)", web)
        self.assertIn("Threading.BoundedSemaphore", web.replace("threading.BoundedSemaphore", "Threading.BoundedSemaphore"))
        self.assertNotIn("setInterval(refresh,5000)", web)

    def test_explicit_web_server_start_stop_releases_thread_and_subscriptions(self) -> None:
        from sorter.db import Database
        from sorter.events import EventBus

        from sorter.web_interface import WebInterfaceServer

        class FakeApp:
            def __init__(self, db) -> None:
                self.db = db
                self.bus = EventBus()
                self.config = object()
                self.training_requested = False

            def get_training_manager(self):
                self.training_requested = True
                raise AssertionError("Stopping LAN Access constructed training")

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "web.sqlite3")
            db.ensure_initialized()
            app = FakeApp(db)
            settings = WebSettings(
                enabled=True, desktop_enabled=True,
                sorter_name="web-test", port=0,
            )
            server = WebInterfaceServer(app, settings)
            with mock.patch("sorter.mdns_advertiser.MdnsAdvertiser.start", return_value=False):
                server.start()
            worker = server._thread
            self.assertIsNotNone(worker)
            self.assertTrue(worker.is_alive())
            server.stop()
            self.assertFalse(worker.is_alive())
            self.assertFalse(app.training_requested)
            self.assertTrue(all(not handlers for handlers in app.bus._subs.values()))
            db.close()

    def test_abandoned_control_and_live_browser_buffers_expire(self) -> None:
        from sorter.db import Database
        from sorter.events import EventBus

        from sorter.web_interface import CONTROL_IDLE_S, WebInterfaceServer

        class Copyable:
            def copy(self):
                return self

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "lease.sqlite3")
            db.ensure_initialized()
            app = types.SimpleNamespace(db=db, bus=EventBus(), config=object())
            server = WebInterfaceServer(app, WebSettings(sorter_name="lease-test", port=0))
            session = server._new_session()
            server._control_sid = session.sid
            server._control_last_seen = __import__("time").monotonic() - CONTROL_IDLE_S - 1
            server._prune_sessions()
            self.assertIsNone(server._control_sid)
            self.assertIn(session.sid, server._sessions)

            client = server.state.register()
            server.state._result_event({"ok": True, "slot": 2, "label": "TULA"})
            server.state._crop_event(Copyable())
            self.assertTrue(server.state._history)
            self.assertIsNotNone(server.state._last_crop)
            server.state.unregister(client.token)
            server.state._result_event({"ok": True, "slot": 2, "label": "NORMA"})
            server.state._crop_event(Copyable())
            self.assertFalse(server.state._history)
            self.assertIsNone(server.state._last_crop)
            server.stop()
            db.close()

    def test_lan_access_is_a_lazy_maintenance_tab(self) -> None:
        app = (ROOT / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
        tab = (
            ROOT / "sorter" / "ui" / "tab_web_access.py"
        ).read_text(encoding="utf-8")

        self.assertIn('_add_lazy_scrolled(\n            WebAccessTab, "LAN Access"', app)
        self.assertIn("Enable Web Interface", tab)
        self.assertIn("Primary LAN address", tab)
        self.assertIn("primary_local_ipv4_addresses()", tab)
        self.assertNotIn("WebInterfaceServer", tab)

    def test_web_security_and_single_control_lease_are_enforced(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        for required in (
            "X-Shibby-CSRF",
            "SameSite=Strict",
            "Cross-origin control is not allowed",
            "X-Frame-Options",
            "Content-Security-Policy",
            "Another browser currently controls this sorter",
            "Technician unlock is required",
            "accepts only local or private-network clients",
        ):
            self.assertIn(required, web + (ROOT / "sorter" / "web_settings.py").read_text(encoding="utf-8"))

    def test_web_opens_without_sorter_password_but_keeps_sessions(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        tab = (ROOT / "sorter" / "ui" / "tab_web_access.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("session = parent._new_session()", web)
        self.assertNotIn("Sorter password", web)
        self.assertNotIn('path == "/login"', web)
        self.assertNotIn("New password", tab)
        self.assertIn("http://127.0.0.1:", tab)

    def test_web_mode_uses_authoritative_shared_services(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        for required in (
            'getattr(self.app, "run_controller"',
            'getattr(self.app, "broker"',
            'getattr(self.app, "camera"',
            "self.app.config",
            "self.db = app.db",
            "refresh_saved_bins_runtime",
            "set_package_slot_headstamp",
            "set_parent_slot",
        ):
            self.assertIn(required, web)
        self.assertNotIn("cv2.VideoCapture", web)

    def test_web_and_desktop_share_one_lazy_training_process_owner(self) -> None:
        app = (ROOT / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
        train = (ROOT / "sorter" / "ui" / "tab_train.py").read_text(encoding="utf-8")
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")

        self.assertIn("def get_training_manager", app)
        self.assertIn("self.training_manager = app.get_training_manager()", train)
        self.assertIn("return self.app.get_training_manager()", web)
        self.assertNotIn("TrainingManager(bus)", train)
        self.assertNotIn("TrainingManager(app.bus)", web)
        self.assertNotIn("subprocess.Popen", web)

    def test_web_parity_pages_and_primary_controls_are_present(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        for required in (
            '("server", "Server")',
            '("lan", "LAN Access")',
            "LOAD SLOT CONFIG",
            "SAVE CURRENT AS NEW",
            "EDIT LAYOUT",
            "IMPORT BROWSER FILE",
            "EXPORT LOCAL COPY TO USB",
            "CONNECT AND FIND MODELS",
            "GET CONFIG FROM BOARD",
            "PUSH TO BOARD",
            "DETECT CAMERAS",
            "START SERVER",
            "DISABLE AND RELEASE",
            "Sort While Training",
            "Stochastic depth",
            "Hidden Features",
        ):
            self.assertIn(required, web)

    def test_web_models_page_omits_active_classifications_block(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        self.assertNotIn("Active Classifications", web)
        self.assertNotIn("renderModelStructure", web)

    def test_web_releases_control_and_transient_buffers(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        self.assertIn("/api/control/release", web)
        self.assertIn("window.addEventListener('pagehide'", web)
        self.assertIn("CONTROL_IDLE_S = 45.0", web)
        self.assertIn("now - self._control_last_seen > CONTROL_IDLE_S", web)
        self.assertIn("if not self._clients:", web)
        self.assertIn("if not self.has_live_clients():", web)
        self.assertIn("self._history.clear()", web)
        self.assertIn("self._last_crop = None", web)

    def test_stopping_web_does_not_cancel_shared_training(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        shutdown = web.split("class WindowsWebOperations", 1)[1].split(
            "def shutdown", 1
        )[1].split(
            "def _training_log_event", 1
        )[0]
        self.assertNotIn("self.training.cancel()", shutdown)
        self.assertNotIn("self.training.is_running", shutdown)
        app = (ROOT / "sorter" / "ui" / "app.py").read_text(encoding="utf-8")
        self.assertIn("manager.cancel()", app)
        self.assertIn("manager.wait(timeout=6.0)", app)
        self.assertIn("server.operations.web_training_active", app)

    def test_saved_bins_web_editor_uses_target_union_and_preserving_update(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        self.assertIn('row["editor_assignments"]', web)
        self.assertIn("service.edit_rows(profile, target)", web)
        self.assertIn("service.replace_target_assignments(", web)
        self.assertIn("data-edit-available", web)
        self.assertIn("target:$('binTarget').value,assignments:rows", web)

    def test_web_preview_timer_exists_only_while_preview_page_is_active(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        self.assertEqual(1, web.count("setInterval("))
        self.assertIn("if(timer)clearInterval(timer)", web)
        self.assertIn("['camera','image'].includes(name)", web)

    def test_expensive_web_operations_remain_explicit(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        # Model loading, training, camera probing, USB discovery and full
        # diagnostics must remain behind direct user actions.
        self.assertIn("def server_start", web)
        self.assertIn("def camera_detect", web)
        self.assertIn("def usb_snapshot", web)
        self.assertIn("def diagnostics_toggle", web)
        self.assertNotIn("self.app.get_api_server_runtime()\n        self.state", web)

    def test_normal_web_system_api_exposes_public_identity(self) -> None:
        web = (ROOT / "sorter" / "web_interface.py").read_text(encoding="utf-8")
        block = web.split("def system_snapshot", 1)[1].split("class WebInterfaceServer", 1)[0]
        self.assertIn('"public_version": PUBLIC_VERSION', block)


if __name__ == "__main__":
    unittest.main()
