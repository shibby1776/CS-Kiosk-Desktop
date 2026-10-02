"""Bounded, event-driven crash capture and support-report export.

This is deliberately smaller than the hidden diagnostic collector. It creates
no timer, polling loop, worker thread, image buffer, or serial history. Only
meaningful lifecycle events and errors reach its two bounded log files.
"""
from __future__ import annotations

import faulthandler
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import paths


LOG_NAME = "session.log"
PREVIOUS_LOG_NAME = "session.log.1"
PENDING_NAME = "pending_error.json"
ACTIVE_SESSION_NAME = "active_session.json"
FATAL_CURRENT_NAME = "fatal_current.log"
FATAL_PREVIOUS_NAME = "fatal_previous.log"
DEFAULT_MAX_LOG_BYTES = 512 * 1024
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\s,;]+"),
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    partial = Path(str(path) + ".partial")
    try:
        with partial.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def _safe_member_name(name: str) -> str:
    candidate = PurePosixPath(str(name).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"Unsafe report member name: {name!r}")
    return candidate.as_posix()


class CrashReporter:
    """Capture bounded failure context without a monitoring subsystem."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        app_info: dict[str, Any] | None = None,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    ) -> None:
        self.directory = Path(directory)
        self.app_info = dict(app_info or {})
        self.max_log_bytes = max(4096, int(max_log_bytes))
        self.session_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self.pending_from_previous_session = False
        self._secrets: list[str] = []
        self._notify: Callable[[], None] | None = None
        self._handler: RotatingFileHandler | None = None
        self._fatal_stream: Any | None = None
        self._enabled_faulthandler = False
        self._started = False
        self._closed = False
        self._original_sys_hook: Any = None
        self._original_thread_hook: Any = None
        self._installed_sys_hook: Any = None
        self._installed_thread_hook: Any = None
        self._logger = logging.getLogger(f"shibbyprints.crash.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

    @property
    def has_pending_report(self) -> bool:
        return any(
            (self.directory / name).is_file()
            for name in (PENDING_NAME, FATAL_PREVIOUS_NAME)
        )

    def start(self) -> None:
        if self._started:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        active_path = self.directory / ACTIVE_SESSION_NAME
        pending_path = self.directory / PENDING_NAME
        fatal_current = self.directory / FATAL_CURRENT_NAME
        fatal_previous = self.directory / FATAL_PREVIOUS_NAME

        previous_unclean = active_path.is_file()
        previous_fatal = fatal_current.is_file() and fatal_current.stat().st_size > 0
        if previous_fatal:
            os.replace(fatal_current, fatal_previous)
        self.pending_from_previous_session = (
            pending_path.is_file()
            or previous_unclean
            or previous_fatal
            or fatal_previous.is_file()
        )
        if (previous_unclean or previous_fatal) and not pending_path.is_file():
            _atomic_json(
                pending_path,
                {
                    "recorded_at": _utc_now(),
                    "source": "startup",
                    "summary": (
                        "A fatal fault was captured during the previous session."
                        if previous_fatal
                        else "The previous application session did not shut down cleanly."
                    ),
                    "traceback": (
                        "See error/fatal_crash.log."
                        if previous_fatal
                        else "No Python traceback was available."
                    ),
                },
            )

        self._handler = RotatingFileHandler(
            self.directory / LOG_NAME,
            maxBytes=self.max_log_bytes,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        formatter = logging.Formatter(
            "%(asctime)sZ | %(levelname)s | %(message)s", "%Y-%m-%dT%H:%M:%S"
        )
        formatter.converter = time.gmtime
        self._handler.setFormatter(formatter)
        self._logger.addHandler(self._handler)

        _atomic_json(
            active_path,
            {
                "session_id": self.session_id,
                "started_at": _utc_now(),
                "application": self.app_info,
            },
        )

        self._fatal_stream = fatal_current.open("ab", buffering=0)
        if not faulthandler.is_enabled():
            faulthandler.enable(file=self._fatal_stream, all_threads=True)
            self._enabled_faulthandler = True

        self._install_exception_hooks()
        self._started = True
        self.record_event("application_start", f"session={self.session_id}")

    def set_notification_callback(self, callback: Callable[[], None] | None) -> None:
        """Set a thread-safe callback that merely schedules the UI prompt."""
        self._notify = callback

    def register_secret(self, value: Any) -> None:
        secret = str(value or "").strip()
        if len(secret) >= 4 and secret.casefold() not in {"none", "nokey", "null"}:
            if secret not in self._secrets:
                self._secrets.append(secret)

    def _redact(self, value: Any) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(r"\1[REDACTED]", text)
        try:
            home = str(Path.home())
            if home:
                text = text.replace(home, "[USER_HOME]")
        except Exception:
            pass
        return text

    def record_event(self, event: str, detail: Any = "", *, level: int = logging.INFO) -> None:
        if not self._started or self._closed:
            return
        clean_event = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(event))[:80]
        clean_detail = self._redact(detail).replace("\r", " ").replace("\n", " ")
        self._logger.log(level, "%s | %s", clean_event, clean_detail[:4000])
        self._flush_log()

    def record_exception(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
        *,
        source: str,
        notify: bool = True,
    ) -> None:
        if not self._started or self._closed:
            return
        formatted = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        redacted_traceback = self._redact(formatted)
        summary = self._redact(f"{exc_type.__name__}: {exc_value}")
        self.record_event(
            "unexpected_exception",
            f"source={source}; {summary}; traceback={redacted_traceback}",
            level=logging.ERROR,
        )
        try:
            _atomic_json(
                self.directory / PENDING_NAME,
                {
                    "recorded_at": _utc_now(),
                    "session_id": self.session_id,
                    "source": str(source),
                    "summary": summary,
                    "traceback": redacted_traceback,
                },
            )
        except Exception:
            pass
        if notify and self._notify is not None:
            try:
                self._notify()
            except Exception:
                pass

    def record_current_exception(self, *, source: str, notify: bool = True) -> None:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        if exc_type is not None and exc_value is not None:
            self.record_exception(
                exc_type, exc_value, exc_traceback, source=source, notify=notify
            )

    def _install_exception_hooks(self) -> None:
        self._original_sys_hook = sys.excepthook

        def sys_hook(exc_type: type[BaseException], exc_value: BaseException, tb: Any) -> None:
            self.record_exception(
                exc_type, exc_value, tb, source="main_thread", notify=False
            )
            self._original_sys_hook(exc_type, exc_value, tb)

        self._installed_sys_hook = sys_hook
        sys.excepthook = sys_hook

        self._original_thread_hook = getattr(threading, "excepthook", None)
        if self._original_thread_hook is not None:
            def thread_hook(args: Any) -> None:
                if args.exc_type is not SystemExit:
                    self.record_exception(
                        args.exc_type,
                        args.exc_value,
                        args.exc_traceback,
                        source=f"thread:{getattr(args.thread, 'name', 'unknown')}",
                        notify=True,
                    )
                self._original_thread_hook(args)

            self._installed_thread_hook = thread_hook
            threading.excepthook = thread_hook

    def _flush_log(self) -> None:
        handler = self._handler
        if handler is not None:
            try:
                handler.flush()
            except Exception:
                pass

    def export_members(self) -> dict[str, bytes]:
        """Return sanitized, bounded crash material for either ZIP exporter."""
        self._flush_log()
        if self._fatal_stream is not None:
            try:
                self._fatal_stream.flush()
            except Exception:
                pass
        members: dict[str, bytes] = {}
        candidates = (
            (PENDING_NAME, "error/error_summary.json"),
            (LOG_NAME, "error/session.log"),
            (PREVIOUS_LOG_NAME, "error/session.previous.log"),
            (FATAL_PREVIOUS_NAME, "error/fatal_crash.log"),
            (FATAL_CURRENT_NAME, "error/fatal_current.log"),
        )
        for filename, member in candidates:
            path = self.directory / filename
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", errors="backslashreplace")
            members[_safe_member_name(member)] = self._redact(text).encode("utf-8")
        training_logs = sorted(paths.logs_dir().glob("training-*.log"), reverse=True)
        if training_logs:
            try:
                raw = training_logs[0].read_bytes()[-2 * 1024 * 1024:]
            except OSError:
                raw = b""
            if raw:
                text = raw.decode("utf-8", errors="backslashreplace")
                members["error/training.latest.log"] = self._redact(text).encode("utf-8")
        members["error/runtime.json"] = json.dumps(
            {
                "generated_at": _utc_now(),
                "session_id": self.session_id,
                "application": self.app_info,
                "platform": platform.platform(),
                "python": sys.version,
                "loaded_versions": {
                    name: getattr(sys.modules.get(name), "__version__", None)
                    for name in ("torch", "torchvision", "cv2", "numpy")
                },
            },
            indent=2,
            default=str,
        ).encode("utf-8")
        return members

    def export_zip(
        self,
        destination: str | os.PathLike[str],
        *,
        camera_info: dict[str, Any] | None = None,
        config_summary: dict[str, Any] | None = None,
    ) -> Path:
        """Atomically write a standalone crash/support ZIP."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(str(destination) + ".partial")
        partial.unlink(missing_ok=True)
        try:
            # Windows requires a writable descriptor for fsync. Keep the same
            # writable handle alive until ZipFile has finalized its directory,
            # then sync it before publishing the completed report.
            with partial.open("w+b") as stream:
                with zipfile.ZipFile(
                    stream, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for name, data in self.export_members().items():
                        archive.writestr(_safe_member_name(name), data)
                    archive.writestr(
                        "error/camera.json",
                        json.dumps(camera_info or {}, indent=2, default=str),
                    )
                    archive.writestr(
                        "error/configuration.json",
                        json.dumps(config_summary or {}, indent=2, default=str),
                    )
                    self._write_build_report(archive)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
        self.clear_pending()
        return destination

    def _write_build_report(self, archive: zipfile.ZipFile) -> None:
        roots = [
            Path(getattr(sys, "_MEIPASS", "")) / "build_report"
            if getattr(sys, "_MEIPASS", None)
            else None,
            Path(__file__).resolve().parents[1] / "build_report",
        ]
        for root in roots:
            if root is None or not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, f"build/{path.relative_to(root).as_posix()}")
            break

    def clear_pending(self) -> None:
        for filename in (PENDING_NAME, FATAL_PREVIOUS_NAME):
            try:
                (self.directory / filename).unlink(missing_ok=True)
            except OSError:
                pass
        self.pending_from_previous_session = False

    def close(self, *, clean: bool) -> None:
        if self._closed:
            return
        if clean:
            self.record_event("application_shutdown", f"session={self.session_id}")
            try:
                (self.directory / ACTIVE_SESSION_NAME).unlink(missing_ok=True)
            except OSError:
                pass
        self._notify = None
        if (
            self._installed_sys_hook is not None
            and sys.excepthook is self._installed_sys_hook
            and self._original_sys_hook is not None
        ):
            sys.excepthook = self._original_sys_hook
        if (
            self._original_thread_hook is not None
            and self._installed_thread_hook is not None
            and getattr(threading, "excepthook", None) is self._installed_thread_hook
        ):
            threading.excepthook = self._original_thread_hook
        if self._enabled_faulthandler:
            try:
                faulthandler.disable()
            except Exception:
                pass
        if self._fatal_stream is not None:
            try:
                self._fatal_stream.close()
            except Exception:
                pass
            self._fatal_stream = None
        fatal_current = self.directory / FATAL_CURRENT_NAME
        try:
            if fatal_current.is_file() and fatal_current.stat().st_size == 0:
                fatal_current.unlink()
        except OSError:
            pass
        if self._handler is not None:
            try:
                self._handler.flush()
                self._handler.close()
                self._logger.removeHandler(self._handler)
            except Exception:
                pass
            self._handler = None
        self._closed = True
