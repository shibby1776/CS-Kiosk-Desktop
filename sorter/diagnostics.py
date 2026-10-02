"""Temporary camera/serial diagnostics for the desktop verification build."""
from __future__ import annotations

import csv
from contextlib import contextmanager
import io
import json
import os
import platform
import statistics
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import cv2
import numpy as np


@contextmanager
def _atomic_zip(destination: Path):
    """Yield a ZIP writer and publish it only after a complete close/fsync."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(destination) + ".partial")
    partial.unlink(missing_ok=True)
    try:
        # Keep one writable handle through ZIP close and fsync. Windows rejects
        # fsync on a descriptor reopened read-only (EBADF), even though that
        # pattern is accepted on POSIX systems.
        with partial.open("w+b") as stream:
            with zipfile.ZipFile(
                stream, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                yield archive
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _safe_extra_member(name: str) -> str:
    candidate = PurePosixPath(str(name).replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise ValueError(f"Unsafe diagnostic member name: {name!r}")
    return candidate.as_posix()


class DiagnosticCollector:
    def __init__(self, max_frames: int = 12000, max_events: int = 5000) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._frames: deque[dict[str, Any]] = deque(maxlen=max_frames)
        self._serial: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._brightest: tuple[float, np.ndarray] | None = None
        self._darkest: tuple[float, np.ndarray] | None = None
        self._reference: np.ndarray | None = None
        self._last_frame_ts: float | None = None
        self._frame_number = 0
        self._last_led_level: int | None = None
        self._last_firmware_response = ""
        self._requested_brightness_percent: float | None = None
        self._requested_led_raw: int | None = None
        self._last_led_tx_mono: float | None = None
        self._last_serial_latency_ms: float | None = None
        self._last_frame_sample_mono: float | None = None
        self._sample_interval_s = 0.25
        self._capture_sessions: deque[dict[str, Any]] = deque(maxlen=50)
        self._capture_images: deque[dict[str, Any]] = deque(maxlen=12)
        self._capture_session_number = 0
        self._pipeline_sessions: deque[dict[str, Any]] = deque(maxlen=30)
        self._pipeline_session_number = 0
        self._sensor_tests: deque[dict[str, Any]] = deque(maxlen=20)

    def record_frame(self, frame: np.ndarray) -> None:
        """Sample preview diagnostics without burdening the Tk UI thread.

        Full 1080p colour conversion and image copies were previously performed
        on every preview refresh.  Diagnostics now samples at 4 Hz and computes
        luminance from a small view while retaining occasional full-size sample
        frames for export.
        """
        mono = time.monotonic()
        with self._lock:
            if self._last_frame_sample_mono is not None and mono - self._last_frame_sample_mono < self._sample_interval_s:
                return
            previous = self._last_frame_ts
            self._last_frame_sample_mono = mono
            self._last_frame_ts = mono
            self._frame_number += 1
            frame_number = self._frame_number

        now = time.time()
        # Subsample before conversion: roughly 240x135 for a 1080p source.
        small = frame[::8, ::8]
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        luminance = float(np.mean(gray))
        interval_ms = None if previous is None else (mono - previous) * 1000.0

        with self._lock:
            self._frames.append({
                "timestamp": datetime.fromtimestamp(now).isoformat(timespec="milliseconds"),
                "frame": frame_number,
                "luminance": round(luminance, 4),
                "interval_ms": "" if interval_ms is None else round(interval_ms, 4),
                "width": int(frame.shape[1]),
                "height": int(frame.shape[0]),
            })
            # Copy full frames only when an exported representative changes.
            if self._reference is None:
                self._reference = frame.copy()
            if self._brightest is None or luminance > self._brightest[0]:
                self._brightest = (luminance, frame.copy())
            if self._darkest is None or luminance < self._darkest[0]:
                self._darkest = (luminance, frame.copy())

    def record_serial(self, direction: str, line: str) -> None:
        stamp = datetime.now().isoformat(timespec="milliseconds")
        with self._lock:
            self._serial.append({"timestamp": stamp, "direction": direction, "line": line})
            if direction == "TX" and line.startswith("cameraledlevel:"):
                self._last_led_tx_mono = time.monotonic()
                try:
                    self._last_led_level = int(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif direction == "RX":
                self._last_firmware_response = line
                if self._last_led_tx_mono is not None:
                    self._last_serial_latency_ms = (time.monotonic() - self._last_led_tx_mono) * 1000.0
                    self._last_led_tx_mono = None


    def record_led_request(self, raw_value: int, percent: float | None = None, source: str = "") -> None:
        with self._lock:
            self._requested_led_raw = int(raw_value)
            self._requested_brightness_percent = None if percent is None else float(percent)
            self._events.append({
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event": "led_request",
                "detail": f"source={source};percent={percent};raw={raw_value}",
            })

    def record_capture_selection(
        self,
        metadata: dict[str, Any],
        selected_frame: np.ndarray | None = None,
        rejected_frames: list[np.ndarray] | None = None,
    ) -> None:
        """Record Run-screen settle/selection details for diagnostic export."""
        with self._lock:
            self._capture_session_number += 1
            session_id = self._capture_session_number
            row = dict(metadata)
            row["session_id"] = session_id
            self._capture_sessions.append(row)
            images: dict[str, Any] = {"session_id": session_id, "selected": None, "rejected": []}
            if selected_frame is not None:
                images["selected"] = self._encode_jpeg(selected_frame)
            for frame in (rejected_frames or [])[:3]:
                encoded = self._encode_jpeg(frame)
                if encoded is not None:
                    images["rejected"].append(encoded)
            self._capture_images.append(images)
            self._events.append({
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event": "classification_capture",
                "detail": (
                    f"session={session_id};settle_ms={row.get('settle_ms')};"
                    f"discarded={row.get('discarded_frames')};"
                    f"stable={row.get('stability_reached')};"
                    f"timeout={row.get('timeout_fallback')};"
                    f"duration_ms={row.get('duration_ms')};"
                    f"selected_sequence={row.get('selected_sequence')};"
                    f"trace_id={row.get('trace_id')};fresh={row.get('fresh_frame')};"
                    f"retry_same_case={row.get('retry_same_case')}"
                ),
            })


    def record_inference_pipeline(
        self,
        *,
        raw_frame: np.ndarray | None,
        processed_frame: np.ndarray | None,
        image_proc_config: dict[str, Any] | None = None,
        trace_id: str | None = None,
        frame_sequence: int | None = None,
    ) -> None:
        """Store paired raw/final images actually used by the inference path."""
        with self._lock:
            self._pipeline_session_number += 1
            session_id = self._pipeline_session_number
            raw = self._encode_jpeg(raw_frame) if raw_frame is not None else None
            processed = self._encode_jpeg(processed_frame) if processed_frame is not None else None
            row = {
                "session_id": session_id,
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "raw": raw,
                "processed": processed,
                "raw_shape": None if raw_frame is None else list(raw_frame.shape),
                "processed_shape": None if processed_frame is None else list(processed_frame.shape),
                "image_proc_config": dict(image_proc_config or {}),
                "trace_id": trace_id,
                "frame_sequence": frame_sequence,
            }
            self._pipeline_sessions.append(row)
            self._events.append({
                "timestamp": row["timestamp"],
                "event": "inference_pipeline",
                "detail": (f"session={session_id};trace_id={trace_id};frame_sequence={frame_sequence};"
                           f"raw_shape={row['raw_shape']};processed_shape={row['processed_shape']}"),
            })

    def record_event(self, event: str, detail: str = "") -> None:
        with self._lock:
            self._events.append({
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event": event,
                "detail": detail,
            })

    def record_sensor_test(self, result: dict[str, Any]) -> None:
        """Retain an explicit hardware-test result for diagnostic export."""
        row = dict(result)
        with self._lock:
            self._sensor_tests.append(row)
            self._events.append({
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event": "sensor_test",
                "detail": (
                    f"test={row.get('test')};status={row.get('status')};"
                    f"restored={row.get('settings_restored')};"
                    f"summary={row.get('summary')}"
                ),
            })

    def live_stats(self) -> dict[str, Any]:
        with self._lock:
            recent = list(self._frames)[-100:]
            lums = [float(row["luminance"]) for row in recent]
            intervals = [float(row["interval_ms"]) for row in recent if row["interval_ms"] != ""]
            return {
                "frame_count": self._frame_number,
                "current_luminance": lums[-1] if lums else None,
                "luminance_min": min(lums) if lums else None,
                "luminance_max": max(lums) if lums else None,
                "luminance_stdev": statistics.pstdev(lums) if len(lums) > 1 else 0.0,
                "mean_interval_ms": statistics.mean(intervals) if intervals else None,
                "last_led_level": self._last_led_level,
                "last_firmware_response": self._last_firmware_response,
                "requested_brightness_percent": self._requested_brightness_percent,
                "requested_led_raw": self._requested_led_raw,
                "last_serial_latency_ms": self._last_serial_latency_ms,
                "recent_serial": list(self._serial)[-20:],
            }

    def export_zip(
        self,
        destination: str | os.PathLike[str],
        camera_info: dict[str, Any],
        app_info: dict[str, Any],
        extra_members: dict[str, bytes] | None = None,
    ) -> Path:
        destination = Path(destination)
        with self._lock:
            frames = list(self._frames)
            serial_rows = list(self._serial)
            events = list(self._events)
            bright = None if self._brightest is None else self._brightest[1].copy()
            dark = None if self._darkest is None else self._darkest[1].copy()
            ref = None if self._reference is None else self._reference.copy()
            capture_sessions = list(self._capture_sessions)
            capture_images = list(self._capture_images)
            pipeline_sessions = list(self._pipeline_sessions)
            sensor_tests = list(self._sensor_tests)
            live = self.live_stats_unlocked()

        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "diagnostic_started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "live_stats": live,
            "camera": camera_info,
            "application": app_info,
            "sensor_test_count": len(sensor_tests),
        }
        system_info = {
            "platform": platform.platform(),
            "python": sys.version,
            "opencv": cv2.__version__,
            "machine": platform.machine(),
            "processor": platform.processor(),
        }

        with _atomic_zip(destination) as zf:
            zf.writestr("diagnostic_report.json", json.dumps(report, indent=2, default=str))
            active=app_info.get("connection_transport") or {}
            previous=app_info.get("last_disconnect_transport")
            if active.get("type")=="esp" or previous:
                zf.writestr("esp_transport_evidence.json",json.dumps(
                    {"connection_transport":active,"last_disconnect_transport":previous},
                    indent=2,default=str))
            zf.writestr("system_info.json", json.dumps(system_info, indent=2))
            zf.writestr("camera_properties.json", json.dumps(camera_info, indent=2, default=str))
            # Carry build provenance with every diagnostic archive. In a
            # PyInstaller ONEDIR build the report is collected under _MEIPASS;
            # from source it lives beside the project files.
            build_roots = [
                Path(getattr(sys, "_MEIPASS", "")) / "build_report" if getattr(sys, "_MEIPASS", None) else None,
                Path(__file__).resolve().parents[1] / "build_report",
            ]
            for build_root in build_roots:
                if build_root is None or not build_root.is_dir():
                    continue
                for build_file in sorted(build_root.rglob("*")):
                    if build_file.is_file():
                        zf.write(build_file, f"build/{build_file.relative_to(build_root).as_posix()}")
                break
            zf.writestr("camera_frames.csv", self._csv_bytes(frames, ["timestamp", "frame", "luminance", "interval_ms", "width", "height"]))
            zf.writestr("serial_commands.csv", self._csv_bytes(serial_rows, ["timestamp", "direction", "line"]))
            zf.writestr("events.csv", self._csv_bytes(events, ["timestamp", "event", "detail"]))
            zf.writestr(
                "sensor_tests.json",
                json.dumps(sensor_tests, indent=2, default=str),
            )
            zf.writestr("classification_capture_sessions.json", json.dumps(capture_sessions, indent=2, default=str))
            sample_rows: list[dict[str, Any]] = []
            for session in capture_sessions:
                for sample in session.get("samples", []):
                    sample_rows.append({
                        "session_id": session.get("session_id"),
                        "started_at": session.get("started_at"),
                        **sample,
                    })
            zf.writestr(
                "classification_capture_samples.csv",
                self._csv_bytes(sample_rows, ["session_id", "started_at", "sequence", "elapsed_ms", "motion_score", "sharpness_score", "stable_count"]),
            )
            for images in capture_images:
                session_id = images.get("session_id")
                if images.get("selected") is not None:
                    zf.writestr(f"classification_captures/session_{session_id:03d}_selected.jpg", images["selected"])
                for index, encoded in enumerate(images.get("rejected", []), start=1):
                    zf.writestr(f"classification_captures/session_{session_id:03d}_rejected_{index}.jpg", encoded)
            pipeline_manifest: list[dict[str, Any]] = []
            for session in pipeline_sessions:
                session_id = session.get("session_id")
                pipeline_manifest.append({
                    "session_id": session_id,
                    "timestamp": session.get("timestamp"),
                    "raw_shape": session.get("raw_shape"),
                    "processed_shape": session.get("processed_shape"),
                    "image_proc_config": session.get("image_proc_config"),
                    "trace_id": session.get("trace_id"),
                    "frame_sequence": session.get("frame_sequence"),
                })
                if session.get("raw") is not None:
                    zf.writestr(f"inference_pipeline/session_{session_id:03d}_raw.jpg", session["raw"])
                if session.get("processed") is not None:
                    zf.writestr(f"inference_pipeline/session_{session_id:03d}_processed.jpg", session["processed"])
            zf.writestr("inference_pipeline/manifest.json", json.dumps(pipeline_manifest, indent=2, default=str))
            self._write_image(zf, "sample_frames/brightest_frame.jpg", bright)
            self._write_image(zf, "sample_frames/darkest_frame.jpg", dark)
            self._write_image(zf, "sample_frames/reference_frame.jpg", ref)
            for name, data in sorted((extra_members or {}).items()):
                zf.writestr(_safe_extra_member(name), data)
        return destination

    def live_stats_unlocked(self) -> dict[str, Any]:
        recent = list(self._frames)[-100:]
        lums = [float(row["luminance"]) for row in recent]
        intervals = [float(row["interval_ms"]) for row in recent if row["interval_ms"] != ""]
        return {
            "frame_count": self._frame_number,
            "current_luminance": lums[-1] if lums else None,
            "luminance_min": min(lums) if lums else None,
            "luminance_max": max(lums) if lums else None,
            "luminance_stdev": statistics.pstdev(lums) if len(lums) > 1 else 0.0,
            "mean_interval_ms": statistics.mean(intervals) if intervals else None,
            "last_led_level": self._last_led_level,
            "last_firmware_response": self._last_firmware_response,
        }

    @staticmethod
    def _csv_bytes(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    @staticmethod
    def _encode_jpeg(frame: np.ndarray) -> bytes | None:
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return encoded.tobytes() if ok else None

    @staticmethod
    def _write_image(zf: zipfile.ZipFile, name: str, frame: np.ndarray | None) -> None:
        if frame is None:
            return
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if ok:
            zf.writestr(name, encoded.tobytes())
