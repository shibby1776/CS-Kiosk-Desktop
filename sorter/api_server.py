"""Optional in-process, OpenAI-compatible multi-model inference server.

The service is fully dormant until ``start`` is called.  It reuses the local
inference coordinator, gives attached-sorter work priority, and maps stable API
names to installed model ids rather than copied checkpoint files.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import numpy as np

from . import classifier, local_inference, paths
from .api_server_settings import ApiServerSettings
from .repository import ApiModelAliasRepo, ModelRepo, SettingsRepo


MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_FEEDBACK_FILES = 100
MAX_FEEDBACK_BYTES = 250 * 1024 * 1024


@dataclass
class ApiServerStatus:
    state: str = "stopped"
    message: str = "Server stopped."
    address: str = ""
    loaded_aliases: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, max_workers: int = 6, **kwargs: Any) -> None:
        self.runtime: ApiServerRuntime
        self._worker_limit = threading.BoundedSemaphore(max(1, int(max_workers)))
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._worker_limit.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 429 Too Many Requests\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Retry-After: 1\r\nConnection: close\r\n\r\n"
                    b'{"error":"Server is busy."}'
                )
            except OSError:
                pass
            finally:
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

    def get_request(self) -> tuple[Any, Any]:
        request, address = super().get_request()
        request.settimeout(30.0)
        return request, address

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_limit.release()


class _Handler(BaseHTTPRequestHandler):
    server_version = "ShibbyPrintsKioskAPI/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def runtime(self) -> "ApiServerRuntime":
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _json(self, status: int, payload: Any, *, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        auth = str(self.headers.get("Authorization", ""))
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            token = str(self.headers.get("X-API-Key", "")).strip()
        return self.runtime.settings.verify_api_key(token)

    def _request_allowed(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        if address.is_loopback or address.is_link_local:
            return True
        networks = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ) if address.version == 4 else (ipaddress.ip_network("fc00::/7"),)
        return any(address in network for network in networks)

    def _guard(self) -> bool:
        if not self._request_allowed():
            self._json(403, {"error": "Only private-network clients are allowed."})
            return False
        if not self._authorized():
            self._json(401, {"error": "Invalid API key."})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if not self._guard():
            return
        try:
            if parsed.path == "/v1/models":
                now = int(time.time())
                self._json(200, {
                    "object": "list",
                    "data": [
                        {"id": alias, "object": "model", "created": now}
                        for alias in self.runtime.alias_names()
                    ],
                })
                return
            if parsed.path == "/getheadstamps":
                alias = str(parse_qs(parsed.query).get("model", [""])[0]).strip()
                if not alias:
                    self._json(400, {"error": "Model is required."})
                    return
                self._json(200, self.runtime.classes(alias))
                return
            self._json(404, {"error": "Not found."})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except local_inference.InferenceBusyError as exc:
            self._json(429, {"error": str(exc)}, extra={"Retry-After": "1"})
        except Exception as exc:
            self.runtime.record_error("request", exc)
            self._json(503, {"error": "The requested model is unavailable."})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        if urlparse(self.path).path != "/v1/chat/completions":
            self._json(404, {"error": "Not found."})
            return
        try:
            raw_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            raw_length = 0
        if raw_length <= 0 or raw_length > MAX_REQUEST_BYTES:
            self._json(413, {"error": "Request body is too large or missing."})
            return
        try:
            payload = json.loads(self.rfile.read(raw_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            alias = str(payload.get("model", "") or "").strip()
            image = self.runtime.decode_request_image(payload)
            label, confidence = self.runtime.classify(alias, image)
            self._json(200, {
                "id": f"kiosk-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": alias,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": label},
                    "finish_reason": "stop",
                }],
                "confidence": float(confidence) / 100.0,
            })
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except local_inference.InferenceBusyError as exc:
            self._json(429, {"error": str(exc)}, extra={"Retry-After": "1"})
        except Exception as exc:
            self.runtime.record_error("inference", exc)
            self._json(503, {"error": "Inference failed on the server."})


class ApiServerRuntime:
    """Lifecycle owner for the optional integrated API server."""

    def __init__(
        self,
        db: Any,
        *,
        bus: Any | None = None,
        auth_provider: Callable[[], Any] | None = None,
        crash_reporter: Any | None = None,
    ) -> None:
        self.db = db
        self.bus = bus
        self.auth_provider = auth_provider or (lambda: None)
        self.crash_reporter = crash_reporter
        self.aliases = ApiModelAliasRepo(db)
        self.models = ModelRepo(db)
        self.settings = ApiServerSettings()
        self.status = ApiServerStatus()
        self._server: _BoundedThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._preloaded: set[str] = set()
        self._on_demand: OrderedDict[str, None] = OrderedDict()
        self._feedback: Any | None = None
        self._feedback_lock = threading.Lock()
        self._feedback_inflight: set[int] = set()

    @property
    def is_running(self) -> bool:
        return self.status.state in {"starting", "running", "stopping"}

    def _publish(self) -> None:
        if self.bus is not None:
            self.bus.post("api_server/status", self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.status.state,
                "message": self.status.message,
                "address": self.status.address,
                "loaded_aliases": sorted(self.status.loaded_aliases, key=str.casefold),
                "errors": dict(self.status.errors),
            }

    def record_error(self, source: str, exc: BaseException) -> None:
        with self._lock:
            self.status.errors[source] = f"{type(exc).__name__}: {exc}"
        if self.crash_reporter is not None:
            try:
                self.crash_reporter.record_exception(
                    type(exc), exc, exc.__traceback__,
                    source=f"api_server:{source}", notify=True,
                )
            except Exception:
                pass
        self._publish()

    def alias_names(self) -> list[str]:
        return [item.alias for item in self.aliases.list()]

    def _resolve(self, alias: str) -> tuple[Any, Any]:
        assignment = self.aliases.get(alias)
        if assignment is None:
            raise KeyError(f"Unknown model: {alias}")
        model = self.models.get(assignment.model_id)
        if model is None or not model.model_path or not Path(model.model_path).is_file():
            raise ValueError(f"{assignment.alias} does not have an available checkpoint.")
        problem = classifier.torch_floor_problem(model)
        if problem:
            raise ValueError(problem)
        return assignment, model

    def _active_local_path(self) -> str | None:
        model_id = SettingsRepo(self.db).get_active_model_id()
        model = self.models.get(model_id) if model_id is not None else None
        return str(Path(model.model_path).resolve()) if model and model.model_path else None

    def start(self, settings: ApiServerSettings) -> None:
        with self._lock:
            if self.is_running:
                return
            settings.validate()
            assignments = self.aliases.list()
            if not assignments:
                raise ValueError("Assign at least one local model to the API server.")
            self.settings = settings
            self.status = ApiServerStatus(state="starting", message="Preparing server…")
            self._preloaded = {item.alias.casefold() for item in assignments if item.preload}
            self._on_demand.clear()
            self._publish()

        server: _BoundedThreadingHTTPServer | None = None
        loaded_during_start: list[str] = []
        try:
            server = _BoundedThreadingHTTPServer(
                (settings.host, int(settings.port)),
                _Handler,
                max_workers=max(2, min(8, int(settings.remote_queue_limit) + 2)),
            )
            server.runtime = self
            for assignment in assignments:
                if not assignment.preload:
                    continue
                _item, model = self._resolve(assignment.alias)
                image_size = int(model.training_config.image_size) if model.training_config else None
                local_inference.warm_model(
                    model.model_path,
                    image_size=image_size,
                    priority="remote",
                    remote_queue_limit=settings.remote_queue_limit,
                )
                loaded_during_start.append(assignment.alias)
                with self._lock:
                    self.status.loaded_aliases.add(assignment.alias)
                    self.status.message = f"Loaded {assignment.alias}."
                self._publish()
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="integrated-api-server",
                daemon=True,
            )
            with self._lock:
                self._server = server
                self._thread = thread
                shown_host = "localhost" if settings.host == "127.0.0.1" else "0.0.0.0"
                self.status.state = "running"
                self.status.address = f"http://{shown_host}:{settings.port}"
                self.status.message = "Server ready."
            thread.start()
            self._publish()
        except Exception:
            if server is not None:
                server.server_close()
            active = self._active_local_path()
            for alias in loaded_during_start:
                try:
                    _item, model = self._resolve(alias)
                    if str(Path(model.model_path).resolve()) != active:
                        local_inference.evict_model(model.model_path)
                except Exception:
                    pass
            with self._lock:
                self._server = None
                self._thread = None
                self.status.state = "stopped"
                self.status.message = "Server failed to start."
            self._publish()
            raise

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            aliases = self.aliases.list()
            self.status.state = "stopping" if server is not None else "stopped"
            self.status.message = "Stopping server…" if server is not None else "Server stopped."
        self._publish()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        active = self._active_local_path()
        for assignment in aliases:
            model = self.models.get(assignment.model_id)
            if model and model.model_path:
                resolved = str(Path(model.model_path).resolve())
                if resolved != active:
                    local_inference.evict_model(model.model_path)
        with self._lock:
            self._preloaded.clear()
            self._on_demand.clear()
            self.status = ApiServerStatus()
        self._publish()

    def _touch_on_demand(self, alias: str, model_path: str) -> None:
        key = alias.casefold()
        if key in self._preloaded:
            return
        limit = int(self.settings.on_demand_cache_slots)
        if limit <= 0:
            return
        with self._lock:
            self._on_demand.pop(key, None)
            while len(self._on_demand) >= limit:
                old_key, _ = self._on_demand.popitem(last=False)
                old = next(
                    (item for item in self.aliases.list() if item.alias.casefold() == old_key),
                    None,
                )
                if old is None:
                    continue
                old_model = self.models.get(old.model_id)
                active = self._active_local_path()
                if old_model and old_model.model_path:
                    old_path = str(Path(old_model.model_path).resolve())
                    assignments = self.aliases.list()
                    retained_model_ids = {
                        item.model_id
                        for item in assignments
                        if item.preload or item.alias.casefold() in self._on_demand
                    }
                    if old_path != active and old.model_id not in retained_model_ids:
                        local_inference.evict_model(old_model.model_path)
                        self.status.loaded_aliases.discard(old.alias)
            self._on_demand[key] = None

    def classes(self, alias: str) -> list[str]:
        assignment, model = self._resolve(alias)
        self._touch_on_demand(assignment.alias, model.model_path)
        classes = local_inference.classes_for_model(
            model.model_path,
            priority="remote",
            remote_queue_limit=self.settings.remote_queue_limit,
        )
        with self._lock:
            self.status.loaded_aliases.add(assignment.alias)
        if not assignment.preload and self.settings.on_demand_cache_slots == 0:
            if str(Path(model.model_path).resolve()) != self._active_local_path():
                local_inference.evict_model(model.model_path)
        self._publish()
        return classes

    def classify(self, alias: str, image_bgr: np.ndarray) -> tuple[str, float]:
        assignment, model = self._resolve(alias)
        self._touch_on_demand(assignment.alias, model.model_path)
        image_size = int(model.training_config.image_size) if model.training_config else None
        label, confidence = local_inference.classify(
            image_bgr,
            model.model_path,
            image_size=image_size,
            priority="remote",
            remote_queue_limit=self.settings.remote_queue_limit,
        )
        with self._lock:
            self.status.loaded_aliases.add(assignment.alias)
        self._capture_feedback(model, image_bgr, label, confidence)
        if not assignment.preload and self.settings.on_demand_cache_slots == 0:
            if str(Path(model.model_path).resolve()) != self._active_local_path():
                local_inference.evict_model(model.model_path)
        self._publish()
        return label, confidence

    def decode_request_image(self, payload: dict[str, Any]) -> np.ndarray:
        import cv2

        data_url = ""
        for message in payload.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            for item in content if isinstance(content, list) else []:
                if not isinstance(item, dict) or item.get("type") != "image_url":
                    continue
                image_url = item.get("image_url") or {}
                data_url = str(image_url.get("url", "") if isinstance(image_url, dict) else image_url)
                if data_url:
                    break
            if data_url:
                break
        if not data_url.startswith("data:image/") or ";base64," not in data_url:
            raise ValueError("A base64 image data URL is required.")
        encoded = data_url.split(";base64,", 1)[1]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Image base64 data is invalid.") from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Decoded image is empty or too large.")
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3:
            raise ValueError("Image data could not be decoded.")
        if int(image.shape[0]) * int(image.shape[1]) > MAX_IMAGE_PIXELS:
            raise ValueError("Decoded image dimensions are too large.")
        return image

    def _capture_feedback(self, model: Any, image: np.ndarray, label: str, confidence: float) -> None:
        try:
            if not model.feedback_loop_enabled or not model.community_model_uid:
                return
            feedback = self._feedback_service()
            if not feedback.should_capture(model, confidence):
                return
            if not feedback.capture(model, image, label, confidence):
                return
            self._bound_feedback_queue(int(model.id))
            if self.bus is not None:
                self.bus.post("api_server/feedback_queued", {
                    "model_id": int(model.id),
                    "upload_mode": model.feedback_loop_upload_mode,
                })
            if model.feedback_loop_upload_mode == "Instant":
                self.upload_feedback(int(model.id))
        except Exception as exc:
            self.record_error("feedback", exc)

    def _bound_feedback_queue(self, model_id: int) -> None:
        files = self._feedback_service().pending_files(model_id)
        total = sum(path.stat().st_size for path in files if path.exists())
        while files and (len(files) > MAX_FEEDBACK_FILES or total > MAX_FEEDBACK_BYTES):
            oldest = files.pop(0)
            try:
                size = oldest.stat().st_size
                oldest.unlink()
                total -= size
            except OSError:
                pass

    def upload_feedback(self, model_id: int) -> bool:
        auth = self.auth_provider()
        if auth is None:
            return False
        try:
            if not auth.is_authenticated():
                return False
        except Exception:
            return False
        with self._feedback_lock:
            if model_id in self._feedback_inflight:
                return False
            self._feedback_inflight.add(model_id)

        def _run() -> None:
            try:
                self._feedback_service().upload_pending(model_id, auth=auth)
            except Exception as exc:
                self.record_error("feedback_upload", exc)
            finally:
                with self._feedback_lock:
                    self._feedback_inflight.discard(model_id)
                self._publish()

        threading.Thread(
            target=_run,
            name=f"api-feedback-{model_id}",
            daemon=True,
        ).start()
        return True

    def _feedback_service(self):
        if self._feedback is None:
            from .feedback import FeedbackService
            self._feedback = FeedbackService(self.db)
        return self._feedback
