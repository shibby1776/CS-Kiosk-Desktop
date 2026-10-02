"""Authenticated browser presentation layer for the Windows sorter backend.

The server never owns camera, serial, routing, database or inference state.
It calls the same application objects used by Tk and observes the same
EventBus.  When Web Interface is disabled this module is not imported.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import queue
import secrets
import tempfile
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

import cv2

from . import api_client, classifier, image_proc, local_inference, paths
from .api_server_settings import ApiServerSettingsRepo
from .events import post_assignment_changed
from .feedback import FeedbackService, is_feedback_model
from .model_io import ExportMode, export_model, import_model, record_installed_version
from .models import CheckpointEnv, FEEDBACK_UPLOAD_MODES, Model, SUPPORTED_MODEL_MODES
from .network_info import local_ipv4_addresses, primary_local_ipv4_addresses
from .repository import (
    ApiModelAliasRepo,
    CartridgeRepo,
    HeadstampParentRepo,
    HeadstampRepo,
    ModelRepo,
    SettingsRepo,
)
from .saved_bins import (
    FILE_SUFFIX,
    SavedBinAssignment,
    SavedBinsService,
    SavedBinsStore,
    SavedBinsTarget,
    copy_layout_file,
)
from .sensor_diagnostics import SensorDiagnosticRunner
from .training.dataset import class_counts, save_training_image
from .training.manager import TrainingJob
from .version import PUBLIC_VERSION
from .web_settings import WebSettings, normalize_sorter_name
from .windows_usb import removable_usb_roots, resolve_usb_root
from .catch_all_report import CatchAllReport


MAX_JSON_BODY = 256 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
SESSION_IDLE_S = 8 * 60 * 60
CONTROL_IDLE_S = 45.0
TECH_UNLOCK_S = 30 * 60
MAX_SESSIONS = 16
MAX_SSE_CLIENTS = 8
PRODUCT_NAME = "ShibbyPrints Kiosk Sorter"


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    return str(value)


def _validated_model_name(value: Any) -> str:
    name = str(value or "").strip()
    if (
        not name
        or len(name) > 128
        or name in {".", ".."}
        or any(char in name for char in "/\\\0\r\n")
    ):
        raise ValueError("Model name contains unsupported path characters.")
    return name


@dataclass
class _Session:
    sid: str
    csrf: str
    created: float
    last_seen: float
    technician_until: float = 0.0

    @property
    def technician(self) -> bool:
        return time.monotonic() < self.technician_until


@dataclass
class _EventClient:
    token: str
    events: queue.Queue[dict[str, Any]] = field(
        default_factory=lambda: queue.Queue(maxsize=32)
    )


@dataclass
class _WebDiagnosticState:
    """Resources created only after Diagnostics is explicitly enabled."""

    runner: SensorDiagnosticRunner | None = None
    busy: bool = False
    phase: str = "idle"
    status: str = "Diagnostics enabled."
    progress: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    result: dict[str, Any] | None = None


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, max_workers: int = 16, **kwargs) -> None:
        self._worker_limit = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._worker_limit.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_limit.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_limit.release()


class WebRuntimeState:
    """Bounded, event-driven browser mirror of authoritative run state."""

    def __init__(self, app: Any, has_sessions: Callable[[], bool]) -> None:
        self.app = app
        self._has_sessions = has_sessions
        self.catch_all_report = getattr(app, "catch_all_report", None)
        self._owns_catch_all_report = self.catch_all_report is None
        if self.catch_all_report is None:
            self.catch_all_report = CatchAllReport(app.bus)
        self._lock = threading.RLock()
        self._clients: dict[str, _EventClient] = {}
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._master_count = 0
        self._slot_counts: dict[int, int] = defaultdict(int)
        self._status = "Ready."
        self._last: dict[str, Any] = {}
        self._last_error = ""
        self._error_revision = 0
        self._image_revision = 0
        self._last_crop = None
        self._history: deque[dict[str, Any]] = deque(maxlen=24)
        self._history_id = 0
        self._serial: dict[str, Any] = {"connected": False}
        self._subscribe()

    def _on(self, topic: str, handler: Callable[[Any], None]) -> None:
        self.app.bus.subscribe(topic, handler)
        self._subscriptions.append((topic, handler))

    def _subscribe(self) -> None:
        self._on("status", self._status_event)
        self._on("run/status", self._status_event)
        self._on("run/result", self._result_event)
        self._on("run/classified", self._classified_event)
        self._on("test/classified", self._classified_event)
        self._on("run/cropped", self._crop_event)
        self._on("test/cropped", self._crop_event)
        self._on("run/error", self._error_event)
        self._on("test/error", self._error_event)
        self._on("run/started", self._recovered_event)
        self._on("run/counters_reset", self._reset_event)
        self._on("run/slot_counter_reset", self._slot_reset_event)
        self._on("run/assignment_changed", lambda _p: self.publish("routing"))
        self._on("run/headstamps_synced", lambda _p: self.publish("routing"))
        self._on("mode/changed", lambda _p: self.publish("models"))
        self._on("serial/connection", self._serial_event)
        for name in (
            "training/start", "training/epoch", "training/log", "training/error",
            "training/done", "training/failed", "training/cancelled",
        ):
            self._on(name, lambda payload, event=name: self.publish(event, payload))

    def shutdown(self) -> None:
        if self._owns_catch_all_report:
            self.catch_all_report.shutdown()
        for topic, handler in self._subscriptions:
            self.app.bus.unsubscribe(topic, handler)
        self._subscriptions.clear()
        with self._lock:
            self._clients.clear()
            self._history.clear()
            self._last_crop = None

    def _status_event(self, payload: Any) -> None:
        with self._lock:
            self._status = str(payload or "")
        self.publish("status")

    def _result_event(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not payload.get("ok"):
            return
        slot = int(payload.get("slot", 0) or 0)
        with self._lock:
            self._last_error = ""
            self._master_count += 1
            self._slot_counts[slot] += 1
            if self.has_live_clients():
                self._history_id += 1
                self._history.appendleft({
                    "id": self._history_id,
                    "label": str(payload.get("label") or ""),
                    "confidence": float(payload.get("confidence", 0) or 0),
                    "slot": slot,
                })
        self.publish("result")

    def _classified_event(self, payload: Any) -> None:
        if isinstance(payload, dict):
            with self._lock:
                self._last_error = ""
                self._last = {
                    "label": str(payload.get("label") or ""),
                    "parent": payload.get("parent"),
                    "confidence": float(payload.get("confidence", 0) or 0),
                    "slot": payload.get("slot"),
                }
        self.publish("classification")

    def _crop_event(self, payload: Any) -> None:
        # No live browser connection means no web-owned image buffer.
        if not self.has_live_clients():
            return
        try:
            image = payload.copy()
        except Exception:
            return
        with self._lock:
            self._last_crop = image
            self._image_revision += 1
        self.publish("image")

    def _error_event(self, payload: Any) -> None:
        with self._lock:
            self._last_error = str(payload or "Unknown error")
            self._error_revision += 1
        self.publish("error")

    def _recovered_event(self, _payload: Any) -> None:
        with self._lock:
            self._last_error = ""
        self.publish("status")

    def _reset_event(self, _payload: Any) -> None:
        with self._lock:
            self._master_count = 0
            self._slot_counts.clear()
        self.publish("counts")

    def _slot_reset_event(self, payload: Any) -> None:
        try:
            slot = int(payload)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._slot_counts[slot] = 0
        self.publish("counts")

    def _serial_event(self, payload: Any) -> None:
        with self._lock:
            self._serial = dict(payload) if isinstance(payload, dict) else {"connected": False}
        self.publish("serial")

    def register(self) -> _EventClient:
        with self._lock:
            if len(self._clients) >= MAX_SSE_CLIENTS:
                raise RuntimeError("Too many live browser connections.")
            client = _EventClient(secrets.token_urlsafe(18))
            self._clients[client.token] = client
            return client

    def unregister(self, token: str) -> None:
        with self._lock:
            self._clients.pop(token, None)
            if not self._clients:
                self._history.clear()
                self._last_crop = None

    def has_live_clients(self) -> bool:
        """Return whether a browser is actively consuming the event stream."""
        with self._lock:
            return bool(self._clients)

    def publish(self, kind: str, detail: Any = None) -> None:
        event = {"type": kind}
        if detail is not None:
            event["detail"] = _safe_json(detail)
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            try:
                client.events.put_nowait(event)
            except queue.Full:
                try:
                    client.events.get_nowait()
                    client.events.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass

    def snapshot(self, *, control_owned: bool, technician: bool) -> dict[str, Any]:
        cfg = self.app.config
        controller = getattr(self.app, "run_controller", None)
        broker = getattr(self.app, "broker", None)
        camera = getattr(self.app, "camera", None)
        try:
            camera_info = camera.diagnostic_info() if camera else {}
            camera_ready = bool(camera_info.get("opened") and not camera_info.get("last_error"))
        except Exception:
            camera_ready = False
        slot_quantity = max(1, int(cfg.serial.get("slot_quantity", 8) or 8))
        assigned: dict[int, list[str]] = {slot: [] for slot in range(slot_quantity)}
        for item in cfg.headstamps:
            name = str(item.get("name") or "").strip()
            if name:
                assigned.setdefault(int(item.get("slot", 0) or 0), []).append(name)
        if cfg.run_package_mode:
            assigned={slot:list(names) for slot,names in cfg.package_slot_map().items()}
        elif cfg.use_parent_classifications and cfg.model_has_parents():
            assigned={slot:[] for slot in range(slot_quantity)}
            for item in cfg.headstamps:
                assigned.setdefault(cfg.slot_for_headstamp(item['name']) or 0,[]).append(item['name'])
        routing_mode = "headstamp"
        routing_items: list[dict[str, Any]] = []
        if cfg.run_package_mode:
            routing_mode = "package"
            routing_items = [
                {
                    "kind": "package",
                    "name": str(item.get("name") or ""),
                    "slots": cfg.slots_for_headstamp_package(
                        str(item.get("name") or "")
                    ),
                }
                for item in cfg.headstamps
                if str(item.get("name") or "").strip()
            ]
        elif cfg.use_parent_classifications and cfg.model_has_parents():
            routing_mode = "parent"
            routing_items = [
                {
                    "kind": "parent",
                    "id": int(parent["id"]),
                    "name": str(parent["name"]),
                    "slot": int(parent.get("slot", 0) or 0),
                }
                for parent in cfg.parents_with_slots()
            ]
            routing_items.extend(
                {
                    "kind": "headstamp",
                    "name": str(item["name"]),
                    "slot": int(item.get("slot", 0) or 0),
                }
                for item in cfg.headstamps_with_parents()
                if item.get("parent_id") is None
            )
        else:
            routing_items = [
                {
                    "kind": "headstamp",
                    "name": str(item.get("name") or ""),
                    "slot": int(item.get("slot", 0) or 0),
                }
                for item in cfg.headstamps
                if str(item.get("name") or "").strip()
            ]
        with self._lock:
            counts = dict(self._slot_counts)
            result = {
                "product": PRODUCT_NAME,
                "version": PUBLIC_VERSION,
                "sorter_name": WebSettings.load(cfg.settings).sorter_name,
                "running": bool(controller and controller.is_running),
                "status": self._status,
                "error": self._last_error,
                "error_revision": self._error_revision,
                "master_count": self._master_count,
                "catch_all_count": counts.get(0, 0),
                "last_classification": dict(self._last),
                "image_revision": self._image_revision,
                "history": list(self._history),
                "serial": dict(self._serial),
            }
        result.update({
            "serial_connected": bool(broker and getattr(broker, "is_connected", False)),
            "usb_recovering": "usb bridge recovering" in str(self._status).casefold(),
            "camera_ready": camera_ready,
            "control_owned": bool(control_owned),
            "technician": bool(technician),
            "training_visible": bool(cfg.training_tools_visible),
            "routing_mode": routing_mode,
            "routing_items": routing_items,
            "auto_select": bool(cfg.run_auto_select_trays),
            "package_mode": bool(cfg.run_package_mode),
            "package_size": int(cfg.run_package_size),
            "confidence_floor": int(cfg.run_confidence_floor),
            "slots": [
                {"slot": slot, "count": counts.get(slot, 0),
                 "headstamps": sorted(assigned.get(slot, []), key=str.casefold)}
                for slot in range(1, slot_quantity)
            ],
            "catch_all_report": self.catch_all_report.snapshot(),
        })
        return result

    def crop_jpeg(self) -> bytes | None:
        with self._lock:
            image = None if self._last_crop is None else self._last_crop.copy()
        return _jpeg(image, 82)


def _jpeg(frame: Any, quality: int = 80) -> bytes | None:
    if frame is None:
        return None
    try:
        ok, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)],
        )
        return encoded.tobytes() if ok else None
    except Exception:
        return None


class WindowsWebOperations:
    """Windows-specific adapter over existing repositories and app services."""

    def __init__(self, app: Any, state: WebRuntimeState) -> None:
        self.app = app
        self.state = state
        self.db = app.db
        self.models = ModelRepo(self.db)
        self.cartridges = CartridgeRepo(self.db)
        self.headstamps = HeadstampRepo(self.db)
        self.parents = HeadstampParentRepo(self.db)
        self.settings = SettingsRepo(self.db)
        self.api_aliases = ApiModelAliasRepo(self.db)
        self.api_settings = ApiServerSettingsRepo(self.db)
        self.feedback = FeedbackService(self.db)
        self._feedback_inflight = False
        self._feedback_declined: set[int] = set()
        self._training_model_id: int | None = None
        self._training_output: Path | None = None
        self._training_last_crop = None
        self._training_lock = threading.Lock()
        self._training_log: deque[str] = deque(maxlen=300)
        self._serial_log: deque[dict[str, str]] = deque(maxlen=300)
        self._serial_lock = threading.Lock()
        self._web_diagnostics: _WebDiagnosticState | None = None
        self._subscriptions: list[tuple[str, Callable[[Any], None]]] = []
        self._subscribe("training/log", self._training_log_event)
        self._subscribe("training/error", self._training_error_event)
        self._subscribe("training/done", self._training_done)
        self._subscribe("serial/rx", lambda p: self._serial_event("RX", p))
        self._subscribe("serial/tx", lambda p: self._serial_event("TX", p))
        self._subscribe("feedback/queued", self._feedback_queued)
        self._subscribe("run/stopped", lambda _p: self._feedback_run_stopped())

    @property
    def training(self):
        """Lazily share the desktop application's sole training owner."""
        return self.app.get_training_manager()

    @property
    def web_training_active(self) -> bool:
        """True only for a training job launched from this web adapter."""
        return self._training_model_id is not None and self.training.is_running

    def _subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        self.app.bus.subscribe(topic, handler)
        self._subscriptions.append((topic, handler))

    def shutdown(self) -> None:
        state = self._web_diagnostics
        self._web_diagnostics = None
        if state is not None and state.runner is not None:
            state.runner.cancel()
        # Training is application-owned and may have been started from the
        # Desktop UI. Stopping LAN Access must never cancel that shared job.
        for topic, handler in self._subscriptions:
            self.app.bus.unsubscribe(topic, handler)
        self._subscriptions.clear()
        self._training_log.clear()
        with self._serial_lock:
            self._serial_log.clear()

    def _training_log_event(self, payload: Any) -> None:
        if self.state.has_live_clients():
            self._training_log.append(str(payload))

    def _training_error_event(self, payload: Any) -> None:
        if self.state.has_live_clients():
            self._training_log.append(f"ERROR: {payload}")

    def _feedback_queued(self, payload: Any) -> None:
        # Once the Desktop Run tab exists it owns automatic feedback upload.
        # This avoids two workers draining the same queue in Both mode.
        if getattr(self.app, "run_tab", None) is not None:
            return
        if not isinstance(payload, dict):
            return
        model_id = payload.get("model_id")
        if payload.get("upload_mode") == "Instant" and model_id is not None:
            self._feedback_drain(int(model_id))

    def _feedback_run_stopped(self) -> None:
        if getattr(self.app, "run_tab", None) is not None:
            return
        model_id = self.settings.get_active_model_id()
        model = self.models.get(model_id) if model_id is not None else None
        if (
            is_feedback_model(model)
            and model.feedback_loop_upload_mode == "OnRunComplete"
        ):
            self._feedback_drain(int(model.id))

    def _feedback_drain(self, model_id: int) -> None:
        if self._feedback_inflight or model_id in self._feedback_declined:
            return
        self._feedback_inflight = True

        def _done(result: dict[str, Any]) -> None:
            self._feedback_inflight = False
            if result.get("declined"):
                self._feedback_declined.add(model_id)
            self.state.publish("models")

        def _error(_exc: Exception) -> None:
            self._feedback_inflight = False

        self.app.run_worker(
            lambda: self.feedback.upload_pending(model_id, auth=getattr(self.app, "auth", None)),
            on_done=_done,
            on_error=_error,
        )

    def feedback_upload(self, model_id: int) -> None:
        model = self.models.get(int(model_id))
        if not is_feedback_model(model):
            raise ValueError("This model does not have community feedback enabled.")
        self._feedback_drain(int(model_id))

    def _serial_event(self, direction: str, payload: Any) -> None:
        if not self.state.has_live_clients():
            return
        with self._serial_lock:
            self._serial_log.append({
                "time": time.strftime("%H:%M:%S"),
                "direction": direction,
                "line": str(payload)[:1000],
            })

    def _training_done(self, payload: Any) -> None:
        model_id, output = self._training_model_id, self._training_output
        self._training_model_id = None
        self._training_output = None
        if model_id is None or output is None or not output.is_file():
            return
        model = self.models.get(model_id)
        if model is not None:
            model.model_path = str(output)
            model.last_training_date = time.strftime("%Y-%m-%d %H:%M")
            if isinstance(payload, dict):
                model.checkpoint_env = CheckpointEnv.from_dict(payload.get("env"))
            self.models.update(model)
        self.state.publish("models")

    def owner(self, fn: Callable[[], Any], timeout: float = 20.0) -> Any:
        return self.app.dispatch_web_action(fn, timeout=timeout)

    def ensure_stopped(self) -> None:
        controller = getattr(self.app, "run_controller", None)
        if controller is not None and (controller.is_running or getattr(controller,'operation_busy',False)):
            raise RuntimeError("Stop sorting before changing this setting.")

    def run_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        controller = getattr(self.app, "run_controller", None)
        broker = getattr(self.app, "broker", None)
        if action == "stop":
            if controller is not None:
                self.owner(controller.stop)
            return {}
        if action == "reset":
            self.owner(self.app.reset_run_counters)
            return {}
        if action == "clear-slots":
            self.ensure_stopped()
            self.app.config.clear_slot_assignments()
            self.owner(self.app.reset_run_counters)
            post_assignment_changed(self.app.bus, "web_clear_slots")
            return {}
        if action == "auto-select":
            self.app.config.set_run_auto_select_trays(bool(payload.get("enabled")))
            post_assignment_changed(self.app.bus, "web_operator_auto_select")
            return {}
        if broker is None or not getattr(broker, "is_connected", False) or controller is None:
            raise RuntimeError("The sorter controller is not connected.")
        if action == "start":
            problem = classifier.checkpoint_problem(self.app.db)
            if problem is not None:
                raise RuntimeError(problem)
            if not controller.is_running:
                controller.refresh_community_feedback(
                    auth=getattr(self.app, "auth", None)
                )
                self.owner(controller.start)
            return {}
        if action == "feed":
            self.ensure_stopped()
            problem = classifier.checkpoint_problem(self.app.db)
            if problem is not None:
                raise RuntimeError(problem)

            def feed_cycle() -> dict[str, Any]:
                controller.refresh_community_feedback(
                    auth=getattr(self.app, "auth", None)
                )
                return controller.cycle_once()

            self.app.run_worker(feed_cycle)
            return {}
        if action == "force-feed":
            self.ensure_stopped()
            self.app.run_worker(lambda: broker.force_sort_and_move(0))
            return {}
        raise ValueError("Unknown Run action.")

    def save_run_options(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_stopped()
        cfg = self.app.config
        cfg.set_run_auto_select_trays(bool(payload.get("auto_select")))
        cfg.set_run_confidence_floor(int(payload.get("confidence_floor", cfg.run_confidence_floor)))
        cfg.set_run_package_mode(bool(payload.get("package_mode")))
        cfg.set_run_package_size(int(payload.get("package_size", cfg.run_package_size)))
        cfg.set_run_store_images(str(payload.get("store_images", cfg.run_store_images)))
        cfg.set_use_parent_classifications(bool(payload.get("use_parents", cfg.use_parent_classifications)))
        post_assignment_changed(self.app.bus, "web_run_options")
        return self.run_options()

    def run_options(self) -> dict[str, Any]:
        cfg = self.app.config
        return {
            "auto_select": cfg.run_auto_select_trays,
            "confidence_floor": cfg.run_confidence_floor,
            "package_mode": cfg.run_package_mode,
            "package_size": cfg.run_package_size,
            "store_images": cfg.run_store_images,
            "use_parents": cfg.use_parent_classifications,
            "has_parents": cfg.model_has_parents(),
        }

    def set_assignment(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        kind = str(payload.get("kind") or "headstamp")
        name = str(payload.get("name") or "").strip()
        slot = int(payload.get("slot", 0) or 0)
        maximum = max(0, int(self.app.config.serial.get("slot_quantity", 8)) - 1)
        if not 0 <= slot <= maximum:
            raise ValueError("Invalid slot.")
        if kind == "package":
            if not name or slot <= 0:
                raise ValueError("Invalid package assignment.")
            self.app.config.set_package_slot_headstamp(
                slot, name, bool(payload.get("enabled"))
            )
        elif kind == "parent":
            parent_id = int(payload.get("id", 0) or 0)
            if parent_id <= 0 or not self.app.config.set_parent_slot(parent_id, slot):
                raise ValueError("The selected parent classification is not available.")
        else:
            if not name or not self.app.config.set_headstamp_slot(name, slot):
                raise ValueError("The selected classification is not available.")
        post_assignment_changed(
            self.app.bus, "web_assignment", {"full_refresh": False, "slot": slot},
        )

    def models_snapshot(self) -> dict[str, Any]:
        active_id = self.settings.get_active_model_id()
        cartridges = {c.id: c.name for c in self.cartridges.list()}
        rows = []
        for model in self.models.list():
            rows.append({
                "id": model.id,
                "name": model.name,
                "caliber": cartridges.get(model.cartridge_id, "Unknown"),
                "model_mode": model.model_mode,
                "model_type": model.model_type,
                "model_version": model.model_version,
                "active": model.id == active_id,
                "trained": bool(model.model_path and Path(model.model_path).is_file()),
                "headstamp_count": len(self.headstamps.list_for_model(model.id)),
                "community_uid": model.community_model_uid or "",
                "feedback_enabled": bool(model.feedback_loop_enabled),
                "feedback_floor": int(model.feedback_loop_confidence_floor),
                "feedback_mode": model.feedback_loop_upload_mode,
                "feedback_pending": self.feedback.count_pending(model.id)
                if model.community_model_uid else 0,
            })
        return {
            "active_model_id": active_id,
            "runtime": "local" if active_id is not None else "remote",
            "software_profile": self.app.config.software_profile,
            "training_visible": self.app.config.training_tools_visible,
            "models": rows,
            "cartridges": sorted(set(cartridges.values()), key=str.casefold),
            "remote": self.remote_snapshot(),
        }

    def set_software_profile(self, profile: str) -> None:
        if self.training.is_running:
            raise RuntimeError("Stop training before changing Software Profile.")
        self.app.config.set_software_profile(str(profile))
        self.app.bus.post("profile/changed", {"profile": profile})
        self.owner(self.app._apply_training_visibility)

    def activate_model(self, model_id: int | None) -> None:
        self.ensure_stopped()
        if model_id is None:
            self.settings.clear_active_model()
        else:
            if self.models.get(int(model_id)) is None:
                raise ValueError("Model not found.")
            self.settings.set_active_model_id(int(model_id))
        self.app.config.reload_headstamps_for_active_model()
        self.app.bus.post("mode/changed", {"active_model_id": model_id})
        post_assignment_changed(self.app.bus, "web_model_activate")

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_stopped()
        name = _validated_model_name(payload.get("name"))
        caliber = str(payload.get("caliber") or "").strip()
        mode = str(payload.get("model_mode") or "convnext_tiny")
        if not name or not caliber:
            raise ValueError("Model name and caliber are required.")
        if mode not in SUPPORTED_MODEL_MODES:
            raise ValueError("Unsupported ConvNeXt model type.")
        cart = self.cartridges.get_or_create(caliber)
        saved = self.models.create(Model(name=name, cartridge_id=cart.id, model_mode=mode))
        paths.ensure_model_subtree(saved.id)
        self.state.publish("models")
        return {"model_id": saved.id}

    def delete_model(self, model_id: int) -> None:
        self.ensure_stopped()
        model = self.models.get(int(model_id))
        active_cleared = self.models.delete(int(model_id))
        import shutil
        shutil.rmtree(paths.model_dir(int(model_id)), ignore_errors=True)
        if model is not None and model.model_path:
            local_inference.evict_model(model.model_path)
        if active_cleared:
            self.app.config.reload_headstamps_for_active_model()
            self.app.bus.post("mode/changed", {"active_model_id": None})
            post_assignment_changed(self.app.bus, "web_model_delete")
        self.state.publish("models")

    def update_model(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        model = self.models.get(int(payload.get("model_id", 0)))
        if model is None:
            raise ValueError("Model not found.")
        if "name" in payload:
            model.name = _validated_model_name(payload.get("name"))
        if model.community_model_uid:
            model.feedback_loop_enabled = bool(payload.get("feedback_enabled", model.feedback_loop_enabled))
            mode = str(payload.get("feedback_mode", model.feedback_loop_upload_mode))
            if mode not in FEEDBACK_UPLOAD_MODES:
                raise ValueError("Unknown feedback upload mode.")
            model.feedback_loop_upload_mode = mode
        self.models.update(model)
        self.state.publish("models")

    def model_headstamps(self, model_id: int | None = None) -> dict[str, Any]:
        if model_id is None:
            entries = self.app.config.remote_headstamps()
        else:
            entries = [
                {"name": h.name, "slot": h.slot, "parent_id": h.parent_id}
                for h in self.headstamps.list_for_model(int(model_id))
            ]
        return {"headstamps": entries}

    def server_snapshot(self) -> dict[str, Any]:
        """Describe the integrated API server without constructing its runtime."""
        settings = self.api_settings.load()
        runtime = getattr(self.app, "api_server_runtime", None)
        status = runtime.snapshot() if runtime is not None else {
            "state": "stopped",
            "message": "Server stopped.",
            "address": "",
            "loaded_aliases": [],
            "errors": {},
        }
        cartridges = {item.id: item.name for item in self.cartridges.list()}
        models = {
            model.id: model for model in self.models.list()
            if model.id is not None
        }
        aliases = []
        for assignment in self.api_aliases.list():
            model = models.get(assignment.model_id)
            aliases.append({
                "alias": assignment.alias,
                "model_id": assignment.model_id,
                "model": model.name if model else "Missing model",
                "caliber": cartridges.get(model.cartridge_id, "—") if model else "—",
                "preload": bool(assignment.preload),
                "loaded": assignment.alias.casefold() in {
                    str(name).casefold() for name in status.get("loaded_aliases", [])
                },
                "error": str(status.get("errors", {}).get(assignment.alias, "")),
            })
        trained = [
            {
                "id": model.id,
                "name": model.name,
                "caliber": cartridges.get(model.cartridge_id, "Unknown"),
            }
            for model in models.values()
            if model.model_path and Path(model.model_path).is_file()
        ]
        address = primary_local_ipv4_addresses()
        return {
            "settings": {
                "host": settings.host,
                "port": settings.port,
                "on_demand_cache_slots": settings.on_demand_cache_slots,
                "remote_queue_limit": settings.remote_queue_limit,
                "api_key_configured": bool(settings.api_key_hash),
            },
            "status": status,
            "aliases": aliases,
            "trained_models": trained,
            "primary_url": (
                f"http://{address[0]}:{settings.port}"
                if address and settings.host == "0.0.0.0"
                else f"http://127.0.0.1:{settings.port}"
            ),
        }

    def _require_api_server_stopped(self) -> None:
        runtime = getattr(self.app, "api_server_runtime", None)
        if runtime is not None and runtime.is_running:
            raise RuntimeError("Stop the integrated API server before changing its settings.")

    def server_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_api_server_stopped()
        settings = self.api_settings.load()
        settings.host = "0.0.0.0" if bool(payload.get("local_network")) else "127.0.0.1"
        settings.port = int(payload.get("port", settings.port))
        settings.on_demand_cache_slots = int(
            payload.get("on_demand_cache_slots", settings.on_demand_cache_slots)
        )
        settings.remote_queue_limit = int(
            payload.get("remote_queue_limit", settings.remote_queue_limit)
        )
        if payload.get("replace_api_key"):
            raw_key = str(payload.get("api_key") or "")
            settings.set_api_key(raw_key)
            reporter = getattr(self.app, "crash_reporter", None)
            if raw_key and reporter is not None:
                reporter.register_secret(raw_key)
        settings.validate()
        self.api_settings.save(settings)
        return self.server_snapshot()

    def server_assign(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_api_server_stopped()
        alias = str(payload.get("alias") or "").strip()
        model_id = int(payload.get("model_id", 0) or 0)
        with self.db.transaction():
            self.api_aliases.assign(alias, model_id, preload=bool(payload.get("preload")))
        return self.server_snapshot()

    def server_preload(self, alias: str) -> dict[str, Any]:
        self._require_api_server_stopped()
        assignment = self.api_aliases.get(str(alias or ""))
        if assignment is None:
            raise ValueError("API model assignment was not found.")
        self.api_aliases.set_preload(assignment.alias, not assignment.preload)
        return self.server_snapshot()

    def server_remove(self, alias: str) -> dict[str, Any]:
        self._require_api_server_stopped()
        self.api_aliases.remove(str(alias or ""))
        return self.server_snapshot()

    def server_start(self) -> dict[str, Any]:
        runtime = self.app.get_api_server_runtime()
        runtime.start(self.api_settings.load())
        return self.server_snapshot()

    def server_stop(self) -> dict[str, Any]:
        runtime = getattr(self.app, "api_server_runtime", None)
        if runtime is not None:
            runtime.stop()
        return self.server_snapshot()

    def server_upload_feedback(self, alias: str) -> dict[str, Any]:
        assignment = self.api_aliases.get(str(alias or ""))
        if assignment is None:
            raise ValueError("API model assignment was not found.")
        runtime = self.app.get_api_server_runtime()
        if not runtime.upload_feedback(assignment.model_id):
            raise RuntimeError("Sign in to Community before uploading pending feedback.")
        return {"queued": True}

    def add_headstamp(self, name: str) -> None:
        self.ensure_stopped()
        if self.settings.get_active_model_id() is None:
            raise ValueError("Remote classifications are managed by the AI server. Use Load Classifications.")
        if not self.app.config.add_headstamp(str(name or "").strip()):
            raise ValueError("Classification is blank or already exists.")
        self.app.bus.post("mode/changed", {"reason": "headstamps"})

    def remove_headstamp(self, name: str) -> None:
        self.ensure_stopped()
        if self.settings.get_active_model_id() is None:
            raise ValueError("Remote classifications are managed by the AI server and cannot be removed here.")
        if not self.app.config.remove_headstamp(str(name or "").strip()):
            raise ValueError("Classification not found.")
        self.app.bus.post("mode/changed", {"reason": "headstamps"})

    def parent_action(self, action: str, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        model_id = self.settings.get_active_model_id()
        if model_id is None:
            raise RuntimeError("Parent classifications require an active local model.")
        if action == "add":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("Parent name is required.")
            if self.parents.find_by_name(model_id, name) is not None:
                raise ValueError("That parent classification already exists.")
            self.parents.add(model_id, name)
        elif action == "rename":
            parent_id = int(payload.get("id", 0) or 0)
            name = str(payload.get("name") or "").strip()
            parent = self.parents.get(parent_id)
            if parent is None or parent.model_id != model_id or not name:
                raise ValueError("Invalid parent classification.")
            self.parents.rename(parent_id, name)
        elif action == "delete":
            parent_id = int(payload.get("id", 0) or 0)
            parent = self.parents.get(parent_id)
            if parent is None or parent.model_id != model_id:
                raise ValueError("Invalid parent classification.")
            self.parents.delete(parent_id)
        elif action == "assign":
            headstamp_id = int(payload.get("headstamp_id", 0) or 0)
            raw_parent = payload.get("parent_id")
            parent_id = (
                int(raw_parent) if raw_parent not in (None, "", 0, "0") else None
            )
            headstamp = next(
                (
                    item for item in self.headstamps.list_for_model(model_id)
                    if item.id == headstamp_id
                ),
                None,
            )
            if headstamp is None:
                raise ValueError("Invalid classification.")
            if parent_id is not None:
                parent = self.parents.get(parent_id)
                if parent is None or parent.model_id != model_id:
                    raise ValueError("Invalid parent classification.")
            self.headstamps.set_parent(headstamp_id, parent_id)
        else:
            raise ValueError("Unknown parent-classification action.")
        self.app.bus.post("mode/changed", {"reason": "parents"})

    def remote_snapshot(self) -> dict[str, Any]:
        cfg = self.app.config.api
        key = str(cfg.get("api_key") or "").strip()
        return {
            "endpoint_url": str(cfg.get("endpoint_url") or ""),
            "model": str(cfg.get("model") or ""),
            "prompt": str(cfg.get("prompt") or ""),
            "image_quality": int(cfg.get("image_quality", 100) or 100),
            "image_scale": int(cfg.get("image_scale", 100) or 100),
            "api_key_configured": bool(key),
            "headstamps": [
                str(item.get("name") or "")
                for item in self.app.config.headstamps
                if str(item.get("name") or "").strip()
            ],
        }

    def save_remote(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure_stopped()
        cfg = self.app.config.api
        cfg["endpoint_url"] = str(payload.get("endpoint_url", cfg.get("endpoint_url", ""))).strip()
        cfg["model"] = str(payload.get("model", cfg.get("model", ""))).strip()
        cfg["prompt"] = str(payload.get("prompt", cfg.get("prompt", "")))[:32000]
        cfg["image_quality"] = max(10, min(100, int(payload.get("image_quality", cfg.get("image_quality", 100)))))
        cfg["image_scale"] = max(10, min(200, int(payload.get("image_scale", cfg.get("image_scale", 100)))))
        if payload.get("clear_api_key"):
            cfg["api_key"] = ""
        elif payload.get("api_key"):
            cfg["api_key"] = str(payload["api_key"])
        self.app.config.save()
        return self.remote_snapshot()

    def test_remote(self) -> dict[str, Any]:
        cfg = self.app.config.api
        endpoint = str(cfg.get("endpoint_url") or "").strip()
        selected = str(cfg.get("model") or "").strip()
        names = api_client.list_models(
            endpoint,
            str(cfg.get("api_key") or ""),
        )
        available = [str(name) for name in names]
        if selected and selected not in available:
            raise ValueError(
                f"Connected, but model {selected!r} is not currently served."
            )
        return {
            "connected": True,
            "selected_model": selected,
            "available_models": available,
        }

    def find_remote_models(self) -> dict[str, Any]:
        cfg = self.app.config.api
        names = api_client.list_models(
            str(cfg.get("endpoint_url") or ""),
            str(cfg.get("api_key") or ""),
        )
        return {"models": [str(name) for name in names]}

    def sync_remote(self) -> dict[str, Any]:
        self.ensure_stopped()
        cfg = self.app.config.api
        names = api_client.get_headstamps(
            str(cfg.get("endpoint_url") or ""), str(cfg.get("model") or ""),
            str(cfg.get("api_key") or ""),
        )
        result = self.app.config.synchronize_remote_headstamps(names)
        self.app.bus.post("run/headstamps_synced", result)
        return result

    def remote_headstamp_action(self, action: str, name: str = "") -> dict[str, Any]:
        raise ValueError("Remote classifications are managed by the AI server. Use Load Classifications to refresh them.")

    def remote_feed(self) -> dict[str, Any]:
        self.ensure_stopped()
        controller = getattr(self.app, "run_controller", None)
        broker = getattr(self.app, "broker", None)
        if controller is None or broker is None or not getattr(broker, "is_connected", False):
            raise RuntimeError("The sorter controller is not connected.")
        self.owner(lambda: self.app.run_worker(controller.test_once))
        return {"started": True}

    def training_snapshot(self) -> dict[str, Any]:
        active_id = self.settings.get_active_model_id()
        model = self.models.get(active_id) if active_id is not None else None
        counts: dict[str, int] = {}
        if model is not None:
            counts = class_counts(paths.model_images_dir(model.id))
            for h in self.headstamps.list_for_model(model.id):
                counts.setdefault(h.name, 0)
        return {
            "available": model is not None,
            "active_model": model.name if model else "",
            "model_id": model.id if model else None,
            "model_mode": model.model_mode if model else "",
            "running": self.training.is_running,
            "counts": counts,
            "config": asdict(model.training_config) if model else {},
            "sort_while_training": bool(self.app.config.sort_while_training),
            "log": list(self._training_log),
            "last_crop_ready": self._training_last_crop is not None,
        }

    def set_sort_while_training(self, enabled: bool) -> dict[str, Any]:
        self.ensure_stopped()
        self.app.config.set_sort_while_training(bool(enabled))
        return {"enabled": bool(self.app.config.sort_while_training)}

    def training_feed(self, label: str = "") -> dict[str, Any]:
        self.ensure_stopped()
        if self.settings.get_active_model_id() is None:
            raise RuntimeError("Activate a local model first.")
        feed_slot = 0
        clean_label = str(label or "").strip()
        if self.app.config.sort_while_training and clean_label:
            feed_slot = int(self.app.config.slot_for_headstamp(clean_label) or 0)
        with self._training_lock:
            broker = getattr(self.app, "broker", None)
            if broker is not None and not broker.force_sort_and_move(feed_slot):
                raise RuntimeError("Feed timeout.")
            frame = self.app.capture_frame()
            if frame is None:
                raise RuntimeError("No camera frame is available.")
            crop = image_proc.crop_headstamp(frame, self.app.config.image_proc)
            cfg = self.app.config.image_proc
            crop = image_proc.apply_primer_mask(
                crop, cfg.get("primer_mode", "none"), int(cfg.get("primer_radius", 0)),
            )
            self._training_last_crop = crop
        return {"captured": True}

    def training_crop_jpeg(self) -> bytes | None:
        with self._training_lock:
            frame = None if self._training_last_crop is None else self._training_last_crop.copy()
        return _jpeg(frame, 86)

    def training_save(self, label: str) -> dict[str, Any]:
        active_id = self.settings.get_active_model_id()
        if active_id is None:
            raise RuntimeError("Activate a local model first.")
        label = str(label or "").strip()
        if not label:
            raise ValueError("A classification label is required.")
        with self._training_lock:
            if self._training_last_crop is None:
                raise RuntimeError("Capture an image first.")
            dest = save_training_image(
                self._training_last_crop, paths.model_images_dir(active_id), label,
            )
            self._training_last_crop = None
        self.app.config.add_headstamp(label)
        return {"filename": dest.name}

    def training_start(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        active_id = self.settings.get_active_model_id()
        model = self.models.get(active_id) if active_id is not None else None
        if model is None:
            raise RuntimeError("Activate a local model first.")
        if self.training.is_running:
            raise RuntimeError("Training is already running.")
        image_dir = paths.model_images_dir(model.id)
        if not image_dir.exists() or not any(image_dir.iterdir()):
            raise RuntimeError("Save training images before starting training.")
        cfg = model.training_config
        for key in (
            "epochs", "batch_size", "learning_rate", "weight_decay", "dropout_rate",
            "val_split", "image_size", "max_workers", "stochastic_depth_prob",
            "focal_gamma", "swa_start", "swa_acc_threshold", "swa_patience",
            "swa_min_epoch",
        ):
            if key in payload:
                current = getattr(cfg, key)
                setattr(cfg, key, type(current)(payload[key]))
        for key in ("train_all", "freeze_backbone", "use_focal_loss", "use_swa"):
            if key in payload:
                setattr(cfg, key, bool(payload[key]))
        if "swa_mode" in payload:
            mode = str(payload["swa_mode"])
            if mode not in {"scheduled", "adaptive"}:
                raise ValueError("Unknown SWA mode.")
            cfg.swa_mode = mode
        cfg.model_name = model.model_mode
        self.models.update(model)
        paths.ensure_model_subtree(model.id)
        output = paths.model_trained_path(model.id)
        self._training_model_id = model.id
        self._training_output = output
        self._training_log.clear()
        self.training.spawn(TrainingJob(image_dir=image_dir, output_model=output, config=cfg))

    def saved_bins_snapshot(self) -> dict[str, Any]:
        return self.saved_bins_snapshot_for("")

    def _saved_bins_targets(self) -> list[tuple[str, SavedBinsTarget]]:
        service = SavedBinsService(self.db, self.app.config)
        targets: list[tuple[str, SavedBinsTarget]] = []
        remote_name = str(self.app.config.api.get("model") or "").strip()
        if remote_name:
            targets.append(("remote", SavedBinsTarget.remote(remote_name, remote_name)))
        for model in self.models.list():
            if model.id is not None:
                targets.append((f"local:{model.id}", service.local_target(model.id)))
        return targets

    def _saved_bins_target(self, key: str = "") -> tuple[str, SavedBinsTarget]:
        targets = self._saved_bins_targets()
        wanted = str(key or "").strip()
        if wanted:
            for target_key, target in targets:
                if target_key == wanted:
                    return target_key, target
            raise ValueError("The selected Saved Bins target is no longer available.")
        active_id = self.settings.get_active_model_id()
        preferred = f"local:{active_id}" if active_id is not None else "remote"
        for target_key, target in targets:
            if target_key == preferred:
                return target_key, target
        if not targets:
            raise RuntimeError("Configure a local or remote model first.")
        return targets[0]

    def _sync_saved_bins_remote(self, target: SavedBinsTarget) -> None:
        if not target.is_remote:
            return
        cfg = self.app.config.api
        names = api_client.get_headstamps(
            str(cfg.get("endpoint_url") or ""),
            str(cfg.get("model") or ""),
            str(cfg.get("api_key") or ""),
        )
        result = self.app.config.synchronize_remote_headstamps(names)
        self.app.bus.post("run/headstamps_synced", result)

    def saved_bins_snapshot_for(self, target_key: str) -> dict[str, Any]:
        store = SavedBinsStore(paths.saved_bins_dir())
        profiles, errors = store.scan()
        service = SavedBinsService(self.db, self.app.config)
        selected_key, target = self._saved_bins_target(target_key)
        profiles = [
            profile for profile in profiles
            if profile.caliber.strip().casefold() == target.caliber.strip().casefold()
        ]
        rows = []
        for profile in profiles:
            row = profile.to_dict()
            row["filename"] = profile.path.name if profile.path else ""
            try:
                analysis = service.analyse(
                    profile, target,
                    int(self.app.config.serial.get("slot_quantity", 8) or 8),
                )
                row["analysis"] = _safe_json(analysis)
                row["editor_assignments"] = _safe_json(
                    service.edit_rows(profile, target)
                )
            except Exception as exc:
                row["analysis_error"] = str(exc)
            rows.append(row)
        targets = [
            {
                "key": key,
                "kind": item.kind,
                "caliber": item.caliber,
                "name": item.name,
                "label": (
                    f"Remote API — {item.name}"
                    if item.is_remote else f"{item.caliber} — {item.name}"
                ),
            }
            for key, item in self._saved_bins_targets()
        ]
        return {
            "profiles": rows,
            "errors": errors,
            "targets": targets,
            "slot_quantity":int(self.app.config.serial.get("slot_quantity",8)),
            "selected_target": selected_key,
            "slot_quantity": int(self.app.config.serial.get("slot_quantity", 8) or 8),
        }

    def saved_bins_save(self, name: str, target_key: str = "") -> dict[str, Any]:
        self.ensure_stopped()
        store = SavedBinsStore(paths.saved_bins_dir())
        service = SavedBinsService(self.db, self.app.config)
        _selected_key, target = self._saved_bins_target(target_key)
        self._sync_saved_bins_remote(target)
        profile = service.snapshot(target, str(name or "").strip())
        path = store.path_for_name(profile.layout_name)
        if path.exists():
            existing = store.read(path)
            profile = service.replace_target_assignments(
                existing,
                target,
                profile.assignments,
                int(self.app.config.serial.get("slot_quantity", 8) or 8),
            )
            path = store.write(profile, path=path, overwrite=True)
        else:
            path = store.write(profile)
        return {"filename": path.name}

    def saved_bins_apply(self, filename: str, target_key: str = "") -> dict[str, Any]:
        self.ensure_stopped()
        store = SavedBinsStore(paths.saved_bins_dir())
        safe = Path(str(filename or "")).name
        profile = store.read(store.directory / safe)
        service = SavedBinsService(self.db, self.app.config)
        _selected_key, target = self._saved_bins_target(target_key)
        self._sync_saved_bins_remote(target)
        result = service.apply(
            profile, target,
            int(self.app.config.serial.get("slot_quantity", 8) or 8),
        )
        self.app.config.reload_headstamps_for_active_model()
        self.app.bus.post(
            "mode/changed",
            {"active_model_id": target.model_id, "source": "web_saved_bins"},
        )
        self.owner(self.app.refresh_saved_bins_runtime)
        return _safe_json(result)

    def saved_bins_update(
        self, filename: str, assignments: object, target_key: str = "",
    ) -> dict[str, Any]:
        self.ensure_stopped()
        store = SavedBinsStore(paths.saved_bins_dir())
        safe = Path(str(filename or "")).name
        path = store.directory / safe
        if path.parent.resolve() != store.directory.resolve() or not path.is_file():
            raise ValueError("The selected Saved Bins layout was not found.")
        if not isinstance(assignments, list):
            raise ValueError("Saved Bins assignments must be a list.")
        profile = store.read(path)
        replacements = [SavedBinAssignment.from_dict(item) for item in assignments]
        keys = [item.key for item in replacements]
        if len(keys) != len(set(keys)):
            raise ValueError("Saved Bins file contains duplicate classifications.")
        service = SavedBinsService(self.db, self.app.config)
        _selected_key, target = self._saved_bins_target(target_key)
        self._sync_saved_bins_remote(target)
        profile = service.replace_target_assignments(
            profile,
            target,
            replacements,
            int(self.app.config.serial.get("slot_quantity", 8) or 8),
        )
        store.write(profile, path=path, overwrite=True)
        return {"filename": path.name}

    def saved_bins_delete(self, filename: str) -> dict[str, Any]:
        self.ensure_stopped()
        store = SavedBinsStore(paths.saved_bins_dir())
        safe = Path(str(filename or "")).name
        path = store.directory / safe
        if (
            not safe.casefold().endswith(FILE_SUFFIX)
            or path.parent.resolve() != store.directory.resolve()
            or not path.is_file()
        ):
            raise ValueError("The selected Saved Bins layout was not found.")
        # Validate before removal so unrelated files can never be targeted.
        store.read(path)
        path.unlink()
        return {"filename": safe}

    def usb_snapshot(self) -> dict[str, Any]:
        """Discover removable drives only after the technician asks."""
        drives = []
        for root in removable_usb_roots():
            profiles, errors = SavedBinsStore(root).scan()
            drives.append({
                "root": str(root),
                "layouts": [
                    {
                        "filename": profile.path.name if profile.path else "",
                        "layout_name": profile.layout_name,
                        "caliber": profile.caliber,
                    }
                    for profile in profiles
                ],
                "errors": errors,
            })
        return {"drives": drives}

    def saved_bins_import_usb(
        self, root: str, filename: str, *, overwrite: bool = False,
    ) -> dict[str, Any]:
        self.ensure_stopped()
        usb_root = resolve_usb_root(root)
        safe = Path(str(filename or "")).name
        if not safe.casefold().endswith(FILE_SUFFIX):
            raise ValueError(f"Saved Bins filenames must end in {FILE_SUFFIX}.")
        source = usb_root / safe
        if source.parent.resolve() != usb_root.resolve() or not source.is_file():
            raise ValueError("The selected USB layout was not found.")
        profile = SavedBinsStore(usb_root).read(source)
        destination = SavedBinsStore(paths.saved_bins_dir()).path_for_name(
            profile.layout_name
        )
        copy_layout_file(source, destination, overwrite=bool(overwrite))
        # Re-read the copied file so success means the local copy is valid.
        SavedBinsStore(paths.saved_bins_dir()).read(destination)
        return {"filename": destination.name}

    def saved_bins_export_usb(self, root: str, filename: str) -> dict[str, Any]:
        usb_root = resolve_usb_root(root)
        safe = Path(str(filename or "")).name
        local_root = paths.saved_bins_dir()
        source = local_root / safe
        if (
            not safe.casefold().endswith(FILE_SUFFIX)
            or source.parent.resolve() != local_root.resolve()
            or not source.is_file()
        ):
            raise ValueError("The selected local Saved Bins layout was not found.")
        destination = usb_root / source.name
        counter = 1
        while destination.exists():
            stem = source.name[: -len(FILE_SUFFIX)]
            destination = usb_root / f"{stem}-{counter}{FILE_SUFFIX}"
            counter += 1
        copy_layout_file(source, destination)
        return {"filename": destination.name, "root": str(usb_root)}

    def camera_snapshot(self) -> dict[str, Any]:
        camera = getattr(self.app, "camera", None)
        return {
            "config": dict(self.app.config.camera),
            "diagnostic": camera.diagnostic_info() if camera and hasattr(camera, "diagnostic_info") else {},
        }

    def camera_detect(self) -> dict[str, Any]:
        """Perform the same explicit, temporary probe used by the Desktop tab."""
        from . import camera as camera_mod

        self.ensure_stopped()
        self.owner(self.app.stop_camera)
        try:
            cameras = self.app.detect_cameras()
        finally:
            self.owner(self.app.start_camera, 30.0)
        return {"cameras": _safe_json(cameras)}

    def camera_apply(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        cfg = self.app.config.camera
        from .transports.network import validate_camera_mode
        validate_camera_mode(getattr(self.app, 'broker', None), payload.get('width', cfg.get('width', 1920)), payload.get('height', cfg.get('height', 1080)))
        cfg["device_index"] = max(0, int(payload.get("device_index", cfg.get("device_index", 0))))
        cfg["width"] = max(1, int(payload.get("width", cfg.get("width", 1920))))
        cfg["height"] = max(1, int(payload.get("height", cfg.get("height", 1080))))
        cfg["device_chosen"] = True
        cfg["prefer_by_usb_id"] = bool(payload.get("prefer_by_usb_id", cfg.get("prefer_by_usb_id", True)))
        if payload.get("preferred_vid"):
            cfg["preferred_vid"] = str(payload.get("preferred_vid"))
        if payload.get("preferred_pid"):
            cfg["preferred_pid"] = str(payload.get("preferred_pid"))
        self.app.config.save()
        self.owner(lambda: self.app.restart_camera(cfg["device_index"], cfg["width"], cfg["height"]), 30.0)

    def camera_jpeg(self) -> bytes | None:
        camera = getattr(self.app, "camera", None)
        frame = camera.latest_frame() if camera is not None else None
        return _jpeg(frame, 76)

    def image_snapshot(self) -> dict[str, Any]:
        return {"image_proc": dict(self.app.config.image_proc), "camera_led": self.app.saved_camera_led()}

    def image_save(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        cfg = self.app.config.image_proc
        for key in ("primer_mode", "primer_radius"):
            if key in payload:
                cfg[key] = payload[key]
        h = payload.get("hough")
        if isinstance(h, dict):
            cfg["hough"] = {**dict(cfg.get("hough", {})), **h}
        if "camera_led" in payload:
            raw = max(0, min(255, int(payload["camera_led"])))
            self.app.config.serial.setdefault("init_settings", {})["cameraledlevel"] = raw
            self.owner(lambda: self.app.set_camera_led(raw, source="web", persist=True))
        self.app.config.save()

    def image_capture(self) -> dict[str, Any]:
        frame = self.app.capture_frame()
        if frame is None:
            raise RuntimeError("No camera frame is available.")
        cfg = self.app.config.image_proc
        params = image_proc.HoughParams.from_dict(cfg.get("hough", {}))
        detection = image_proc.hough_detect(frame, params)
        before = image_proc.overlay_detection(frame, detection)
        after = image_proc.crop_headstamp(frame, cfg)
        after = image_proc.apply_primer_mask(
            after,
            str(cfg.get("primer_mode", "none")),
            int(cfg.get("primer_radius", 0)),
        )
        before_jpeg = _jpeg(before, 78)
        after_jpeg = _jpeg(after, 84)
        return {
            "detection": list(detection) if detection is not None else None,
            "before": base64.b64encode(before_jpeg).decode("ascii") if before_jpeg else "",
            "after": base64.b64encode(after_jpeg).decode("ascii") if after_jpeg else "",
        }

    def serial_snapshot(self) -> dict[str, Any]:
        from . import serial_broker
        broker = getattr(self.app, "broker", None)
        with self._serial_lock:
            log = list(self._serial_log)
        from .sorter_profiles import SorterProfiles
        profiles=SorterProfiles(self.app.config)
        return {
            "sorter_profiles":profiles.names(), "active_sorter":profiles.active(),
            "config": dict(self.app.config.serial),
            "ports": list(dict.fromkeys([str(self.app.config.serial.get("port", "")), *serial_broker.list_serial_ports()])),
            "connected": bool(broker and getattr(broker, "is_connected", False)),
            "firmware": str(getattr(broker, "firmware_version", "") or "") if broker else "",
            "log": log,
        }

    def serial_save(self, payload: dict[str, Any]) -> None:
        self.ensure_stopped()
        cfg = self.app.config.serial
        if 'wifi_enabled' in payload:
            from .connections import connection_settings
            cfg = connection_settings(cfg,wifi_enabled=bool(payload['wifi_enabled']),
                address=payload.get('wifi_address',cfg.get('wifi_address','')),
                usb_port=payload.get('usb_port',cfg.get('usb_port','')))
            self.app.config.data['serial']=cfg
        for key in (("baud", "slot_quantity", "handshake_timeout_s", "init_on_startup") if "wifi_enabled" in payload else ("port", "baud", "slot_quantity", "handshake_timeout_s", "init_on_startup")):
            if key in payload:
                cfg[key] = payload[key]
        if isinstance(payload.get("init_settings"), dict):
            current = dict(cfg.get("init_settings", {}))
            for key, value in payload["init_settings"].items():
                if key in current or key == "sortsteps":
                    current[key] = int(value)
            cfg["init_settings"] = current
        self.app.config.save()
        tab = getattr(self.app, 'serial_tab', None)
        if tab is not None:
            self.owner(tab.load_connection_form)

    def serial_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure_stopped()
        if action == 'save-sorter':
            self.serial_save(payload)
            from .sorter_profiles import SorterProfiles
            name=SorterProfiles(self.app.config).save(payload.get('name',''))
            return {'name':name}
        elif action == 'load-sorter':
            self.owner(lambda:self.app.select_sorter_profile(str(payload.get('name',''))),30.0)
        elif action == "connect":
            if 'wifi_enabled' in payload:
                self.serial_save(payload)
                port=self.app.config.serial['port']
            else:
                port=str(payload.get('port') or '')
            self.owner(lambda: self.app.connect_serial(port), 30.0)
        elif action == "disconnect":
            self.owner(self.app.disconnect_serial)
        elif action == "command":
            broker = getattr(self.app, "broker", None)
            command = str(payload.get("command") or "").strip()
            if not broker or not getattr(broker, "is_connected", False):
                raise RuntimeError("Serial controller is not connected.")
            if not command or len(command) > 256:
                raise ValueError("Invalid serial command.")
            broker.send_command(command)
        elif action == "sort-to":
            broker = getattr(self.app, "broker", None)
            if not broker or not getattr(broker, "is_connected", False):
                raise RuntimeError("Serial controller is not connected.")
            broker.move_sorter_to_slot(int(payload.get("slot", 0)))
        elif action == "get-config":
            broker = getattr(self.app, "broker", None)
            if not broker or not getattr(broker, "is_connected", False):
                raise RuntimeError("Serial controller is not connected.")
            from .machine_settings import normalized_board_config
            values = normalized_board_config(broker.get_config() or {})
            return {"config": _safe_json(values)}
        elif action == "push-config":
            broker = getattr(self.app, "broker", None)
            if not broker or not getattr(broker, "is_connected", False):
                raise RuntimeError("Serial controller is not connected.")
            settings = dict(self.app.config.serial.get("init_settings", {}))
            if not bool(int(settings.get("airdropenabled", 0) or 0)):
                for key in (
                    "airdroppredelay", "airdropdsignalduration", "airdroppostdelay",
                ):
                    settings.pop(key, None)
            broker.update_init_settings(settings)
            return {"pushed": True}
        else:
            raise ValueError("Unknown serial action.")
        return None

    def diagnostics_mark(self, detail: str) -> dict[str, Any]:
        collector = getattr(self.app, "diagnostics", None)
        if collector is None:
            raise RuntimeError("Enable Diagnostics before marking an event.")
        collector.record_event("web_mark", str(detail or "Technician mark")[:500])
        return self.diagnostics_snapshot()

    def community_snapshot(self) -> dict[str, Any]:
        auth = getattr(self.app, "auth", None)
        signed_in = bool(auth and auth.is_authenticated())
        name = email = None
        if signed_in:
            try:
                name, email = auth.identity()
            except Exception:
                pass
        return {"signed_in": signed_in, "name": name or "", "email": email or ""}

    def community_login(self) -> dict[str, Any]:
        auth = getattr(self.app, "auth", None)
        if auth is None:
            raise RuntimeError("Community authentication is unavailable.")
        auth.login_interactive()
        return self.community_snapshot()

    def community_logout(self) -> None:
        auth = getattr(self.app, "auth", None)
        if auth is not None:
            auth.logout()

    def community_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .community_api import CommunityApi
        auth = getattr(self.app, "auth", None)
        if auth is None or not auth.is_authenticated():
            raise RuntimeError("Sign in to the community first.")
        api = CommunityApi(auth=auth)
        models = api.get_models(
            search=str(payload.get("search") or ""),
            model_type=str(payload.get("model_type") or ""),
            cartridge=str(payload.get("cartridge") or ""),
        )
        installed = {m.community_model_uid: m for m in self.models.list() if m.community_model_uid}
        return {"models": [
            {
                **asdict(info),
                "installed_version": int(installed[info.model_uid].model_version)
                if info.model_uid in installed else None,
            }
            for info in models
        ]}

    def community_download(self, uid: str, version: int = 1) -> dict[str, Any]:
        self.ensure_stopped()
        from .community_api import CommunityApi
        auth = getattr(self.app, "auth", None)
        if auth is None or not auth.is_authenticated():
            raise RuntimeError("Sign in to the community first.")
        api = CommunityApi(auth=auth)
        payload = api.request_download(str(uid))
        url = payload.get("FullUrl") or payload.get("fullUrl")
        if not url:
            raise RuntimeError("Community server returned no download URL.")
        with tempfile.TemporaryDirectory(prefix="sp-community-") as tmp:
            source = Path(tmp) / "community-model.zip"
            api.download_to(url, source)
            _cart, model_id = import_model(
                source,
                db=self.db,
                community_download=True,
            )
            record_installed_version(self.db, model_id, int(version))
        self.state.publish("models")
        return {"model_id": model_id}

    def diagnostics_enabled(self) -> bool:
        return getattr(self.app, "diagnostics", None) is not None

    def diagnostics_toggle(self, enabled: bool) -> dict[str, Any]:
        if enabled and not self.diagnostics_enabled():
            self.owner(self.app._enable_diagnostics)
            self._web_diagnostics = _WebDiagnosticState()
        elif not enabled and self.diagnostics_enabled():
            state = self._web_diagnostics
            if state is not None and state.busy:
                if state.runner is not None:
                    state.runner.cancel()
                raise RuntimeError(
                    "The sensor test is reaching a safe stop point. Disable "
                    "Diagnostics after it finishes restoring settings."
                )
            ok = self.owner(self.app._disable_diagnostics)
            if ok is False:
                raise RuntimeError("A sensor test is still reaching a safe stop point.")
            self._web_diagnostics = None
        return self.diagnostics_snapshot()

    def _require_web_diagnostics(self) -> _WebDiagnosticState:
        state = self._web_diagnostics
        if not self.diagnostics_enabled():
            raise RuntimeError("Enable Diagnostics before using this control.")
        if state is None:
            state = _WebDiagnosticState()
            self._web_diagnostics = state
        return state

    def _sensor_preconditions(self) -> _WebDiagnosticState:
        state = self._require_web_diagnostics()
        if state.busy:
            raise RuntimeError("A sensor test is already running.")
        self.ensure_stopped()
        broker = getattr(self.app, "broker", None)
        if broker is None or not getattr(broker, "is_connected", False):
            raise RuntimeError("Connect the Arduino controller first.")
        return state

    def _sensor_progress(self, message: str) -> None:
        state = self._web_diagnostics
        if state is None:
            return
        line = f"{time.strftime('%H:%M:%S')}  {str(message)}"
        state.status = str(message)
        state.progress.append(line)
        self.state.publish("diagnostics")

    def _run_sensor_async(
        self,
        state: _WebDiagnosticState,
        phase: str,
        function: Callable[[], dict[str, Any]],
        *, await_cases: bool = False,
    ) -> None:
        state.busy = True
        state.phase = phase
        state.result = None

        def worker() -> None:
            try:
                result = function()
            except Exception as exc:
                result = {
                    "test": phase,
                    "status": "failed",
                    "summary": str(exc) or exc.__class__.__name__,
                    "settings_restored": False,
                }
            current = self._web_diagnostics
            if current is not state:
                return
            current.busy = False
            current.result = result
            status = str(result.get("status", "failed")).casefold()
            summary = str(result.get("summary", "No result was returned."))
            current.progress.append(
                f"{time.strftime('%H:%M:%S')}  {status.upper()}: {summary}"
            )
            if await_cases and status == "pass":
                current.phase = "awaiting_cases"
                current.status = (
                    "Empty proximity state passed. Load at least three cases, "
                    "then press Continue Classifier Test."
                )
            else:
                current.phase = "complete"
                current.status = f"{status.upper()}: {summary}"
                current.runner = None
            collector = getattr(self.app, "diagnostics", None)
            if collector is not None:
                collector.record_sensor_test(result)
            self.state.publish("diagnostics")

        threading.Thread(
            target=worker,
            name=f"WebSensorDiagnostic-{phase}",
            daemon=True,
        ).start()

    def sensor_start(self, kind: str) -> dict[str, Any]:
        state = self._sensor_preconditions()
        runner = SensorDiagnosticRunner(
            self.app.broker,
            progress=self._sensor_progress,
        )
        state.runner = runner
        state.progress.clear()
        if kind == "classifier":
            state.status = "Checking the empty classifier proximity state…"
            self._run_sensor_async(
                state,
                "classifier_proximity_clear",
                runner.run_proximity_clear_check,
                await_cases=True,
            )
        elif kind == "sorter":
            state.status = "Running sorter 0 → 5 → 0 homing test…"
            self._run_sensor_async(state, "sorter_home", runner.run_sorter_test)
        else:
            state.runner = None
            raise ValueError("Unknown sensor test.")
        return self.diagnostics_snapshot()

    def sensor_continue_classifier(self) -> dict[str, Any]:
        state = self._require_web_diagnostics()
        if state.busy or state.phase != "awaiting_cases" or state.runner is None:
            raise RuntimeError("The classifier test is not waiting for loaded cases.")
        state.status = "Running three normal classifier cycles and homing probes…"
        self._run_sensor_async(
            state, "classifier_sensors", state.runner.run_classifier_test
        )
        return self.diagnostics_snapshot()

    def sensor_cancel(self) -> dict[str, Any]:
        state = self._require_web_diagnostics()
        if state.runner is not None:
            state.runner.cancel()
            state.status = (
                "Cancellation requested; waiting for the active command and "
                "safe setting restoration."
            )
        return self.diagnostics_snapshot()

    def diagnostics_snapshot(self) -> dict[str, Any]:
        collector = getattr(self.app, "diagnostics", None)
        state = self._web_diagnostics
        result = {
            "enabled": collector is not None,
            "camera": self.camera_snapshot().get("diagnostic", {}),
            "saved_camera_led": self.app.saved_camera_led(),
            "message": "Diagnostics are disabled and consume no diagnostic resources."
            if collector is None else "Diagnostics are enabled.",
        }
        if collector is not None:
            result["live"] = _safe_json(collector.live_stats())
            result["sensor"] = {
                "busy": bool(state and state.busy),
                "phase": state.phase if state else "desktop",
                "status": state.status if state else "Use the Desktop Diagnostics tab.",
                "progress": list(state.progress) if state else [],
                "result": state.result if state else None,
            }
        return result

    def diagnostics_export(self, destination: Path) -> Path:
        collector = getattr(self.app, "diagnostics", None)
        if collector is None:
            raise RuntimeError("Enable Diagnostics before exporting a report.")
        return collector.export_zip(
            destination,
            camera_info=self.app._camera_diagnostic_info(),
            app_info=self.app._diagnostic_app_info(),
            extra_members=(
                self.app.crash_reporter.export_members()
                if self.app.crash_reporter is not None else None
            ),
        )

    def system_snapshot(self) -> dict[str, Any]:
        import platform
        web = WebSettings.load(self.settings)
        server = getattr(self.app, "web_server", None)
        advertiser = getattr(server, "_mdns", None)
        return {
            "product": PRODUCT_NAME,
            "public_version": PUBLIC_VERSION,
            "sorter_name": web.sorter_name,
            "friendly_url": f"http://{web.sorter_name}.local:{web.port}",
            "mdns_active": bool(advertiser and advertiser.active),
            "mdns_error": str(getattr(advertiser, "error", "") or ""),
            "ip_urls": [
                f"http://{ip}:{web.port}"
                for ip in primary_local_ipv4_addresses()
            ],
            "diagnostic_ip_urls": [
                f"http://{ip}:{web.port}" for ip in local_ipv4_addresses()
            ],
            "port": web.port,
            "web_enabled": web.enabled,
            "desktop_enabled": web.desktop_enabled,
            "os": platform.platform(),
            "python": platform.python_version(),
        }

    def lan_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply presentation policy without restarting the active web socket."""
        current = WebSettings.load(self.settings)
        requested_name = normalize_sorter_name(
            str(payload.get("sorter_name", current.sorter_name))
        )
        requested_port = int(payload.get("port", current.port))
        if requested_name != current.sorter_name or requested_port != current.port:
            raise RuntimeError(
                "Changing the active web address requires the Desktop LAN Access tab "
                "so the service can restart without interrupting this request."
            )
        updated = WebSettings(
            enabled=True,
            desktop_enabled=bool(payload.get("desktop_enabled", current.desktop_enabled)),
            sorter_name=current.sorter_name,
            port=current.port,
        )
        updated.save(self.settings)
        if updated.desktop_enabled:
            self.owner(self.app.request_restart_in_desktop)
        else:
            def hide_desktop() -> None:
                self.app.web_only = True
                self.app.root.withdraw()
            self.owner(hide_desktop)
        return self.system_snapshot()


class WebInterfaceServer:
    def __init__(self, app: Any, settings: WebSettings) -> None:
        self.app = app
        self.settings = settings
        self._sessions: dict[str, _Session] = {}
        self._session_lock = threading.RLock()
        self._control_sid: str | None = None
        self._control_last_seen = 0.0
        self.state = WebRuntimeState(app, self._has_sessions)
        self.operations = WindowsWebOperations(app, self.state)
        self._server: _BoundedThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._mdns = None

    @property
    def address(self) -> str:
        return f"http://{self.settings.sorter_name}.local:{self.settings.port}"

    def _has_sessions(self) -> bool:
        self._prune_sessions()
        with self._session_lock:
            return bool(self._sessions)

    def _prune_sessions(self) -> None:
        now = time.monotonic()
        with self._session_lock:
            if (
                self._control_sid is not None
                and now - self._control_last_seen > CONTROL_IDLE_S
            ):
                self._control_sid = None
                self._control_last_seen = 0.0
            expired = [sid for sid, s in self._sessions.items() if now - s.last_seen > SESSION_IDLE_S]
            for sid in expired:
                self._sessions.pop(sid, None)
                if self._control_sid == sid:
                    self._control_sid = None
                    self._control_last_seen = 0.0

    def _release_control(self, sid: str) -> None:
        with self._session_lock:
            if self._control_sid == sid:
                self._control_sid = None
                self._control_last_seen = 0.0

    def _new_session(self) -> _Session:
        """Create one bounded browser session without requiring a login page."""
        now = time.monotonic()
        session = _Session(
            sid=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            created=now,
            last_seen=now,
        )
        with self._session_lock:
            self._prune_sessions()
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda item: item.last_seen)
                self._sessions.pop(oldest.sid, None)
                if self._control_sid == oldest.sid:
                    self._control_sid = None
                    self._control_last_seen = 0.0
            self._sessions[session.sid] = session
        return session

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        parent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ShibbyPrintsWeb/1"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _headers(self, cache: str = "no-store") -> None:
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", cache)
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                )

            def _json(self, status: int, value: Any) -> None:
                body = json.dumps(_safe_json(value), separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._headers()
                self.end_headers()
                self.wfile.write(body)

            def _html(self, body: str, *, cookie: str | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self._headers()
                self.end_headers()
                self.wfile.write(data)

            def _error(self, status: int, message: str) -> None:
                self._json(status, {"ok": False, "error": str(message)[:1000]})

            def _read_json(self) -> dict[str, Any]:
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                except ValueError as exc:
                    raise ValueError("Invalid request length.") from exc
                if length < 0 or length > MAX_JSON_BODY:
                    raise ValueError("Request body is too large.")
                raw = self.rfile.read(length) if length else b"{}"
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("JSON request must be an object.")
                return value

            def _session(self, *, touch: bool = True) -> _Session | None:
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                except Exception:
                    return None
                item = cookie.get("sp_web_session")
                sid = item.value if item else ""
                parent._prune_sessions()
                with parent._session_lock:
                    session = parent._sessions.get(sid)
                    if session is not None and touch:
                        session.last_seen = time.monotonic()
                    return session

            def _require_session(self) -> _Session | None:
                session = self._session()
                if session is None:
                    self._error(
                        HTTPStatus.UNAUTHORIZED,
                        "Open the sorter Web Interface page to start a browser session.",
                    )
                return session

            def _private_client_allowed(self) -> bool:
                try:
                    address = ipaddress.ip_address(str(self.client_address[0]))
                except ValueError:
                    self._error(HTTPStatus.FORBIDDEN, "Invalid client address.")
                    return False
                if address.is_global:
                    self._error(
                        HTTPStatus.FORBIDDEN,
                        "Web Interface accepts only local or private-network clients.",
                    )
                    return False
                return True

            def _require_post(self, *, technician: bool = False, control: bool = False) -> _Session | None:
                session = self._require_session()
                if session is None:
                    return None
                supplied = self.headers.get("X-Shibby-CSRF", "")
                if not supplied or not secrets.compare_digest(session.csrf, supplied):
                    self._error(HTTPStatus.FORBIDDEN, "Invalid browser session token.")
                    return None
                origin = self.headers.get("Origin", "")
                if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                    self._error(HTTPStatus.FORBIDDEN, "Cross-origin control is not allowed.")
                    return None
                if technician and not session.technician:
                    self._error(HTTPStatus.FORBIDDEN, "Technician unlock is required.")
                    return None
                if control and parent._control_sid not in (None, session.sid):
                    self._error(HTTPStatus.CONFLICT, "Another browser currently controls this sorter.")
                    return None
                if control and parent._control_sid is None:
                    parent._control_sid = session.sid
                if control and parent._control_sid == session.sid:
                    parent._control_last_seen = time.monotonic()
                return session

            def do_GET(self) -> None:  # noqa: N802
                if not self._private_client_allowed():
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)
                try:
                    if path == "/":
                        session = self._session()
                        if session is None:
                            session = parent._new_session()
                            cookie = (
                                f"sp_web_session={session.sid}; Path=/; "
                                "HttpOnly; SameSite=Strict"
                            )
                            self._html(
                                _app_page(parent.settings.sorter_name, session.csrf),
                                cookie=cookie,
                            )
                            return
                        self._html(_app_page(parent.settings.sorter_name, session.csrf))
                        return
                    if path == "/healthz":
                        self._json(HTTPStatus.OK, {"ok": True, "sorter": parent.settings.sorter_name})
                        return
                    session = self._require_session()
                    if session is None:
                        return
                    maintenance_get = (
                        path in {
                            "/api/models", "/api/run-options", "/api/training",
                            "/api/community", "/api/saved-bins", "/api/usb",
                            "/api/server",
                            "/api/camera", "/api/image-processing", "/api/serial",
                            "/api/diagnostics", "/api/training-crop.jpg",
                            "/api/camera.jpg",
                        }
                        or path.startswith("/api/model-export/")
                        or path.startswith("/api/saved-bins-export/")
                        or path == "/api/diagnostics-export"
                    )
                    if maintenance_get and not session.technician:
                        self._error(HTTPStatus.FORBIDDEN, "Technician unlock is required.")
                        return
                    if path == "/api/state":
                        self._json(HTTPStatus.OK, parent.state.snapshot(
                            control_owned=parent._control_sid == session.sid,
                            technician=session.technician,
                        ))
                    elif path == "/api/models":
                        self._json(HTTPStatus.OK, parent.operations.models_snapshot())
                    elif path == "/api/run-options":
                        self._json(HTTPStatus.OK, parent.operations.run_options())
                    elif path == "/api/training":
                        self._json(HTTPStatus.OK, parent.operations.training_snapshot())
                    elif path == "/api/community":
                        self._json(HTTPStatus.OK, parent.operations.community_snapshot())
                    elif path == "/api/saved-bins":
                        self._json(
                            HTTPStatus.OK,
                            parent.operations.saved_bins_snapshot_for(
                                str(query.get("target", [""])[0])
                            ),
                        )
                    elif path == "/api/server":
                        self._json(HTTPStatus.OK, parent.operations.server_snapshot())
                    elif path == "/api/usb":
                        self._json(HTTPStatus.OK, parent.operations.usb_snapshot())
                    elif path == "/api/camera":
                        self._json(HTTPStatus.OK, parent.operations.camera_snapshot())
                    elif path == "/api/image-processing":
                        self._json(HTTPStatus.OK, parent.operations.image_snapshot())
                    elif path == "/api/serial":
                        self._json(HTTPStatus.OK, parent.operations.serial_snapshot())
                    elif path == "/api/diagnostics":
                        self._json(HTTPStatus.OK, parent.operations.diagnostics_snapshot())
                    elif path == "/api/system":
                        self._json(HTTPStatus.OK, parent.operations.system_snapshot())
                    elif path == "/api/crop.jpg":
                        self._write_image(parent.state.crop_jpeg())
                    elif path == "/api/camera.jpg":
                        self._write_image(parent.operations.camera_jpeg())
                    elif path == "/api/training-crop.jpg":
                        self._write_image(parent.operations.training_crop_jpeg())
                    elif path == "/events":
                        self._events(session)
                    elif path.startswith("/api/model-export/"):
                        self._model_export(path)
                    elif path.startswith("/api/saved-bins-export/"):
                        self._saved_bins_export(path)
                    elif path == "/api/diagnostics-export":
                        self._diagnostics_export()
                    elif path == "/api/catch-all-export":
                        self._catch_all_export()
                    else:
                        self.send_error(HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

            def _write_image(self, data: bytes | None) -> None:
                if not data:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self._headers("private, max-age=1")
                self.end_headers()
                self.wfile.write(data)

            def _download(self, path: Path, content_type: str) -> None:
                data = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                self.send_header("Content-Length", str(len(data)))
                self._headers()
                self.end_headers()
                self.wfile.write(data)

            def _model_export(self, path: str) -> None:
                model_id = int(path.rsplit("/", 1)[-1])
                model = parent.operations.models.get(model_id)
                if model is None:
                    raise ValueError("Model not found.")
                cart = parent.operations.cartridges.get(model.cartridge_id)
                with tempfile.TemporaryDirectory(prefix="sp-export-") as tmp:
                    output = Path(tmp) / f"ShibbyPrints-Model-{model.id}.zip"
                    export_model(
                        output, model, cart.name if cart else "Unknown",
                        parent.operations.headstamps.list_for_model(model_id),
                        mode=ExportMode.MODEL_AND_IMAGES,
                        model_file=model.model_path,
                        images_dir=paths.model_images_dir(model_id),
                    )
                    self._download(output, "application/zip")

            def _saved_bins_export(self, path: str) -> None:
                filename = Path(unquote(path.rsplit("/", 1)[-1])).name
                candidate = paths.saved_bins_dir() / filename
                if candidate.parent.resolve() != paths.saved_bins_dir().resolve() or not candidate.is_file():
                    raise ValueError("Saved Bins file not found.")
                self._download(candidate, "application/json")

            def _diagnostics_export(self) -> None:
                with tempfile.TemporaryDirectory(prefix="sp-diagnostic-export-") as tmp:
                    output = Path(tmp) / (
                        f"ShibbyPrints-Diagnostics-{time.strftime('%Y-%m-%d-%H%M%S')}.zip"
                    )
                    parent.operations.diagnostics_export(output)
                    self._download(output, "application/zip")

            def _catch_all_export(self) -> None:
                with tempfile.TemporaryDirectory(prefix="sp-catch-all-") as tmp:
                    output = Path(tmp) / f"ShibbyPrints-Catch-All-{time.strftime('%Y-%m-%d-%H%M%S')}.csv"
                    output.write_bytes(parent.state.catch_all_report.csv_bytes())
                    self._download(output, "text/csv; charset=utf-8")

            def _events(self, _session: _Session) -> None:
                client = parent.state.register()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "keep-alive")
                self._headers("no-cache")
                self.end_headers()
                try:
                    self.wfile.write(b"data: {\"type\":\"connected\"}\n\n")
                    self.wfile.flush()
                    while True:
                        parent._prune_sessions()
                        with parent._session_lock:
                            if parent._sessions.get(_session.sid) is not _session:
                                break
                            _session.last_seen = time.monotonic()
                            if parent._control_sid == _session.sid:
                                parent._control_last_seen = _session.last_seen
                        try:
                            event = client.events.get(timeout=15.0)
                            line = json.dumps(_safe_json(event), separators=(",", ":"))
                            self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    parent.state.unregister(client.token)

            def do_POST(self) -> None:  # noqa: N802
                if not self._private_client_allowed():
                    return
                path = urlparse(self.path).path
                if path.startswith("/api/upload/"):
                    self._upload(path)
                    return
                tech = path.startswith((
                    "/api/models/", "/api/training/", "/api/community/",
                    "/api/camera/", "/api/image-processing/", "/api/serial/",
                    "/api/diagnostics/", "/api/system/", "/api/remote/",
                    "/api/saved-bins/", "/api/usb/", "/api/run-options",
                    "/api/server/", "/api/lan/",
                ))
                control = path.startswith((
                    "/api/run/", "/api/run-options", "/api/routing/",
                    "/api/saved-bins/apply",
                ))
                session = self._require_post(technician=tech, control=control)
                if session is None:
                    return
                try:
                    payload = self._read_json()
                    result = self._dispatch(path, payload, session)
                    self._json(HTTPStatus.OK, {"ok": True, **(result or {})})
                except FileExistsError as exc:
                    self._error(HTTPStatus.CONFLICT, str(exc))
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                except RuntimeError as exc:
                    self._error(HTTPStatus.CONFLICT, str(exc))
                except Exception as exc:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

            def _dispatch(self, path: str, p: dict[str, Any], session: _Session) -> dict[str, Any] | None:
                o = parent.operations
                if path == "/api/control/claim":
                    force = bool(p.get("force")) and session.technician
                    if parent._control_sid not in (None, session.sid) and not force:
                        raise RuntimeError("Another browser controls this sorter.")
                    parent._control_sid = session.sid
                    parent._control_last_seen = time.monotonic()
                    return {}
                if path == "/api/control/release":
                    parent._release_control(session.sid)
                    return {}
                if path == "/api/technician/unlock":
                    session.technician_until = time.monotonic() + TECH_UNLOCK_S; return {}
                if path.startswith("/api/run/"): return o.run_action(path.rsplit("/", 1)[-1], p)
                if path == "/api/run-options": return o.save_run_options(p)
                if path == "/api/routing/assign": o.set_assignment(p); return {}
                if path == "/api/models/activate": o.activate_model(int(p["model_id"]) if p.get("model_id") is not None else None); return {}
                if path == "/api/models/profile": o.set_software_profile(str(p.get("profile") or "")); return {}
                if path == "/api/models/create": return o.create_model(p)
                if path == "/api/models/update": o.update_model(p); return {}
                if path == "/api/models/feedback-upload": o.feedback_upload(int(p.get("model_id", 0))); return {}
                if path == "/api/models/delete": o.delete_model(int(p.get("model_id", 0))); return {}
                if path == "/api/models/headstamp/add": o.add_headstamp(str(p.get("name") or "")); return {}
                if path == "/api/models/headstamp/remove": o.remove_headstamp(str(p.get("name") or "")); return {}
                if path.startswith("/api/models/parent/"):
                    o.parent_action(path.rsplit("/", 1)[-1], p); return {}
                if path == "/api/remote/save": return o.save_remote(p)
                if path == "/api/remote/test": return o.test_remote()
                if path == "/api/remote/models": return o.find_remote_models()
                if path == "/api/remote/sync": return o.sync_remote()
                if path == "/api/remote/feed": return o.remote_feed()
                if path.startswith("/api/remote/headstamps/"):
                    return o.remote_headstamp_action(
                        path.rsplit("/", 1)[-1], str(p.get("name") or "")
                    )
                if path == "/api/training/feed": return o.training_feed(
                    str(p.get("label") or "")
                )
                if path == "/api/training/sort-while": return o.set_sort_while_training(
                    bool(p.get("enabled"))
                )
                if path == "/api/training/save": return o.training_save(str(p.get("label") or ""))
                if path == "/api/training/start": o.training_start(p); return {}
                if path == "/api/training/cancel": o.training.cancel(); return {}
                if path == "/api/community/login": return o.community_login()
                if path == "/api/community/logout": o.community_logout(); return {}
                if path == "/api/community/catalog": return o.community_catalog(p)
                if path == "/api/community/download": return o.community_download(str(p.get("uid") or ""), int(p.get("version", 1)))
                if path == "/api/saved-bins/save": return o.saved_bins_save(
                    str(p.get("name") or ""), str(p.get("target") or "")
                )
                if path == "/api/saved-bins/apply": return o.saved_bins_apply(
                    str(p.get("filename") or ""), str(p.get("target") or "")
                )
                if path == "/api/saved-bins/update": return o.saved_bins_update(
                    str(p.get("filename") or ""), p.get("assignments"),
                    str(p.get("target") or "")
                )
                if path == "/api/saved-bins/delete": return o.saved_bins_delete(
                    str(p.get("filename") or "")
                )
                if path == "/api/usb/import-bins": return o.saved_bins_import_usb(
                    str(p.get("root") or ""), str(p.get("filename") or ""),
                    overwrite=bool(p.get("overwrite")),
                )
                if path == "/api/usb/export-bins": return o.saved_bins_export_usb(
                    str(p.get("root") or ""), str(p.get("filename") or "")
                )
                if path == "/api/camera/apply": o.camera_apply(p); return {}
                if path == "/api/camera/detect": return o.camera_detect()
                if path == "/api/camera/start": o.owner(o.app.start_camera, 30); return {}
                if path == "/api/camera/stop": o.owner(o.app.stop_camera); return {}
                if path == "/api/image-processing/save": o.image_save(p); return {}
                if path == "/api/image-processing/capture": return o.image_capture()
                if path == "/api/serial/save": o.serial_save(p); return {}
                if path.startswith("/api/serial/"): return o.serial_action(path.rsplit("/", 1)[-1], p) or {}
                if path == "/api/diagnostics/toggle": return o.diagnostics_toggle(bool(p.get("enabled")))
                if path == "/api/diagnostics/sensor-start": return o.sensor_start(str(p.get("kind") or ""))
                if path == "/api/diagnostics/classifier-continue": return o.sensor_continue_classifier()
                if path == "/api/diagnostics/sensor-cancel": return o.sensor_cancel()
                if path == "/api/diagnostics/mark": return o.diagnostics_mark(str(p.get("detail") or ""))
                if path == "/api/server/save": return o.server_save(p)
                if path == "/api/server/assign": return o.server_assign(p)
                if path == "/api/server/preload": return o.server_preload(str(p.get("alias") or ""))
                if path == "/api/server/remove": return o.server_remove(str(p.get("alias") or ""))
                if path == "/api/server/start": return o.server_start()
                if path == "/api/server/stop": return o.server_stop()
                if path == "/api/server/feedback": return o.server_upload_feedback(str(p.get("alias") or ""))
                if path == "/api/lan/save": return o.lan_save(p)
                if path == "/api/system/restart-desktop": o.owner(o.app.request_restart_in_desktop); return {}
                if path == "/api/system/exit": o.owner(o.app._on_close); return {}
                raise ValueError("Unknown action.")

            def _upload(self, path: str) -> None:
                session = self._require_post(technician=True, control=path.endswith("/connection-settings"))
                if session is None:
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    if length <= 0 or length > MAX_UPLOAD_BYTES:
                        raise ValueError("Upload size is invalid or exceeds 2 GiB.")
                    filename = Path(unquote(self.headers.get("X-Filename", "upload.bin"))).name
                    suffix = Path(filename).suffix.lower()
                    if path.endswith("/model") and suffix != ".zip":
                        raise ValueError("Model imports must be ZIP files.")
                    if path.endswith("/saved-bins") and not filename.lower().endswith(".bins.json"):
                        raise ValueError("Saved Bins imports must end in .bins.json.")
                    with tempfile.TemporaryDirectory(prefix="sp-upload-") as tmp:
                        target = Path(tmp) / filename
                        remaining = length
                        with target.open("wb") as out:
                            while remaining:
                                chunk = self.rfile.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError("Upload ended before the declared size.")
                                out.write(chunk); remaining -= len(chunk)
                            out.flush(); os.fsync(out.fileno())
                        if path.endswith("/model"):
                            parent.operations.ensure_stopped()
                            _cart, model_id = import_model(target, db=parent.app.db)
                            result = {"model_id": model_id}
                        elif path.endswith("/saved-bins"):
                            store = SavedBinsStore(paths.saved_bins_dir())
                            profile = store.read(target)
                            dest = store.write(profile, overwrite=False)
                            result = {
                                "filename": dest.name,
                                "caliber": profile.caliber,
                            }
                        elif path.endswith('/connection-settings'):
                            parent.operations.ensure_stopped()
                            data=json.loads(target.read_text(encoding='utf-8-sig'))
                            result=parent.operations.owner(lambda:parent.app.import_connection_settings(data))
                        else:
                            raise ValueError("Unknown upload target.")
                    self._json(HTTPStatus.OK, {"ok": True, **result})
                except FileExistsError as exc:
                    self._error(HTTPStatus.CONFLICT, str(exc))
                except ValueError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                except Exception as exc:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self._server = _BoundedThreadingHTTPServer(("0.0.0.0", self.settings.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ShibbyPrintsWebInterface", daemon=True,
        )
        self._thread.start()
        try:
            from .mdns_advertiser import MdnsAdvertiser

            advertiser = MdnsAdvertiser(
                self.settings.sorter_name,
                self.settings.port,
                primary_local_ipv4_addresses(),
            )
            advertiser.start()
            self._mdns = advertiser
        except Exception:
            # Direct IP access remains authoritative if optional discovery is
            # unavailable on a particular Windows network.
            self._mdns = None

    def stop(self) -> None:
        advertiser, self._mdns = self._mdns, None
        if advertiser is not None:
            advertiser.stop()
        server, self._server = self._server, None
        thread, self._thread = self._thread, None
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.operations.shutdown()
        self.state.shutdown()
        with self._session_lock:
            self._sessions.clear()
            self._control_sid = None
            self._control_last_seen = 0.0


_STYLE = """<style>:root{--b:#11151b;--p:#1c222b;--p2:#252d38;--t:#eef3f7;--m:#a6b0bc;--a:#3b82f6;--g:#22c55e;--w:#f59e0b;--r:#ef4444;--l:#394553}*{box-sizing:border-box}body{margin:0;background:var(--b);color:var(--t);font:15px system-ui,Segoe UI,sans-serif}header{display:flex;align-items:center;gap:14px;padding:12px 18px;background:#171d25;border-bottom:1px solid var(--l);position:sticky;top:0;z-index:4}header h1{font-size:20px;margin:0}header .spacer{flex:1}nav{display:flex;gap:4px;overflow:auto;padding:8px 12px;background:#141a21;position:sticky;top:54px;z-index:3}button{background:var(--p2);color:var(--t);border:1px solid var(--l);padding:9px 12px;border-radius:4px;font-weight:650;cursor:pointer}button.primary{background:#2563eb}button.danger{background:#8b2d2d}button:disabled{opacity:.45}.page{display:none;padding:14px}.page.active{display:block}.card{background:var(--p);border:1px solid var(--l);padding:14px;margin-bottom:12px;border-radius:5px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.two{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.slots{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px}.slot{background:var(--p2);padding:12px;border:1px solid var(--l)}input,select,textarea{width:100%;background:#0d1218;color:var(--t);border:1px solid var(--l);padding:8px;margin:3px 0 8px}input[type=checkbox]{width:auto}.actions{display:flex;gap:7px;flex-wrap:wrap}.muted{color:var(--m)}.good{color:var(--g)}.bad{color:var(--r)}.warn{color:var(--w)}img.preview{width:100%;max-height:430px;object-fit:contain;background:#070a0e}table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid var(--l);padding:7px}.bin-choice{display:flex;align-items:center;gap:8px;padding:6px 3px;border-bottom:1px solid var(--l);min-width:0}.bin-choice.assigned{background:#18304c}.bin-choice input{margin:0;flex-shrink:0}.bin-choice span{overflow-wrap:anywhere}.bin-choice small{margin-left:auto;color:var(--m);white-space:nowrap}.classification-rows{max-height:340px;overflow-y:auto}.route-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(135px,170px);gap:10px;align-items:center;padding:4px 0;border-bottom:1px solid var(--l)}.route-row span{overflow-wrap:anywhere}.route-row select{margin:0;padding:5px}.package-bins{display:flex;flex-wrap:wrap;gap:6px}.package-bins label{white-space:nowrap}.package-bins input{margin:0}.catch-summary{margin-top:12px;padding-top:8px;border-top:1px solid var(--l)}.catch-summary details{margin:8px 0}.catch-summary summary{cursor:pointer}.assignment-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;max-height:420px;overflow-y:auto;padding:3px}.assignment{display:flex;align-items:center;gap:7px;border:1px solid var(--l);padding:10px;border-radius:4px;min-width:0}.assignment.assigned{border-color:var(--a);background:#18304c}.assignment span{overflow-wrap:anywhere}.assignment small{margin-left:auto;color:var(--m);white-space:nowrap}.assignment input{margin:0;flex-shrink:0}.login{max-width:430px;margin:12vh auto;padding:14px}.login button{width:100%;margin-top:8px}.hide{display:none}pre{white-space:pre-wrap;max-height:330px;overflow:auto;background:#090d12;padding:10px}@media(max-width:850px){.two{grid-template-columns:1fr}nav{top:54px}}</style>"""


def _app_page(sorter_name: str, csrf: str) -> str:
    safe_csrf = json.dumps(csrf)
    safe_name = html.escape(sorter_name)
    maintenance_pages = {
        "run", "models", "training", "community", "remote", "image",
        "serial", "camera", "bins", "server", "lan", "diagnostics",
    }
    navigation = "".join(
        f'<button class="{"maintenance hide" if page in maintenance_pages else ""}" '
        f'data-page="{page}">{label}</button>'
        for page, label in [
            ("operator", "Operator"), ("run", "Run"), ("models", "Models"),
            ("training", "Train"), ("community", "Community"),
            ("remote", "AI Config"), ("image", "Image Processing"),
            ("serial", "Serial"), ("camera", "Camera"),
            ("bins", "Saved Bins"), ("server", "Server"),
            ("lan", "LAN Access"), ("diagnostics", "Diagnostics"),
            ("system", "About & System"),
        ]
    )
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>{safe_name} · ShibbyPrints</title>{_STYLE}</head><body><header><h1>{safe_name}</h1><span id=health class=muted>Connecting…</span><span class=spacer></span><button id=claim>CLAIM CONTROL</button><button id=unlock>TECHNICIAN</button></header><nav>{navigation}</nav><main>
<section id=operator class='page active'><div class=two><div class=card><h2>Live result</h2><img id=crop class=preview><h1 id=result>—</h1><p id=destination class=muted>—</p><p><strong>Master count: <span id=masterCount>0</span></strong></p><label><input id=operatorAuto type=checkbox> Automatically sort tray</label><div class=actions><button class=primary data-run=start>START</button><button data-run=stop>STOP</button><button data-run=feed>FEED ONE</button><button data-run=reset>RESET COUNTERS</button><button class=danger data-run=clear-slots>CLEAR SLOTS</button></div></div><div class=card><h2>Bin assignments</h2><label>Select a bin<select id=opBinPicker><option value="">Choose a bin…</option></select></label><p id=opBinSummary class=muted>Select a bin to show its classification options.</p><div id=opBinOptions class=hide><h3 id=opAssignmentTitle>Classifications</h3><label>Search classifications<input id=opFilter placeholder="Search classifications" autocomplete=off></label><p id=opAssignmentHint class=muted></p><div id=opAssignments class=classification-rows></div></div><div class=catch-summary><strong>Catch-All completed: <span id=opCatchCount>0</span></strong><details id=opCatchDetails><summary>Classification breakdown</summary><p class=muted>Predictions, not verified identities. Only acknowledged drops are counted. Export before resetting counters or closing the software.</p><div id=opCatchRows></div></details><a href='/api/catch-all-export'><button>EXPORT CATCH-ALL CSV</button></a></div></div></div></section>
<section id=run class=page><div class=actions><button class=primary data-run=start>START</button><button data-run=stop>STOP</button><button data-run=feed>FEED ONE</button><button data-run=force-feed>FORCE FEED</button><button data-run=reset>RESET COUNTERS</button><button class=danger data-run=clear-slots>CLEAR SLOTS</button><button id=openBins>LOAD SLOT CONFIG</button></div><div class=two><div class=card><h2>Bin assignments</h2><label>Select a bin<select id=runBinPicker><option value="">Choose a bin…</option></select></label><p id=runBinSummary class=muted>Select a bin to show its classification options.</p><div id=runBinOptions class=hide><h3 id=runAssignmentTitle>Classifications</h3><label>Search classifications<input id=runFilter placeholder="Search classifications" autocomplete=off></label><p id=runAssignmentHint class=muted></p><div id=runSlots class=classification-rows></div></div><div class=catch-summary><strong>Catch-All completed: <span id=runCatchCount>0</span></strong><details id=runCatchDetails><summary>Classification breakdown</summary><p class=muted>Predictions, not verified identities. Only acknowledged drops are counted. Export before resetting counters or closing the software.</p><div id=runCatchRows></div></details><a href='/api/catch-all-export'><button>EXPORT CATCH-ALL CSV</button></a></div></div><div class=card><h2>Run options</h2><label>Confidence floor</label><input id=confidence type=number min=0 max=100><label><input id=autoSelect type=checkbox> Auto-select trays</label><label><input id=useParents type=checkbox> Use parent classifications</label><label><input id=packageMode type=checkbox> Package mode</label><label>Package size</label><input id=packageSize type=number min=1><label>Store images</label><select id=storeImages><option value=none>None</option><option value=above>Above floor</option><option value=below>Below floor</option><option value=all>All</option></select><button id=saveRun>Save Run Options</button><h3>Recent classifications</h3><div id=runHistory></div></div></div></section>
<section id=models class=page><div class=card><h2>Software Profile</h2><select id=softwareProfile><option value=classification_only>Classification Only</option><option value=full>Full</option></select><button id=saveProfile>SAVE PROFILE</button><p class=muted>Remote models always hide training. Local models show training only in Full.</p></div><div class=card><h2>Installed Models</h2><div class=actions><button id=activateRemote>USE REMOTE MODEL</button><button id=refreshModels>REFRESH</button><input id=modelUpload type=file accept=.zip><button id=importModel>IMPORT ZIP</button></div><div id=modelList></div></div><div class=card><h2>Create Model</h2><div class=grid><input id=newModelName placeholder='Model name'><input id=newCaliber placeholder='Caliber'><select id=newModelMode><option>convnext_tiny</option><option>convnext_small</option><option>convnext_base</option><option>convnext_large</option></select></div><button id=createModel>CREATE</button></div></section>
<section id=training class=page><div class=two><div class=card><h2>Capture Training Image</h2><img id=trainCrop class=preview><label>Classification label</label><input id=trainLabel><label><input id=sortWhileTraining type=checkbox> Sort While Training using the selected label's current slot</label><div class=actions><button id=trainFeed>FEED & CAPTURE</button><button id=trainSave>SAVE IMAGE</button></div><div id=trainCounts></div></div><div class=card><h2>Train Active Model</h2><div class=grid><label>Epochs<input id=epochs type=number min=1></label><label>Batch size<input id=batchSize type=number min=1></label><label>Learning rate<input id=learningRate type=number step=.000001></label><label>Weight decay<input id=weightDecay type=number step=.000001></label><label>Dropout<input id=dropoutRate type=number step=.01></label><label>Validation split<input id=valSplit type=number step=.01></label><label>Image size<input id=imageSize type=number min=64></label><label>Max workers (-1 = auto)<input id=maxWorkers type=number min=-1></label><label>Stochastic depth (-1 = default)<input id=stochasticDepth type=number step=.01></label><label>Focal gamma<input id=focalGamma type=number step=.1></label><label>SWA start<input id=swaStart type=number step=.01></label><label>SWA mode<select id=swaMode><option>scheduled</option><option>adaptive</option></select></label><label>SWA accuracy threshold<input id=swaAcc type=number step=.001></label><label>SWA patience<input id=swaPatience type=number min=0></label><label>SWA minimum epoch<input id=swaMinEpoch type=number min=0></label></div><label><input id=trainAll type=checkbox> Train on full dataset</label><label><input id=freezeBackbone type=checkbox> Freeze backbone</label><label><input id=useFocal type=checkbox> Use focal loss</label><label><input id=useSwa type=checkbox> Use stochastic weight averaging</label><div class=actions><button class=primary id=startTraining>START TRAINING</button><button id=cancelTraining>CANCEL</button></div><pre id=trainLog></pre></div></div></section>
<section id=community class=page><div class=card><h2>Community Models</h2><div id=communityState></div><div class=actions><button id=communityLogin>SIGN IN</button><button id=communityLogout>SIGN OUT</button><button id=communitySearch>SEARCH MODELS</button></div><input id=communityQuery placeholder='Search'><div id=communityModels></div></div></section>
<section id=remote class=page><div class=two><div class=card><h2>Remote Model</h2><label>Endpoint URL</label><input id=endpoint><label>API key</label><input id=apiKey type=password placeholder='Leave blank to keep saved key'><label><input id=clearApiKey type=checkbox> Clear saved API key</label><label>Model</label><select id=remoteModel></select><label>Prompt</label><textarea id=prompt></textarea><div class=grid><label>JPEG quality<input id=imageQuality type=number min=10 max=100></label><label>Image scale %<input id=imageScale type=number min=10 max=200></label></div><div class=actions><button id=findRemoteModels>CONNECT AND FIND MODELS</button><button id=saveRemote>SAVE</button><button id=testRemote>TEST CONNECTION</button><button id=syncRemote>LOAD CLASSIFICATIONS</button><button id=remoteFeed>FEED ONE TEST</button></div><pre id=remoteResult></pre></div><div class=card><h2>Remote Classifications</h2><p class=muted>Provided by the selected AI server model. Use LOAD CLASSIFICATIONS to refresh; bin assignments do not change this list.</p><label>Search classifications<input id=remoteHeadstampFilter placeholder="Search classifications" autocomplete=off></label><div id=remoteHeadstamps class=grid></div></div></div></section>
<section id=image class=page><div class=two><div class=card><h2>Image Processing</h2><div class=grid><label>Accumulator scale (dp)<input id=houghDp type=number step=.1></label><label>Min center separation<input id=houghMinDist type=number></label><label>Edge strength<input id=houghP1 type=number></label><label>Detection threshold<input id=houghP2 type=number></label><label>Min case radius<input id=houghMinR type=number></label><label>Max case radius<input id=houghMaxR type=number></label></div><label>Primer mode</label><select id=primerMode><option>none</option><option>use</option><option>hide</option></select><label>Primer radius</label><input id=primerRadius type=number><label>Camera LED raw 0–255</label><input id=cameraLed type=number min=0 max=255><div class=actions><button id=saveImage>SAVE</button><button id=captureImage>CAPTURE IMAGE</button><button id=revertLighting>REVERT LIGHTING</button></div></div><div class=card><h2>Live and Processing Preview</h2><img id=imageLive class=preview><div class=two><img id=imageBefore class=preview><img id=imageAfter class=preview></div><p id=imageResult class=muted></p></div></div></section>
<section id=serial class=page><div class=two><div class=card><h2>Serial Configuration</h2><label>USB port</label><input id=serialPort list=serialPorts placeholder="COM7"><datalist id=serialPorts></datalist><label><input id=serialWifi type=checkbox> Kiosk Node (network sorter)</label><label id=serialIpLabel class=hide>Kiosk Node IP<input id=serialIp placeholder="192.168.4.92"></label><label>Saved sorter<input id=serialProfile list=sorterProfiles><datalist id=sorterProfiles></datalist></label><div class=actions><button id=saveSorter>SAVE SORTER</button><button id=loadSorter>LOAD SORTER</button></div><label>Baud</label><input id=serialBaud type=number><label>Probe timeout (s)</label><input id=serialTimeout type=number step=.5><label>Slot quantity</label><input id=serialSlots type=number><label><input id=serialInitStartup type=checkbox> Initialize these settings on startup</label><p>Firmware: <strong id=serialFirmware>Not connected</strong></p><div class=actions><button id=serialSave>SAVE</button><button id=serialConnect>CONNECT</button><button id=serialDisconnect>DISCONNECT</button><input id=connectionImport type=file accept=.json><button id=importConnection>IMPORT PREVIOUS SETUP</button><button id=serialGetConfig>GET CONFIG FROM BOARD</button><button id=serialPushConfig>PUSH TO BOARD</button></div><label>Sort to slot</label><div class=actions><input id=serialSortTo type=number min=0><button id=serialMove>SORT TO SLOT</button><button id=serialHome>HOME SORTER</button></div><details><summary>Board init and airdrop settings</summary><div id=serialInitFields class=grid></div></details><label>Direct command</label><div class=actions><input id=serialCommand><button id=serialSend>SEND</button></div></div><div class=card><h2>Serial Traffic</h2><pre id=serialLog></pre></div></div></section>
<section id=camera class=page><div class=two><div class=card><h2>Native DirectShow Camera</h2><label>Detected camera</label><select id=cameraDevice></select><label>Device index</label><input id=cameraIndex type=number min=0><label>Width</label><input id=cameraWidth type=number><label>Height</label><input id=cameraHeight type=number><label><input id=cameraPreferUsb type=checkbox> Prefer this camera by USB VID/PID</label><div class=actions><button id=cameraDetect>DETECT CAMERAS</button><button id=cameraApply>APPLY & RESTART</button><button id=cameraStart>START</button><button id=cameraStop>STOP</button></div><pre id=cameraInfo></pre></div><div class=card><h2>Preview</h2><img id=cameraLive class=preview></div></div></section>
<section id=bins class=page><div class=card><h2>Saved Bins</h2><label>Target caliber and model</label><select id=binTarget></select><label>Layout name</label><input id=binName><div class=actions><button id=refreshBins>REFRESH</button><button id=saveBins>SAVE CURRENT AS NEW</button><input id=binUpload type=file accept=.bins.json><button id=importBins>IMPORT BROWSER FILE</button></div><p id=binErrors class=warn></p><div id=binList></div></div><div id=binEditor class='card hide'><h2>Edit Layout</h2><p id=binEditorTitle></p><label>Filter classifications<input id=binEditFilter placeholder="Filter classifications"></label><div id=binEditorRows></div><div class=actions><button id=saveBinEdits>SAVE CHANGES</button><button id=cancelBinEdits>CANCEL</button></div></div><div class=card><h2>USB Transfer</h2><button id=refreshUsb>REFRESH USB DRIVES</button><label>USB drive</label><select id=usbDrive></select><label>USB layout to import</label><select id=usbLayout></select><label><input id=overwriteUsb type=checkbox> Replace matching local layout</label><div class=actions><button id=importUsb>IMPORT FROM USB</button><select id=localBinSelect></select><button id=exportUsb>EXPORT LOCAL COPY TO USB</button></div><p class=muted>Files are read from and written directly to the USB root. Export keeps the local copy.</p></div></section>
<section id=server class=page><div class=card><h2>Integrated API Server</h2><p id=serverStatus></p><p id=serverAddress class=muted></p><div class=actions><button class=primary id=startServer>START SERVER</button><button id=stopServer>STOP SERVER</button><button id=refreshServer>REFRESH</button></div></div><div class=card><h2>Network and Capacity</h2><label><input id=serverLan type=checkbox> Allow local-network clients</label><div class=grid><label>Port<input id=serverPort type=number></label><label>On-demand cache<input id=serverCache type=number min=0 max=32></label><label>Remote queue<input id=serverQueue type=number min=1 max=32></label></div><label>New API key (blank is allowed)</label><input id=serverKey type=password><label><input id=serverReplaceKey type=checkbox> Replace the saved API key with this value</label><button id=saveServer>SAVE SETTINGS</button></div><div class=card><h2>Models Available to Network Clients</h2><div class=grid><label>Installed model<select id=serverModel></select></label><label>API model name<input id=serverAlias></label><label><input id=serverPreload type=checkbox checked> Preload when server starts</label></div><button id=assignServerModel>ASSIGN MODEL</button><div id=serverAliases></div></div></section>
<section id=lan class=page><div class=card><h2>LAN Access</h2><div id=lanInfo></div><label>Sorter name</label><input id=lanName readonly><label>Web port</label><input id=lanPort readonly><label><input id=lanDesktop type=checkbox> Keep the Desktop Interface available on this PC</label><button id=saveLan>SAVE PRESENTATION SETTING</button><p class=muted>Change the sorter name or web port from the Desktop LAN Access tab so the active browser request is not interrupted during service restart.</p></div></section>
<section id=diagnostics class=page><div class=card><h2>Hidden Diagnostics</h2><p id=diagState></p><div class=actions><button id=enableDiag>ENABLE DIAGNOSTICS</button><button id=disableDiag>DISABLE AND RELEASE</button><a href='/api/diagnostics-export'><button>EXPORT REPORT</button></a><button id=markDiag>MARK EVENT</button></div><pre id=diagLive></pre><div class=actions><input id=diagLed type=number min=0 max=255 placeholder='Exact raw LED value'><button id=diagSendLed>SEND EXACT RAW</button><button id=diagRevertLed>REVERT TO SAVED</button></div></div><div class=card><h2>Classifier and Sorter Sensors</h2><p class=warn>These tests move the machine. Stop sorting, clear the mechanism, and keep hands and tools away.</p><div class=actions><button id=testClassifier>START CLASSIFIER TEST</button><button id=continueClassifier>CONTINUE WITH 3 LOADED CASES</button><button id=testSorter>START SORTER 0→5→0 TEST</button><button id=cancelSensor>CANCEL SAFELY</button></div><pre id=sensorLog></pre></div></section>
<section id=system class=page><div class=card><h2>About & System</h2><div id=systemInfo></div><h3>ShibbyPrints Kiosk Sorter Software</h3><p>The ShibbyPrints Kiosk Edition is based on software originally created by SJSeth Solutions and licensed under the GNU General Public License, version 3 or later (GPL-3.0-or-later). This software is provided without warranty. The complete license terms are included in the LICENSE file, and corresponding source code is provided with each official release.</p><p>ShibbyPrints independently maintains this Kiosk Edition as an optional companion to the software and hardware ecosystem created by SJSeth Solutions. This edition is not an official SJSeth Solutions product. No sponsorship or endorsement by SJSeth Solutions is claimed or implied. Support for this Kiosk Edition is provided by ShibbyPrints.</p><p>Original source code: <a href='https://github.com/sjseth/AI-Case-Sorter-Py'>github.com/sjseth/AI-Case-Sorter-Py</a></p><p>SJSeth provides electronics kits and components for compatible sorter systems: <a href='https://shop.sjseth.com/'>shop.sjseth.com</a></p><p>ShibbyPrints provides printed-parts kits and fully assembled CS7.2 sorter units: <a href='https://www.shibbyprints.com/'>shibbyprints.com</a></p><p>Looking for the community Discord? A link is available on the SJSeth software website: <a href='https://www.reloadingrecipes.com/HeadstampSorter'>reloadingrecipes.com/HeadstampSorter</a></p><details><summary><strong>Hidden Features</strong></summary><p class=muted>These desktop keyboard controls are intentionally kept out of normal Operator operation.</p><p><strong>Ctrl + D</strong> — Enable hidden diagnostics. Press it again to stop diagnostics and release its resources.</p><p><strong>Ctrl + O</strong> — Return directly to Operator Mode.</p><p><strong>Ctrl + M</strong> — Open Maintenance Mode directly.</p><p><strong>Ctrl + Q</strong> — Close the application safely.</p><p><strong>F11</strong> — Toggle desktop fullscreen mode.</p></details><div class=actions><button id=restartDesktop>SHOW DESKTOP INTERFACE</button><button class=danger id=exitApp>EXIT APPLICATION</button></div></div></section>
</main><div id=error class='card bad hide' style='position:fixed;right:12px;bottom:12px;max-width:650px'></div><script>
const CSRF={safe_csrf};let S=null,active='operator',timer=null,BINS=null,editingBin=null,savedLed=null;
const $=id=>document.getElementById(id);const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
async function api(path,opt={{}}){{opt.headers=Object.assign({{'Content-Type':'application/json'}},opt.headers||{{}});if(opt.method==='POST')opt.headers['X-Shibby-CSRF']=CSRF;let r=await fetch(path,opt),j=await r.json().catch(()=>({{}}));if(!r.ok)throw Error(j.error||r.statusText);return j}}
const post=(p,b={{}})=>api(p,{{method:'POST',body:JSON.stringify(b)}});function fail(e){{$('error').textContent=e.message||String(e);$('error').classList.remove('hide');setTimeout(()=>$('error').classList.add('hide'),8000)}}
async function upload(path,file){{if(!file)throw Error('Choose a file first.');let r=await fetch(path,{{method:'POST',headers:{{'X-Shibby-CSRF':CSRF,'X-Filename':file.name,'Content-Type':'application/octet-stream'}},body:file}}),j=await r.json().catch(()=>({{}}));if(!r.ok)throw Error(j.error||r.statusText);return j}}
function page(name){{active=name;document.querySelectorAll('.page').forEach(e=>e.classList.toggle('active',e.id===name));document.querySelectorAll('nav button').forEach(e=>e.classList.toggle('primary',e.dataset.page===name));if(timer)clearInterval(timer);timer=null;if(['camera','image'].includes(name)){{let tick=()=>{{$(name==='camera'?'cameraLive':'imageLive').src='/api/camera.jpg?r='+Date.now()}};tick();timer=setInterval(tick,1000)}}loadPage(name)}}document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>page(b.dataset.page));
function render(s){{S=s;let recovering=!!s.usb_recovering;$('health').innerHTML=`<span class="${{s.running?'good':recovering?'warn':s.error?'bad':'muted'}}">${{s.running?'RUNNING':recovering?'RECOVERING':s.error?'FAULT':'READY'}}</span> · ${{esc(recovering?'USB bridge recovering':s.serial_connected?'Serial connected':'Serial disconnected')}} · ${{esc(recovering?'Camera reconnecting':s.camera_ready?'Camera ready':'Camera unavailable')}}`;$('claim').textContent=s.control_owned?'RELEASE CONTROL':'CLAIM CONTROL';$('unlock').textContent=s.technician?'TECHNICIAN UNLOCKED':'TECHNICIAN';document.querySelectorAll('nav .maintenance').forEach(e=>e.classList.toggle('hide',!s.technician));if(!s.technician&&!['operator','system'].includes(active))page('operator');let lc=s.last_classification||{{}};$('result').textContent=lc.label||'—';$('destination').textContent=lc.label?`${{Number(lc.confidence||0).toFixed(2)}}% · ${{lc.slot===0?'Catch-All':lc.slot==null?'Test':'Slot '+lc.slot}}`:'—';$('masterCount').textContent=Number(s.master_count||0);$('operatorAuto').checked=!!s.auto_select;if(s.image_revision>0)$('crop').src='/api/crop.jpg?r='+s.image_revision;renderOperatorAssignments(s);renderCatchAll(s);$('runHistory').innerHTML=(s.history||[]).map(x=>`<p>${{esc(x.label)}} · ${{Number(x.confidence||0).toFixed(2)}}% · ${{x.slot===0?'Catch-All':'Slot '+x.slot}}</p>`).join('')||'<p class=muted>No classifications recorded in this browser session.</p>'}}
function assignmentPanel(s,container,prefix,filterId,titleId,hintId){{
 const slots=s.slots||[],picker=$(prefix+'BinPicker'),panel=$(prefix+'BinOptions'),box=$(container);
 const slotSignature=JSON.stringify(slots.map(x=>x.slot));
 if(picker.dataset.signature!==slotSignature){{let previous=picker.value;picker.dataset.signature=slotSignature;picker.innerHTML='<option value="">Choose a bin…</option>'+slots.map(x=>`<option value="${{x.slot}}">Slot ${{x.slot}}</option>`).join('');picker.value=slots.some(x=>String(x.slot)===previous)?previous:''}}
 picker.onchange=()=>{{if(S)assignmentPanel(S,container,prefix,filterId,titleId,hintId)}};
 const selected=slots.find(x=>String(x.slot)===picker.value);
 panel.classList.toggle('hide',!selected);
 if(!selected){{$(prefix+'BinSummary').textContent='Select a bin to show its classification options.';box.replaceChildren();delete box.dataset.signature;return}}
 const slot=selected.slot;
 $(prefix+'BinSummary').textContent=`Slot ${{slot}} · ${{selected.count||0}} cases`;
 const filter=$(filterId).value.trim().toLocaleLowerCase();
 const items=(s.routing_items||[]).slice().sort((a,b)=>a.name.localeCompare(b.name)).filter(x=>(x.name+' '+(x.parent_name||'')).toLocaleLowerCase().includes(filter));
 const signature=JSON.stringify([slot,filter,s.routing_mode,items]);
 $(titleId).textContent=`Slot ${{slot}} classifications`;
 $(hintId).textContent=s.routing_mode==='package'?'Tick classifications for this bin. Package mode allows several bins per classification.':'Tick classifications for this bin. To move a name from another bin, untick it in its current bin first. Everything unassigned uses Catch-All automatically.';
 if(box.dataset.signature===signature)return;
 box.dataset.signature=signature;
 box.innerHTML=items.map(x=>{{const checked=s.routing_mode==='package'?(x.slots||[]).includes(slot):Number(x.slot||0)===slot;
 const disabled=s.routing_mode!=='package'&&Number(x.slot||0)!==0&&Number(x.slot||0)!==slot;
 const location=s.routing_mode==='package'?(x.slots||[]).map(n=>'Slot '+n).join(', '):x.slot?'Slot '+x.slot:'Unassigned';
 const attrs=s.routing_mode==='package'?`data-${{prefix}}-package="${{esc(x.name)}}"`:`data-${{prefix}}-route="${{esc(x.name)}}" data-kind="${{esc(x.kind)}}" data-id="${{x.id||0}}"`;
 return `<label class="bin-choice ${{checked?'assigned':''}}"><input type=checkbox ${{attrs}} ${{checked?'checked':''}} ${{disabled?'disabled':''}}><span>${{esc(x.kind==='parent'?'Parent: '+x.name:x.name)}}</span><small>${{esc(location||'Unassigned')}}</small></label>`;
 }}).join('')||'<p class=muted>No matching classifications.</p>';
 box.querySelectorAll('input').forEach(e=>e.onchange=async()=>{{e.disabled=true;try{{let packageName=e.dataset[prefix+'Package'],name=e.dataset[prefix+'Route'];
 await post('/api/routing/assign',packageName!==undefined?{{kind:'package',name:packageName,slot,enabled:e.checked}}:{{kind:e.dataset.kind,id:Number(e.dataset.id),name,slot:e.checked?slot:0}});
 await refresh();
 }}catch(error){{e.checked=!e.checked;fail(error)}}finally{{const item=S?.routing_items?.find(x=>x.name===e.dataset[prefix+'Route']);e.disabled=S?.routing_mode!=='package'&&!!item&&Number(item.slot||0)!==0&&Number(item.slot||0)!==slot}}}});
}}
function renderOperatorAssignments(s){{assignmentPanel(s,'opAssignments','op','opFilter','opAssignmentTitle','opAssignmentHint')}}
function renderRouting(s){{assignmentPanel(s,'runSlots','run','runFilter','runAssignmentTitle','runAssignmentHint')}}
function renderCatchAll(s){{
 const report=s.catch_all_report||{{count:0,rows:[]}};
 ['op','run'].forEach(prefix=>{{$(prefix+'CatchCount').textContent=report.count||0;const details=$(prefix+'CatchDetails'),box=$(prefix+'CatchRows');details.ontoggle=()=>{{if(S)renderCatchAll(S)}};
 if(!details.open){{box.replaceChildren();delete box.dataset.signature;return}}
 const signature=JSON.stringify(report.rows||[]);if(box.dataset.signature===signature)return;box.dataset.signature=signature;
 box.innerHTML=(report.rows||[]).length?'<table><thead><tr><th>Predicted classification</th><th>Reason</th><th>Count</th></tr></thead><tbody>'+(report.rows||[]).map(x=>`<tr><td>${{esc(x.classification)}}</td><td>${{esc(x.reason==='below_confidence_floor'?'Below confidence floor':x.reason==='unassigned'?'No bin assigned':x.reason)}}</td><td>${{x.count}}</td></tr>`).join('')+'</tbody></table>':'<p class=muted>No acknowledged Catch-All drops in this counting period.</p>';
 }})
}}
async function refresh(){{try{{let state=await api('/api/state');render(state);renderRouting(state);document.querySelector('[data-page=training]').classList.toggle('hide',!state.technician||!state.training_visible);if(active==='training'&&!state.training_visible)page('operator')}}catch(e){{if(e.message.includes('browser session'))location.reload();else fail(e)}}}}
async function loadPage(p){{try{{if(p==='models')renderModels(await api('/api/models'));if(p==='training')renderTraining(await api('/api/training'));if(p==='community')renderCommunity(await api('/api/community'));if(p==='remote')renderRemote((await api('/api/models')).remote);if(p==='serial')renderSerial(await api('/api/serial'));if(p==='camera')renderCamera(await api('/api/camera'));if(p==='image')renderImage(await api('/api/image-processing'));if(p==='bins'){{let target=$('binTarget').value||'';renderBins(await api('/api/saved-bins?target='+encodeURIComponent(target)));renderUsb(await api('/api/usb'))}}if(p==='server')renderServer(await api('/api/server'));if(p==='lan')renderLan(await api('/api/system'));if(p==='diagnostics')renderDiag(await api('/api/diagnostics'));if(p==='system')renderSystem(await api('/api/system'));if(p==='run')renderRun(await api('/api/run-options'))}}catch(e){{fail(e)}}}}
function renderRun(x){{$('confidence').value=x.confidence_floor;$('autoSelect').checked=x.auto_select;$('useParents').checked=x.use_parents;$('useParents').disabled=!x.has_parents;$('packageMode').checked=x.package_mode;$('packageSize').value=x.package_size;$('storeImages').value=x.store_images}}
function renderModels(x){{$('softwareProfile').value=x.software_profile;document.querySelector('[data-page=training]').classList.toggle('hide',!x.training_visible);if(!x.training_visible&&active==='training')page('models');$('modelList').innerHTML=(x.models||[]).map(m=>`<div class=card><strong>${{esc(m.name)}}</strong> ${{m.active?'<span class=good>ACTIVE</span>':''}}<p class=muted>${{esc(m.caliber)}} · ${{esc(m.model_mode)}} · ${{m.headstamp_count}} classifications · ${{m.trained?'trained':'not trained'}}</p>${{m.community_uid?`<label><input type=checkbox data-fb=${{m.id}} ${{m.feedback_enabled?'checked':''}}> Participate in feedback below ${{m.feedback_floor}}%</label><select data-fbm=${{m.id}}><option ${{m.feedback_mode==='Instant'?'selected':''}}>Instant</option><option ${{m.feedback_mode==='OnRunComplete'?'selected':''}}>OnRunComplete</option><option ${{m.feedback_mode==='Manual'?'selected':''}}>Manual</option></select><p class=muted>Pending feedback: ${{m.feedback_pending}}</p>${{m.feedback_mode==='Manual'&&m.feedback_pending?`<button data-fbu=${{m.id}}>UPLOAD FEEDBACK</button>`:''}}`:''}}<div class=actions><button data-activate=${{m.id}}>ACTIVATE</button><a href="/api/model-export/${{m.id}}"><button>EXPORT</button></a><button class=danger data-delete=${{m.id}}>DELETE</button></div></div>`).join('');document.querySelectorAll('[data-activate]').forEach(b=>b.onclick=()=>post('/api/models/activate',{{model_id:Number(b.dataset.activate)}}).then(()=>{{loadPage('models');refresh()}}).catch(fail));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>confirm('Delete this model and its stored data?')&&post('/api/models/delete',{{model_id:Number(b.dataset.delete)}}).then(()=>loadPage('models')).catch(fail));document.querySelectorAll('[data-fb]').forEach(c=>c.onchange=()=>post('/api/models/update',{{model_id:Number(c.dataset.fb),feedback_enabled:c.checked,feedback_mode:document.querySelector(`[data-fbm="${{c.dataset.fb}}"]`).value}}).catch(fail));document.querySelectorAll('[data-fbu]').forEach(b=>b.onclick=()=>post('/api/models/feedback-upload',{{model_id:Number(b.dataset.fbu)}}).then(()=>setTimeout(()=>loadPage('models'),500)).catch(fail))}}
function renderTraining(x){{let c=x.config||{{}};$('sortWhileTraining').checked=!!x.sort_while_training;$('epochs').value=c.epochs??10;$('batchSize').value=c.batch_size??16;$('learningRate').value=c.learning_rate??.001;$('weightDecay').value=c.weight_decay??.0001;$('dropoutRate').value=c.dropout_rate??0;$('valSplit').value=c.val_split??.2;$('imageSize').value=c.image_size??480;$('maxWorkers').value=c.max_workers??-1;$('stochasticDepth').value=c.stochastic_depth_prob??-1;$('focalGamma').value=c.focal_gamma??2;$('swaStart').value=c.swa_start??.75;$('swaMode').value=c.swa_mode||'scheduled';$('swaAcc').value=c.swa_acc_threshold??0;$('swaPatience').value=c.swa_patience??0;$('swaMinEpoch').value=c.swa_min_epoch??0;$('trainAll').checked=!!c.train_all;$('freezeBackbone').checked=!!c.freeze_backbone;$('useFocal').checked=!!c.use_focal_loss;$('useSwa').checked=!!c.use_swa;$('trainLog').textContent=(x.log||[]).join('\\n');$('trainCounts').innerHTML=Object.entries(x.counts||{{}}).map(([k,v])=>`<button data-label="${{esc(k)}}">${{esc(k)}} · ${{v}}</button>`).join('');document.querySelectorAll('[data-label]').forEach(b=>b.onclick=()=>{{$('trainLabel').value=b.dataset.label}});if(x.last_crop_ready)$('trainCrop').src='/api/training-crop.jpg?r='+Date.now()}}
function renderCommunity(x){{$('communityState').textContent=x.signed_in?`Signed in: ${{x.name||x.email}}`:'Not signed in.'}}
function renderRemote(x){{$('endpoint').value=x.endpoint_url||'';let names=[x.model,...(x.available_models||[])].filter(Boolean);$('remoteModel').innerHTML=[...new Set(names)].map(n=>`<option ${{n===x.model?'selected':''}}>${{esc(n)}}</option>`).join('');$('prompt').value=x.prompt||'';$('imageQuality').value=x.image_quality||100;$('imageScale').value=x.image_scale||100;REMOTE_LABELS=(x.headstamps||[]).slice();renderRemoteLabels()}}
let REMOTE_LABELS=[];
function renderRemoteLabels(){{const filter=$('remoteHeadstampFilter').value.trim().toLocaleLowerCase();$('remoteHeadstamps').innerHTML=REMOTE_LABELS.filter(n=>n.toLocaleLowerCase().includes(filter)).map(n=>`<div class=slot>${{esc(n)}}</div>`).join('')||'<p class=muted>No matching classifications.</p>'}}
$('remoteHeadstampFilter').oninput=renderRemoteLabels;
const SERIAL_KEYS=['feedhomingoffset','sorthomingoffset','feedspeed','sortspeed','feedsteps','slotdropdelay','notificationdelay','automotorstandbytimeout','feedmotorcurrent','sortmotorcurrent','fan','debounceTimeout','debounceTime','cameraledlevel','sortsteps','airdropenabled','airdroppredelay','airdropdsignalduration','airdroppostdelay'];
function connectionMode(){{let remote=$('serialWifi').checked;$('serialPort').disabled=remote;$('serialIpLabel').classList.toggle('hide',!remote);$('serialBaud').disabled=remote}}
function renderSerial(x){{let c=x.config||{{}},remote=c.wifi_enabled??/^https?:/.test(c.port||'');
 $('serialPorts').innerHTML=(x.ports||[]).filter(p=>!/^https?:/.test(p)).map(p=>`<option value="${{esc(p)}}"></option>`).join('');
 $('serialPort').value=c.usb_port||(remote?'':c.port||'');$('serialWifi').checked=remote;$('serialIp').value=c.wifi_address||(remote?c.port||'':'');connectionMode();
 $('serialProfile').value=x.active_sorter||'';$('sorterProfiles').innerHTML=(x.sorter_profiles||[]).map(n=>`<option value="${{esc(n)}}"></option>`).join('');
 $('serialBaud').value=c.baud;$('serialTimeout').value=c.handshake_timeout_s||4;$('serialSlots').value=c.slot_quantity;$('serialInitStartup').checked=!!c.init_on_startup;$('serialFirmware').textContent=x.firmware||'Not connected';
 let init=c.init_settings||{{}},enabled=!!Number(init.airdropenabled||0);
 $('serialInitFields').innerHTML=SERIAL_KEYS.map(k=>k==='airdropenabled'?`<label><input type=checkbox id=airdropEnabled data-init="${{k}}" ${{enabled?'checked':''}}> Airdrop enabled</label>`:`<label>${{esc(k)}}<input type=number data-init="${{esc(k)}}" value="${{Number(init[k]||0)}}" ${{k.startsWith('airdrop')&&!enabled?'disabled':''}}></label>`).join('');
 $('airdropEnabled').onchange=()=>document.querySelectorAll('[data-init]').forEach(e=>{{if(e.dataset.init.startsWith('airdrop')&&e.dataset.init!=='airdropenabled')e.disabled=!$('airdropEnabled').checked}});
 $('serialLog').textContent=(x.log||[]).map(r=>`${{r.time}} ${{r.direction}} ${{r.line}}`).join('\\n');
}}
function renderCamera(x){{let c=x.config;$('cameraIndex').value=c.device_index;$('cameraWidth').value=c.width;$('cameraHeight').value=c.height;$('cameraPreferUsb').checked=c.prefer_by_usb_id!==false;$('cameraInfo').textContent=JSON.stringify(x.diagnostic,null,2)}}function renderImage(x){{let p=x.image_proc||{{}},h=p.hough||{{}};$('primerMode').value=p.primer_mode;$('primerRadius').value=p.primer_radius;$('cameraLed').value=x.camera_led;savedLed=x.camera_led;$('houghDp').value=h.dp;$('houghMinDist').value=h.min_dist;$('houghP1').value=h.param1;$('houghP2').value=h.param2;$('houghMinR').value=h.min_radius;$('houghMaxR').value=h.max_radius}}
function renderBins(x){{BINS=x;let old=$('binTarget').value;$('binTarget').innerHTML=(x.targets||[]).map(t=>`<option value="${{esc(t.key)}}" ${{t.key===x.selected_target?'selected':''}}>${{esc(t.label)}}</option>`).join('');if(old&&(x.targets||[]).some(t=>t.key===old))$('binTarget').value=old;let profiles=x.profiles||[];$('binErrors').textContent=(x.errors||[]).join('\\n');$('binList').innerHTML=profiles.map(p=>{{let a=p.analysis||{{}},summary=p.analysis_error?`Cannot apply: ${{esc(p.analysis_error)}}`:`Will apply ${{a.matched_count||0}} · New ${{a.new_count||0}} · Unavailable ${{a.unavailable_count||0}} · Unsupported bins ${{a.unsupported_count||0}}`;return `<div class=card><strong>${{esc(p.layout_name)}}</strong> · ${{esc(p.caliber)}}<p class=muted>${{summary}}</p><div class=actions><button data-bin="${{esc(p.filename)}}">APPLY SELECTED LAYOUT</button><button data-bin-edit="${{esc(p.filename)}}">EDIT LAYOUT</button><a href="/api/saved-bins-export/${{encodeURIComponent(p.filename)}}"><button>DOWNLOAD COPY</button></a><button class=danger data-bin-delete="${{esc(p.filename)}}">DELETE</button></div></div>`}}).join('');$('localBinSelect').innerHTML=profiles.map(p=>`<option value="${{esc(p.filename)}}">${{esc(p.layout_name)}} (${{esc(p.filename)}})</option>`).join('');document.querySelectorAll('[data-bin]').forEach(b=>b.onclick=()=>confirm('Replace current routing and counters with this layout?')&&post('/api/saved-bins/apply',{{filename:b.dataset.bin,target:$('binTarget').value}}).then(()=>{{refresh();loadPage('bins')}}).catch(fail));document.querySelectorAll('[data-bin-edit]').forEach(b=>b.onclick=()=>openBinEditor(b.dataset.bin));document.querySelectorAll('[data-bin-delete]').forEach(b=>b.onclick=()=>confirm('Delete this saved layout file?')&&post('/api/saved-bins/delete',{{filename:b.dataset.bin}}).then(()=>loadPage('bins')).catch(fail))}}
function openBinEditor(filename){{let p=(BINS.profiles||[]).find(x=>x.filename===filename);if(!p)return;editingBin=filename;$('binEditorTitle').textContent=p.layout_name;$('binEditFilter').value='';
let max=Math.max(1,Number(BINS.slot_quantity||8)-1);
$('binEditorRows').innerHTML=(p.editor_assignments||[]).map((a,i)=>`<div class=card data-edit-row="${{esc(a.name||'')}}"><label>Classification<input data-edit-name="${{i}}" data-edit-available="${{a.available?'1':'0'}}" value="${{esc(a.name||'')}}" readonly></label><input type=hidden data-edit-kind="${{i}}" value="${{esc(a.kind||'headstamp')}}"><p class=muted>${{a.available?'Select bins:':'Preserved: unavailable in the selected model.'}}</p><div class=assignment-grid>${{Array.from({{length:max}},(_,j)=>j+1).map(n=>`<label><input type=checkbox data-edit-bin="${{i}}" data-bin="${{n}}" ${{(a.bins||[]).includes(n)?'checked':''}} ${{a.available?'':'disabled'}}> Slot ${{n}}</label>`).join('')}}</div></div>`).join('');$('binEditor').classList.remove('hide')}}
function renderServer(x){{let s=x.settings||{{}},st=x.status||{{}};$('serverStatus').textContent=st.message||'Server stopped.';$('serverAddress').textContent=x.primary_url||'';$('serverLan').checked=s.host==='0.0.0.0';$('serverPort').value=s.port;$('serverCache').value=s.on_demand_cache_slots;$('serverQueue').value=s.remote_queue_limit;$('serverModel').innerHTML=(x.trained_models||[]).map(m=>`<option value="${{m.id}}" data-caliber="${{esc(m.caliber)}}">${{esc(m.caliber+' — '+m.name)}}</option>`).join('');$('serverAliases').innerHTML=(x.aliases||[]).map(a=>`<div class=card><strong>${{esc(a.alias)}}</strong> → ${{esc(a.model)}} · ${{a.preload?'Preload':'On demand'}} · ${{a.loaded?'Loaded':a.error?'Error':'Stopped/available'}}<div class=actions><button data-server-preload="${{esc(a.alias)}}">TOGGLE PRELOAD</button><button data-server-feedback="${{esc(a.alias)}}">UPLOAD FEEDBACK</button><button class=danger data-server-remove="${{esc(a.alias)}}">REMOVE</button></div></div>`).join('');document.querySelectorAll('[data-server-preload]').forEach(b=>b.onclick=()=>post('/api/server/preload',{{alias:b.dataset.serverPreload}}).then(renderServer).catch(fail));document.querySelectorAll('[data-server-remove]').forEach(b=>b.onclick=()=>confirm('Remove this API model assignment?')&&post('/api/server/remove',{{alias:b.dataset.serverRemove}}).then(renderServer).catch(fail));document.querySelectorAll('[data-server-feedback]').forEach(b=>b.onclick=()=>post('/api/server/feedback',{{alias:b.dataset.serverFeedback}}).then(()=>alert('Feedback upload queued.')).catch(fail))}}
function renderLan(x){{$('lanName').value=x.sorter_name;$('lanPort').value=x.port;$('lanDesktop').checked=!!x.desktop_enabled;$('lanInfo').innerHTML=`<p>Friendly address: ${{esc(x.friendly_url)}}</p><p>Primary LAN address: ${{esc((x.ip_urls||[]).join(' · ')||'Unavailable')}}</p>`}}
function renderUsb(x){{let drives=x.drives||[];$('usbDrive').innerHTML=drives.map(d=>`<option value="${{esc(d.root)}}">${{esc(d.root)}}</option>`).join('');window.USB=drives;updateUsbLayouts()}}function updateUsbLayouts(){{let d=(window.USB||[]).find(x=>x.root===$('usbDrive').value);$('usbLayout').innerHTML=((d&&d.layouts)||[]).map(p=>`<option value="${{esc(p.filename)}}">${{esc(p.layout_name)}} (${{esc(p.filename)}})</option>`).join('')}}
function renderDiag(x){{$('diagState').textContent=x.message;$('diagLive').textContent=x.enabled?JSON.stringify({{camera:x.camera,live:x.live}},null,2):'';savedLed=x.saved_camera_led;$('diagLed').value=savedLed;let s=x.sensor||{{}};$('sensorLog').textContent=[s.status,...(s.progress||[])].filter(Boolean).join('\\n');$('continueClassifier').disabled=s.phase!=='awaiting_cases';$('cancelSensor').disabled=!s.busy;$('testClassifier').disabled=!x.enabled||s.busy;$('testSorter').disabled=!x.enabled||s.busy}}function renderSystem(x){{$('systemInfo').innerHTML=`<p><strong>${{esc(x.product)}}</strong> · ${{esc(x.public_version)}}</p><p>Friendly address: ${{esc(x.friendly_url)}} · ${{x.mdns_active?'<span class=good>advertised</span>':'<span class=warn>use direct IP if unavailable</span>'}}</p><p>Primary LAN address: ${{esc((x.ip_urls||[]).join(' · ')||'Unavailable')}}</p><p>${{esc(x.os)}}</p>`}}
document.querySelectorAll('[data-run]').forEach(b=>b.onclick=()=>{{let a=b.dataset.run;if(a==='clear-slots'&&!confirm('Clear every slot assignment and reset every counter?'))return;post('/api/run/'+a).then(refresh).catch(fail)}});$('claim').onclick=()=>post(S&&S.control_owned?'/api/control/release':'/api/control/claim').then(refresh).catch(fail);$('unlock').onclick=()=>confirm('Open technician controls on this private-network browser for 30 minutes?')&&post('/api/technician/unlock').then(refresh).catch(fail);$('operatorAuto').onchange=()=>post('/api/run/auto-select',{{enabled:$('operatorAuto').checked}}).then(refresh).catch(fail);$('openBins').onclick=()=>page('bins');
$('saveRun').onclick=()=>post('/api/run-options',{{confidence_floor:Number($('confidence').value),auto_select:$('autoSelect').checked,use_parents:$('useParents').checked,package_mode:$('packageMode').checked,package_size:Number($('packageSize').value),store_images:$('storeImages').value}}).then(refresh).catch(fail);$('saveProfile').onclick=()=>post('/api/models/profile',{{profile:$('softwareProfile').value}}).then(()=>loadPage('models')).catch(fail);$('activateRemote').onclick=()=>post('/api/models/activate',{{model_id:null}}).then(()=>{{loadPage('models');refresh()}}).catch(fail);$('refreshModels').onclick=()=>loadPage('models');$('createModel').onclick=()=>post('/api/models/create',{{name:$('newModelName').value,caliber:$('newCaliber').value,model_mode:$('newModelMode').value}}).then(()=>loadPage('models')).catch(fail);$('importModel').onclick=()=>upload('/api/upload/model',$('modelUpload').files[0]).then(()=>loadPage('models')).catch(fail);
$('sortWhileTraining').onchange=()=>post('/api/training/sort-while',{{enabled:$('sortWhileTraining').checked}}).catch(fail);$('trainFeed').onclick=()=>post('/api/training/feed',{{label:$('trainLabel').value}}).then(()=>loadPage('training')).catch(fail);$('trainSave').onclick=()=>post('/api/training/save',{{label:$('trainLabel').value}}).then(()=>loadPage('training')).catch(fail);$('startTraining').onclick=()=>post('/api/training/start',{{epochs:Number($('epochs').value),batch_size:Number($('batchSize').value),learning_rate:Number($('learningRate').value),weight_decay:Number($('weightDecay').value),dropout_rate:Number($('dropoutRate').value),val_split:Number($('valSplit').value),image_size:Number($('imageSize').value),max_workers:Number($('maxWorkers').value),stochastic_depth_prob:Number($('stochasticDepth').value),focal_gamma:Number($('focalGamma').value),swa_start:Number($('swaStart').value),swa_mode:$('swaMode').value,swa_acc_threshold:Number($('swaAcc').value),swa_patience:Number($('swaPatience').value),swa_min_epoch:Number($('swaMinEpoch').value),train_all:$('trainAll').checked,freeze_backbone:$('freezeBackbone').checked,use_focal_loss:$('useFocal').checked,use_swa:$('useSwa').checked}}).then(()=>loadPage('training')).catch(fail);$('cancelTraining').onclick=()=>post('/api/training/cancel').catch(fail);
$('communityLogin').onclick=()=>post('/api/community/login').then(()=>loadPage('community')).catch(fail);$('communityLogout').onclick=()=>post('/api/community/logout').then(()=>loadPage('community')).catch(fail);$('communitySearch').onclick=()=>post('/api/community/catalog',{{search:$('communityQuery').value}}).then(x=>{{$('communityModels').innerHTML=(x.models||[]).map(m=>`<div class=card><strong>${{esc(m.model_name||m.model_uid)}}</strong><p>${{esc(m.cartridge_name||'')}} · v${{m.model_version||1}}</p><button data-cuid="${{esc(m.model_uid)}}" data-cver="${{m.model_version||1}}">DOWNLOAD / UPDATE</button></div>`).join('');document.querySelectorAll('[data-cuid]').forEach(b=>b.onclick=()=>post('/api/community/download',{{uid:b.dataset.cuid,version:Number(b.dataset.cver)}}).then(()=>loadPage('models')).catch(fail))}}).catch(fail);
function remotePayload(){{return {{endpoint_url:$('endpoint').value,api_key:$('apiKey').value,clear_api_key:$('clearApiKey').checked,model:$('remoteModel').value,prompt:$('prompt').value,image_quality:Number($('imageQuality').value),image_scale:Number($('imageScale').value)}}}}$('saveRemote').onclick=()=>post('/api/remote/save',remotePayload()).then(renderRemote).catch(fail);$('findRemoteModels').onclick=()=>post('/api/remote/save',remotePayload()).then(()=>post('/api/remote/models')).then(x=>{{let current=$('remoteModel').value;$('remoteModel').innerHTML=(x.models||[]).map(n=>`<option ${{n===current?'selected':''}}>${{esc(n)}}</option>`).join('');$('remoteResult').textContent=`Found ${{(x.models||[]).length}} model(s).`;}}).catch(fail);$('testRemote').onclick=()=>post('/api/remote/test').then(x=>$('remoteResult').textContent=JSON.stringify(x,null,2)).catch(fail);$('syncRemote').onclick=()=>post('/api/remote/sync').then(x=>{{$('remoteResult').textContent=JSON.stringify(x,null,2);loadPage('remote')}}).catch(fail);$('remoteFeed').onclick=()=>post('/api/remote/feed').then(x=>$('remoteResult').textContent='Test cycle started.').catch(fail);
function serialPayload(){{let init={{}};document.querySelectorAll('[data-init]').forEach(e=>init[e.dataset.init]=e.type==='checkbox'?Number(e.checked):Number(e.value));return {{wifi_enabled:$('serialWifi').checked,wifi_address:$('serialIp').value,usb_port:$('serialPort').value,baud:Number($('serialBaud').value),handshake_timeout_s:Number($('serialTimeout').value),slot_quantity:Number($('serialSlots').value),init_on_startup:$('serialInitStartup').checked,init_settings:init}}}}$('importConnection').onclick=()=>confirm('Import connection, crop and remote bins? Disconnect first. Local models and saved layouts are retained.')&&upload('/api/upload/connection-settings',$('connectionImport').files[0]).then(()=>{{loadPage('serial');refresh()}}).catch(fail);$('serialSave').onclick=()=>post('/api/serial/save',serialPayload()).then(()=>loadPage('serial')).catch(fail);$('serialConnect').onclick=()=>post('/api/serial/connect',{{wifi_enabled:$('serialWifi').checked,wifi_address:$('serialIp').value,usb_port:$('serialPort').value,init_on_startup:$('serialInitStartup').checked}}).then(()=>loadPage('serial')).catch(fail);$('serialDisconnect').onclick=()=>post('/api/serial/disconnect').then(()=>loadPage('serial')).catch(fail);$('serialSend').onclick=()=>post('/api/serial/command',{{command:$('serialCommand').value}}).then(()=>loadPage('serial')).catch(fail);$('serialMove').onclick=()=>post('/api/serial/sort-to',{{slot:Number($('serialSortTo').value)}}).catch(fail);$('serialHome').onclick=()=>post('/api/serial/sort-to',{{slot:0}}).catch(fail);$('serialGetConfig').onclick=()=>post('/api/serial/get-config').then(x=>{{Object.entries(x.config||{{}}).forEach(([k,v])=>{{let e=[...document.querySelectorAll('[data-init]')].find(n=>n.dataset.init===k);if(e){{if(e.type==='checkbox'){{e.checked=!!Number(v);e.dispatchEvent(new Event('change'))}}else e.value=v}}}})}}).catch(fail);$('serialPushConfig').onclick=async()=>{{try{{await post('/api/serial/save',serialPayload());await post('/api/serial/push-config');await loadPage('serial')}}catch(e){{fail(e)}}}};
$('cameraDetect').onclick=()=>post('/api/camera/detect').then(x=>{{$('cameraDevice').innerHTML=(x.cameras||[]).map((c,i)=>`<option value="${{i}}">${{esc('('+c.index+') '+(c.name||'Camera')+(c.vid&&c.pid?' ['+c.vid+':'+c.pid+']':''))}}</option>`).join('');window.CAMERAS=x.cameras||[];$('cameraDevice').onchange=()=>{{let c=window.CAMERAS[Number($('cameraDevice').value)];if(!c)return;$('cameraIndex').value=c.index;let r=(c.resolutions||[]).slice(-1)[0];if(r){{$('cameraWidth').value=r[0];$('cameraHeight').value=r[1]}}}};$('cameraDevice').onchange()}}).catch(fail);$('cameraApply').onclick=()=>{{let c=(window.CAMERAS||[])[Number($('cameraDevice').value)]||{{}};post('/api/camera/apply',{{device_index:Number($('cameraIndex').value),width:Number($('cameraWidth').value),height:Number($('cameraHeight').value),prefer_by_usb_id:$('cameraPreferUsb').checked,preferred_vid:c.vid||'',preferred_pid:c.pid||''}}).then(()=>loadPage('camera')).catch(fail)}};$('cameraStart').onclick=()=>post('/api/camera/start').catch(fail);$('cameraStop').onclick=()=>post('/api/camera/stop').catch(fail);
function imagePayload(raw=savedLed){{return {{primer_mode:$('primerMode').value,primer_radius:Number($('primerRadius').value),camera_led:Number(raw),hough:{{dp:Number($('houghDp').value),min_dist:Number($('houghMinDist').value),param1:Number($('houghP1').value),param2:Number($('houghP2').value),min_radius:Number($('houghMinR').value),max_radius:Number($('houghMaxR').value)}}}}}}$('saveImage').onclick=()=>post('/api/image-processing/save',imagePayload($('cameraLed').value)).then(()=>loadPage('image')).catch(fail);$('captureImage').onclick=()=>post('/api/image-processing/save',imagePayload($('cameraLed').value)).then(()=>post('/api/image-processing/capture')).then(x=>{{$('imageBefore').src='data:image/jpeg;base64,'+x.before;$('imageAfter').src='data:image/jpeg;base64,'+x.after;$('imageResult').textContent=x.detection?'Circle detected: '+x.detection.join(', '):'No circle detected.'}}).catch(fail);$('revertLighting').onclick=()=>{{$('cameraLed').value=savedLed;post('/api/image-processing/save',imagePayload(savedLed)).catch(fail)}};
$('binTarget').onchange=()=>loadPage('bins');$('refreshBins').onclick=()=>loadPage('bins');$('saveBins').onclick=()=>post('/api/saved-bins/save',{{name:$('binName').value,target:$('binTarget').value}}).then(()=>loadPage('bins')).catch(fail);$('importBins').onclick=()=>upload('/api/upload/saved-bins',$('binUpload').files[0]).then(()=>loadPage('bins')).catch(fail);$('saveBinEdits').onclick=()=>{{let rows=[...document.querySelectorAll('[data-edit-name]')].filter(e=>e.dataset.editAvailable==='1').map(e=>{{let i=e.dataset.editName,bins=[...document.querySelectorAll(`[data-edit-bin="${{i}}"]`)].filter(e=>e.checked).map(e=>Number(e.dataset.bin));return {{name:e.value,kind:document.querySelector(`[data-edit-kind="${{i}}"]`).value,bins}}}}).filter(x=>x.bins.length);post('/api/saved-bins/update',{{filename:editingBin,target:$('binTarget').value,assignments:rows}}).then(()=>{{$('binEditor').classList.add('hide');loadPage('bins')}}).catch(fail)}};$('cancelBinEdits').onclick=()=>$('binEditor').classList.add('hide');$('refreshUsb').onclick=()=>api('/api/usb').then(renderUsb).catch(fail);$('usbDrive').onchange=updateUsbLayouts;$('importUsb').onclick=()=>post('/api/usb/import-bins',{{root:$('usbDrive').value,filename:$('usbLayout').value,overwrite:$('overwriteUsb').checked}}).then(()=>loadPage('bins')).catch(fail);$('exportUsb').onclick=()=>post('/api/usb/export-bins',{{root:$('usbDrive').value,filename:$('localBinSelect').value}}).then(x=>alert('Copied to '+x.root+' as '+x.filename)).catch(fail);
$('saveServer').onclick=()=>post('/api/server/save',{{local_network:$('serverLan').checked,port:Number($('serverPort').value),on_demand_cache_slots:Number($('serverCache').value),remote_queue_limit:Number($('serverQueue').value),replace_api_key:$('serverReplaceKey').checked,api_key:$('serverKey').value}}).then(renderServer).catch(fail);$('startServer').onclick=()=>post('/api/server/start').then(renderServer).catch(fail);$('stopServer').onclick=()=>post('/api/server/stop').then(renderServer).catch(fail);$('refreshServer').onclick=()=>loadPage('server');$('serverModel').onchange=()=>{{let o=$('serverModel').selectedOptions[0];if(o)$('serverAlias').value=o.dataset.caliber||''}};$('assignServerModel').onclick=()=>post('/api/server/assign',{{model_id:Number($('serverModel').value),alias:$('serverAlias').value,preload:$('serverPreload').checked}}).then(renderServer).catch(fail);$('saveLan').onclick=()=>post('/api/lan/save',{{sorter_name:$('lanName').value,port:Number($('lanPort').value),desktop_enabled:$('lanDesktop').checked}}).then(renderLan).catch(fail);
$('enableDiag').onclick=()=>post('/api/diagnostics/toggle',{{enabled:true}}).then(renderDiag).catch(fail);$('disableDiag').onclick=()=>post('/api/diagnostics/toggle',{{enabled:false}}).then(renderDiag).catch(fail);$('markDiag').onclick=()=>post('/api/diagnostics/mark',{{detail:prompt('Diagnostic marker','Web technician mark')||''}}).then(renderDiag).catch(fail);$('diagSendLed').onclick=()=>post('/api/image-processing/save',{{camera_led:Number($('diagLed').value)}}).then(()=>loadPage('diagnostics')).catch(fail);$('diagRevertLed').onclick=()=>post('/api/image-processing/save',{{camera_led:savedLed}}).then(()=>loadPage('diagnostics')).catch(fail);$('testClassifier').onclick=()=>confirm('Remove all cases, clear the mechanism, and start the classifier proximity check?')&&post('/api/diagnostics/sensor-start',{{kind:'classifier'}}).then(renderDiag).catch(fail);$('continueClassifier').onclick=()=>confirm('At least three cases are loaded and the mechanism is clear?')&&post('/api/diagnostics/classifier-continue').then(renderDiag).catch(fail);$('testSorter').onclick=()=>confirm('The mechanism is clear for a sorter 0 → 5 → 0 movement test?')&&post('/api/diagnostics/sensor-start',{{kind:'sorter'}}).then(renderDiag).catch(fail);$('cancelSensor').onclick=()=>post('/api/diagnostics/sensor-cancel').then(renderDiag).catch(fail);$('restartDesktop').onclick=()=>post('/api/system/restart-desktop').then(()=>alert('Desktop Interface is now available on the sorter PC.')).catch(fail);$('exitApp').onclick=()=>confirm('Exit the sorter application?')&&post('/api/system/exit').catch(fail);
$('binEditFilter').oninput=()=>{{let needle=$('binEditFilter').value.trim().toLocaleLowerCase();document.querySelectorAll('[data-edit-row]').forEach(e=>e.classList.toggle('hide',!e.dataset.editRow.toLocaleLowerCase().includes(needle)))}};
$('opFilter').oninput=()=>{{if(S)renderOperatorAssignments(S)}};$('runFilter').oninput=()=>{{if(S)renderRouting(S)}};
$('serialWifi').onchange=()=>{{if($('serialWifi').checked){{let ip=prompt('Enter the sorter IP address:',$('serialIp').value||'');if(ip===null||!ip.trim())$('serialWifi').checked=false;else {{$('serialIp').value=ip.trim();$('serialInitStartup').checked=false}}}}connectionMode()}};
$('saveSorter').onclick=()=>post('/api/serial/save-sorter',{{...serialPayload(),name:$('serialProfile').value}}).then(()=>loadPage('serial')).catch(fail);
$('loadSorter').onclick=()=>post('/api/serial/load-sorter',{{name:$('serialProfile').value}}).then(()=>{{loadPage('serial');refresh()}}).catch(fail);
window.addEventListener('pagehide',()=>{{if(S&&S.control_owned)fetch('/api/control/release',{{method:'POST',headers:{{'Content-Type':'application/json','X-Shibby-CSRF':CSRF}},body:'{{}}',keepalive:true}})}});let ev=new EventSource('/events');ev.onmessage=()=>{{refresh();if(active==='training'||active==='diagnostics'||active==='server')loadPage(active)}};refresh();
</script></body></html>"""
