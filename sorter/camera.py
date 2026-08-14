"""Cross-platform latest-frame camera capture.

Windows uses a native DirectShow/IAMStreamConfig graph and accepts an exact
advertised 1080p30 source mode in deterministic MJPG, NV12, YUY2 order. Linux
uses OpenCV/V4L2. Both backends publish into a single latest-frame slot; stale
preview frames are never queued.

Device metadata enumeration also stays native on Windows, avoiding the old
OpenCV probe path that could silently negotiate YUY2 while merely detecting a
camera.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np


# Resolutions probed by `list_cameras_with_metadata`. Add more here if the
# operator's camera advertises something not in the list.
COMMON_RESOLUTIONS: list[tuple[int, int]] = [
    (320, 240),
    (640, 360),
    (640, 480),
    (800, 600),
    (1024, 768),
    (1280, 720),
    (1280, 960),
    (1600, 1200),
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
]


def _preferred_backend() -> int:
    if sys.platform.startswith("win"):
        return cv2.CAP_DSHOW
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


# ----- friendly-name lookup ---------------------------------------------------


def _linux_camera_names() -> dict[int, str]:
    """video device index -> human-readable name, from /sys."""
    names: dict[int, str] = {}
    sysroot = "/sys/class/video4linux"
    if not os.path.isdir(sysroot):
        return names
    for entry in sorted(os.listdir(sysroot)):
        if not entry.startswith("video"):
            continue
        try:
            idx = int(entry[5:])
        except ValueError:
            continue
        name_path = os.path.join(sysroot, entry, "name")
        try:
            with open(name_path, "r", encoding="utf-8") as f:
                names[idx] = f.read().strip()
        except OSError:
            pass
    return names


def _linux_usb_identity(index: int) -> dict[str, str]:
    """Return USB VID/PID and stable device path details for a V4L2 node."""
    base = os.path.realpath(f"/sys/class/video4linux/video{index}/device")
    current = base
    for _ in range(8):
        vid_path = os.path.join(current, "idVendor")
        pid_path = os.path.join(current, "idProduct")
        if os.path.isfile(vid_path) and os.path.isfile(pid_path):
            try:
                with open(vid_path, encoding="ascii") as f:
                    vid = f.read().strip().lower()
                with open(pid_path, encoding="ascii") as f:
                    pid = f.read().strip().lower()
                serial = ""
                serial_path = os.path.join(current, "serial")
                if os.path.isfile(serial_path):
                    with open(serial_path, encoding="utf-8", errors="replace") as f:
                        serial = f.read().strip()
                return {"vid": vid, "pid": pid, "serial": serial, "device": f"/dev/video{index}"}
            except OSError:
                break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return {"vid": "", "pid": "", "serial": "", "device": f"/dev/video{index}"}


def _windows_camera_names() -> dict[int, str]:
    """device index -> name, via pygrabber's DirectShow enumeration.

    Returns an empty dict if pygrabber is not installed (it's a Windows-only
    optional dep).
    """
    try:
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]
    except Exception:
        return {}
    try:
        graph = FilterGraph()
        return {i: name for i, name in enumerate(graph.get_input_devices())}
    except Exception:
        return {}


def camera_names() -> dict[int, str]:
    if sys.platform.startswith("linux"):
        return _linux_camera_names()
    if sys.platform.startswith("win"):
        return _windows_camera_names()
    return {}


def _windows_camera_resolutions(indices: list[int]) -> dict[int, list[tuple[int, int]]]:
    """device index -> sorted resolutions via DirectShow's GetStreamCaps.

    OpenCV's DSHOW set/get probe is unreliable — once FOURCC is set during
    probing, the device commonly wedges and reports only 640x480 for every
    width/height query for the rest of the session. pygrabber's
    ``get_formats()`` walks the device's advertised media types directly
    (IAMStreamConfig::GetStreamCaps), so it returns the real list. Returns
    {} if pygrabber isn't installed.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]
    except Exception:
        return {}

    # comtypes only auto-inits COM on the thread that first imports it
    # (the main thread). Worker threads — like the one run_worker hands
    # this off to during the startup auto-detect — start with COM
    # uninitialised, which makes the very first FilterGraph() build a
    # broken graph and get_formats() return nothing. Init/uninit per-call
    # so we work regardless of caller thread.
    try:
        import comtypes  # type: ignore[import-not-found]
        comtypes.CoInitialize()
        com_inited = True
    except Exception:
        comtypes = None  # type: ignore[assignment]
        com_inited = False

    try:
        out: dict[int, list[tuple[int, int]]] = {}
        for idx in indices:
            try:
                graph = FilterGraph()
                graph.add_video_input_device(idx)
                formats = graph.get_input_device().get_formats()
            except Exception:
                continue
            seen: set[tuple[int, int]] = set()
            for f in formats:
                w = int(f.get("width", 0) or 0)
                h = int(f.get("height", 0) or 0)
                if w > 0 and h > 0:
                    seen.add((w, h))
            if seen:
                out[idx] = sorted(seen, key=lambda wh: wh[0] * wh[1])
        return out
    finally:
        if com_inited and comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def _windows_dshow_device_count() -> int | None:
    """Number of currently-attached DirectShow capture devices, via pygrabber.

    Returns None if pygrabber isn't installed (callers should treat as
    "unknown — try every index"). Returns 0 if no devices are connected.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        import comtypes  # type: ignore[import-not-found]
        comtypes.CoInitialize()
        com_inited = True
    except Exception:
        comtypes = None  # type: ignore[assignment]
        com_inited = False
    try:
        try:
            graph = FilterGraph()
            return len(graph.get_input_devices())
        except Exception:
            return None
    finally:
        if com_inited and comtypes is not None:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


# ----- enumeration ------------------------------------------------------------


def _candidate_indices(max_index: int) -> list[int]:
    """Indices worth probing.

    On Linux, skip indices with no /dev/videoN node at all — probing them
    just produces OpenCV's noisy "can't open camera by index" V4L2 warnings.
    On Windows, ask DirectShow how many capture devices are attached so
    we don't fire OpenCV at indices 1-9 when only camera 0 exists (each
    miss prints "VIDEOIO(DSHOW): backend is generally available but can't
    be used to capture by index").
    """
    if sys.platform.startswith("linux"):
        return [i for i in range(max_index) if os.path.exists(f"/dev/video{i}")]
    if sys.platform.startswith("win"):
        count = _windows_dshow_device_count()
        if count is not None:
            return list(range(min(count, max_index)))
    return list(range(max_index))


def enumerate_devices(max_index: int = 10, probe_timeout_s: float = 1.5) -> list[int]:
    """Return camera indices that successfully opened and returned a frame.

    Each probe runs in a worker thread; we move on if it exceeds probe_timeout_s
    (some V4L2 devices hang indefinitely on open).
    """
    backend = _preferred_backend()
    found: list[int] = []
    for idx in _candidate_indices(max_index):
        result: list[bool] = [False]

        def _probe(i: int = idx) -> None:
            cap = cv2.VideoCapture(i, backend)
            try:
                if cap.isOpened():
                    ok, _ = cap.read()
                    if ok:
                        result[0] = True
            finally:
                cap.release()

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(probe_timeout_s)
        if result[0]:
            found.append(idx)
    return found


def _probe_resolutions(cap: cv2.VideoCapture) -> list[tuple[int, int]]:
    """Try each resolution in COMMON_RESOLUTIONS; collect what the device actually serves.

    DirectShow on Windows commonly substitutes the closest supported size when
    you ask for one it doesn't have, so we add the resolution the camera
    *returned* (not the one we asked for) — every returned size is, by
    definition, one the device supports.
    """
    # Match the playback pixel format on V4L2. Many UVC webcams advertise
    # 1080p only under MJPG; the V4L2 default of YUYV refuses (or caps the
    # FPS of) the higher modes, so probing under YUYV under-reports what the
    # camera can actually deliver at runtime. On DirectShow this set() can
    # leave the device stuck reporting 640x480 for every subsequent get(),
    # so the Windows path uses _windows_camera_resolutions instead and only
    # falls back here if pygrabber isn't installed.
    if sys.platform.startswith("linux"):
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    supported: set[tuple[int, int]] = set()
    if orig_w > 0 and orig_h > 0:
        supported.add((orig_w, orig_h))
    for w, h in COMMON_RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if actual_w > 0 and actual_h > 0:
            supported.add((actual_w, actual_h))
    if orig_w and orig_h:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, orig_w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, orig_h)
    # Sort by total pixel count so callers can pick "highest" as the last item.
    return sorted(supported, key=lambda wh: wh[0] * wh[1])


def list_cameras_with_metadata(
    max_index: int = 10, probe_timeout_s: float = 2.5
) -> list[dict]:
    """Enumerate cameras and return [{'index': int, 'name': str, 'resolutions': [(w,h), ...]}].

    Resolutions are sorted ascending by pixel count, so `resolutions[-1]` is the
    highest the device accepts among COMMON_RESOLUTIONS.
    """
    backend = _preferred_backend()
    names = camera_names()
    candidates = _candidate_indices(max_index)

    # On Windows, query each device's supported resolutions via DirectShow
    # before any OpenCV capture is opened. The OpenCV DSHOW probe used on
    # Linux (set width/height, read back actual) reliably wedges at 640x480
    # on Windows once FOURCC has been touched, so the native enumeration is
    # both faster and accurate.
    pre_resolutions: dict[int, list[tuple[int, int]]] = {}
    if sys.platform.startswith("win"):
        pre_resolutions = _windows_camera_resolutions(candidates)

    # On Windows the DirectShow device list and IAMStreamConfig capabilities
    # are sufficient for enumeration. Do not briefly open each camera through
    # OpenCV: that legacy probe could negotiate YUY2, consume USB bandwidth,
    # and disturb the exact MJPG graph that starts immediately afterward.
    if sys.platform.startswith("win"):
        return [
            {
                "index": idx,
                "name": names.get(idx, f"Camera {idx}"),
                "resolutions": pre_resolutions.get(idx, []),
                "vid": "", "pid": "", "serial": "", "device": str(idx),
            }
            for idx in candidates
        ]

    out: list[dict] = []
    for idx in candidates:
        result: dict = {"opened": False, "resolutions": [], "name": ""}

        def _probe(i: int = idx) -> None:
            cap = cv2.VideoCapture(i, backend)
            try:
                if not cap.isOpened():
                    return
                resolutions = _probe_resolutions(cap)
                ok, _ = cap.read()
                if not ok:
                    return
                result["opened"] = True
                result["resolutions"] = resolutions
            finally:
                cap.release()

        t = threading.Thread(target=_probe, daemon=True)
        t.start()
        t.join(probe_timeout_s)
        if result["opened"]:
            out.append({
                "index": idx,
                "name": names.get(idx, f"Camera {idx}"),
                "resolutions": result["resolutions"],
                **_linux_usb_identity(idx),
            })
    return out


class Camera:
    _instances_lock = threading.Lock()
    _active_instances = 0
    WINDOWS_SOURCE_FORMAT_PRIORITY = ("MJPG", "NV12", "YUY2")

    def __init__(self, device_index: int = 0, width: int = 1920, height: int = 1080) -> None:
        self.device_index = device_index
        self.width = width
        self.height = height
        self._cap: Optional[cv2.VideoCapture] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_sequence = 0
        self._frame_lock = threading.Lock()
        self._frame_condition = threading.Condition(self._frame_lock)
        self._latest_frame_monotonic: float | None = None
        self._latest_frame_wall_time: float | None = None
        # OpenCV capture backends are not safe for overlapping read/set calls.
        # Keep all device access serialized, especially on DirectShow.
        self._capture_lock = threading.Lock()
        self._discard_until = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counted_active = False
        self._requested_fourcc = "MJPG"
        self._negotiated_fourcc = ""
        self._mjpg_verified = False
        self._compatibility_mode = False
        self._selected_backend = ""
        self._mode_attempts: list[dict[str, object]] = []
        # Windows-only DirectShow graph. Bind one complete advertised media
        # capability before graph construction and publish decoded BGR frames.
        self._dshow_graph = None
        self._dshow_comtypes = None
        self._dshow_frame_event = threading.Event()
        self._dshow_callback_error: str | None = None
        self._dshow_format: dict[str, object] | None = None
        self._callback_count = 0
        self._callback_started_monotonic: float | None = None
        self._callback_last_monotonic: float | None = None

    @staticmethod
    def _frame_is_usable(frame: Optional[np.ndarray]) -> bool:
        """Reject only empty or completely zero-filled capture frames."""
        return bool(frame is not None and frame.size and frame.ndim >= 2 and np.any(frame))

    @staticmethod
    def _format_is_mjpg(fmt: dict[str, object]) -> bool:
        """Return whether an advertised DirectShow capability is MJPG."""
        return Camera._is_mjpg_media_type(fmt.get("media_type_str", ""))

    @staticmethod
    def _normalize_windows_source_format(value: object) -> str:
        """Normalize the three Windows source formats accepted by this build."""
        text = str(value or "").upper()
        if "MJPG" in text or "MJPEG" in text or text.endswith("JPEG"):
            return "MJPG"
        if "NV12" in text:
            return "NV12"
        if "YUY2" in text or "YUYV" in text:
            return "YUY2"
        return ""

    @classmethod
    def _windows_capability_candidates(
        cls,
        formats: list[dict[str, object]],
        width: int,
        height: int,
        fps: float = 30.0,
    ) -> list[tuple[str, float, dict[str, object]]]:
        """Return exact modes in stable format-priority and rate order."""
        candidates: list[tuple[str, float, dict[str, object]]] = []
        for fmt in formats:
            row = dict(fmt)
            row_width = int(row.get("width", 0) or 0)
            row_height = abs(int(row.get("height", 0) or 0))
            source_format = cls._normalize_windows_source_format(
                row.get("media_type_str", row.get("media_type", ""))
            )
            frame_rates = [
                float(row.get("min_framerate", 0.0) or 0.0),
                float(row.get("max_framerate", 0.0) or 0.0),
            ]
            max_fps = max(frame_rates)
            if (
                row_width == int(width)
                and row_height == int(height)
                and source_format in cls.WINDOWS_SOURCE_FORMAT_PRIORITY
                and max_fps + 0.25 >= float(fps)
            ):
                candidates.append((source_format, max_fps, row))

        priority = {
            source_format: index
            for index, source_format in enumerate(cls.WINDOWS_SOURCE_FORMAT_PRIORITY)
        }
        candidates.sort(
            key=lambda item: (
                priority[item[0]],
                -item[1],
                int(item[2].get("index", 0) or 0),
            )
        )
        return candidates

    @staticmethod
    def _format_supports_fps(fmt: dict[str, object], fps: float) -> bool:
        values = [
            float(fmt.get("min_framerate", 0.0) or 0.0),
            float(fmt.get("max_framerate", 0.0) or 0.0),
        ]
        return max(values) + 0.25 >= float(fps)

    @property
    def _native_graph(self):
        """Compatibility alias for the single active DirectShow graph."""
        return self._dshow_graph

    @_native_graph.setter
    def _native_graph(self, value) -> None:
        self._dshow_graph = value

    @property
    def _native_format(self):
        """Compatibility alias for the selected DirectShow capability."""
        return self._dshow_format

    @_native_format.setter
    def _native_format(self, value) -> None:
        self._dshow_format = value

    @staticmethod
    def _is_mjpg_media_type(value: object) -> bool:
        text = str(value or "").upper()
        return "MJPG" in text or "MJPEG" in text or text.endswith("JPEG")

    def _publish_frame(self, frame: np.ndarray) -> None:
        if not self._frame_is_usable(frame):
            return
        if time.monotonic() < self._discard_until:
            return
        contiguous = np.ascontiguousarray(frame)
        mono = time.monotonic()
        wall = time.time()
        with self._frame_condition:
            # SampleGrabber/OpenCV may reuse their callback/read buffer. One
            # ownership copy here is required; consumers copy only on demand.
            self._latest_frame = contiguous.copy()
            self._frame_sequence += 1
            self._latest_frame_monotonic = mono
            self._latest_frame_wall_time = wall
            self._callback_count += 1
            if self._callback_started_monotonic is None:
                self._callback_started_monotonic = mono
            self._callback_last_monotonic = mono
            self._frame_condition.notify_all()
        self._dshow_frame_event.set()

    @staticmethod
    def _release_dshow_graph(graph) -> None:
        if graph is None:
            return
        try:
            graph.stop()
        except Exception:
            pass
        try:
            graph.remove_filters()
        except Exception:
            pass

    def _clear_published_frame(self) -> None:
        with self._frame_condition:
            self._latest_frame = None
            self._latest_frame_monotonic = None
            self._latest_frame_wall_time = None
        self._dshow_frame_event.clear()
        self._dshow_callback_error = None
        self._callback_count = 0
        self._callback_started_monotonic = None
        self._callback_last_monotonic = None

    def _open_windows_native_dshow(self) -> bool:
        """Open an exact native DirectShow 1080p30 source capability.

        MJPG remains preferred. NV12 and YUY2 are compatibility modes for
        cameras whose Windows pipeline does not expose MJPG. Each candidate
        uses a fresh graph and must deliver an exact-size BGR frame before it
        is accepted. OpenCV negotiation is never used on Windows.
        """
        try:
            import comtypes  # type: ignore[import-not-found]
            from pygrabber.dshow_graph import FilterGraph  # type: ignore[import-not-found]
        except Exception as exc:
            self._mode_attempts.append({
                "backend": "Native DirectShow / pygrabber",
                "opened": False,
                "reason": "native_dshow_dependency_missing",
                "error": repr(exc),
            })
            return False

        comtypes.CoInitialize()
        self._dshow_comtypes = comtypes
        discovery_graph = None
        try:
            discovery_graph = FilterGraph()
            devices = list(discovery_graph.get_input_devices())
            if self.device_index < 0 or self.device_index >= len(devices):
                raise RuntimeError(
                    f"Camera index {self.device_index} is unavailable; DirectShow found {len(devices)} device(s)."
                )
            discovery_graph.add_video_input_device(self.device_index)
            advertised = [
                dict(fmt) for fmt in discovery_graph.get_input_device().get_formats()
            ]
            candidates = self._windows_capability_candidates(
                advertised, self.width, self.height, 30.0
            )
            self._release_dshow_graph(discovery_graph)
            discovery_graph = None
            if not candidates:
                self._mode_attempts.append({
                    "backend": "Native DirectShow / pygrabber",
                    "opened": False,
                    "reason": "no_supported_exact_capability",
                    "requested_width": int(self.width),
                    "requested_height": int(self.height),
                    "requested_fps": 30.0,
                    "format_priority": list(self.WINDOWS_SOURCE_FORMAT_PRIORITY),
                    "advertised_formats": advertised,
                })
                raise RuntimeError(
                    f"Camera does not advertise MJPG, NV12, or YUY2 "
                    f"{self.width}x{self.height} at 30 FPS through DirectShow."
                )

            def on_frame(image: np.ndarray) -> None:
                try:
                    self._publish_frame(image)
                except Exception as exc:  # callback must never unwind into COM
                    self._dshow_callback_error = repr(exc)

            for source_format, _max_fps, selected in candidates:
                graph = None
                self._clear_published_frame()
                self._dshow_format = dict(selected)
                try:
                    graph = FilterGraph()
                    graph.add_video_input_device(self.device_index)
                    graph.get_input_device().set_format(int(selected["index"]))
                    graph.add_sample_grabber(on_frame)
                    graph.add_null_render()
                    graph.prepare_preview_graph()
                    graph.run()

                    # Prove that DirectShow can convert the selected source to
                    # the BGR frame shape expected by the unchanged pipeline.
                    for attempt in range(1, 31):
                        graph.grab_frame()
                        if self._dshow_frame_event.wait(timeout=0.12):
                            with self._frame_lock:
                                shape = (
                                    None
                                    if self._latest_frame is None
                                    else self._latest_frame.shape
                                )
                            if (
                                shape is not None
                                and shape[1] == self.width
                                and shape[0] == self.height
                            ):
                                self._dshow_graph = graph
                                self._selected_backend = (
                                    "Native DirectShow / IAMStreamConfig"
                                )
                                self._negotiated_fourcc = source_format
                                self._mjpg_verified = source_format == "MJPG"
                                self._compatibility_mode = not self._mjpg_verified
                                self._mode_attempts.append({
                                    "backend": self._selected_backend,
                                    "opened": True,
                                    "camera_name": devices[self.device_index],
                                    "source_format": source_format,
                                    "compatibility_mode": self._compatibility_mode,
                                    "format_priority": list(
                                        self.WINDOWS_SOURCE_FORMAT_PRIORITY
                                    ),
                                    "requested_width": int(self.width),
                                    "requested_height": int(self.height),
                                    "requested_fps": 30.0,
                                    "selected_capability": dict(selected),
                                    "delivered_width": int(shape[1]),
                                    "delivered_height": int(shape[0]),
                                    "mjpg_verified": self._mjpg_verified,
                                    "startup_grab_attempts": attempt,
                                })
                                if not self._counted_active:
                                    with self._instances_lock:
                                        type(self)._active_instances += 1
                                    self._counted_active = True
                                return True
                        self._dshow_frame_event.clear()
                    raise RuntimeError(
                        f"{source_format} graph did not deliver a verified "
                        f"{self.width}x{self.height} frame."
                    )
                except Exception as exc:
                    self._mode_attempts.append({
                        "backend": "Native DirectShow / IAMStreamConfig",
                        "opened": False,
                        "reason": "capability_graph_failed",
                        "source_format": source_format,
                        "compatibility_mode": source_format != "MJPG",
                        "error": repr(exc),
                        "callback_error": self._dshow_callback_error,
                        "selected_capability": dict(selected),
                    })
                    self._release_dshow_graph(graph)
                    self._dshow_graph = None

            raise RuntimeError(
                "All exact 1920x1080 30 FPS DirectShow source modes failed."
            )
        except Exception as exc:
            self._mode_attempts.append({
                "backend": "Native DirectShow / IAMStreamConfig",
                "opened": False,
                "reason": "native_dshow_graph_failed",
                "error": repr(exc),
                "selected_capability": self._dshow_format,
                "format_priority": list(self.WINDOWS_SOURCE_FORMAT_PRIORITY),
            })
            self._release_dshow_graph(discovery_graph)
            self._dshow_graph = None
            self._dshow_format = None
            self._clear_published_frame()
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
            self._dshow_comtypes = None
            return False

    def open(self) -> bool:
        """Open the camera. Windows requires an exact native 1080p30 mode."""
        self._mode_attempts = []
        self._negotiated_fourcc = ""
        self._mjpg_verified = False
        self._compatibility_mode = False
        if sys.platform.startswith("win"):
            # Do not create an OpenCV VideoCapture on Windows. Capability
            # selection and validation remain explicit in the native graph.
            return self._open_windows_native_dshow()

        backend = _preferred_backend()
        self._selected_backend = "V4L2" if backend == getattr(cv2, "CAP_V4L2", -999) else str(backend)
        self._cap = cv2.VideoCapture(self.device_index, backend)
        if not self._cap.isOpened():
            self._cap = None
            return False
        if not self._counted_active:
            with self._instances_lock:
                type(self)._active_instances += 1
            self._counted_active = True
        requested_fourcc_value = cv2.VideoWriter_fourcc(*self._requested_fourcc)
        self._cap.set(cv2.CAP_PROP_FOURCC, requested_fourcc_value)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.width))
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.height))
        self._cap.set(cv2.CAP_PROP_FPS, 30)
        self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
        self._cap.set(cv2.CAP_PROP_EXPOSURE, 156)
        ok, frame = self._cap.read()
        if not ok or not self._frame_is_usable(frame):
            self.stop()
            return False
        self.width, self.height = int(frame.shape[1]), int(frame.shape[0])
        fourcc_value = int(self._cap.get(cv2.CAP_PROP_FOURCC))
        self._negotiated_fourcc = "".join(chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00")
        self._mjpg_verified = self._is_mjpg_media_type(self._negotiated_fourcc)
        self._publish_frame(frame)
        return True

    def start_preview(self) -> bool:
        if self._cap is None and self._dshow_graph is None and not self.open():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        target = self._native_grab_loop if self._dshow_graph is not None else self._grab_loop
        name = "DirectShowGrab" if self._dshow_graph is not None else "CameraGrab"
        self._thread = threading.Thread(target=target, name=name, daemon=True)
        self._thread.start()
        return True

    def _native_grab_loop(self) -> None:
        # The graph streams continuously; grab_frame arms the SampleGrabber for
        # the next arriving sample. Poll faster than 30 Hz to avoid missing a
        # sample while keeping only the newest decoded frame. COM must be
        # initialized on every thread that invokes a DirectShow interface.
        comtypes = None
        try:
            import comtypes  # type: ignore[import-not-found]
            comtypes.CoInitialize()
            period = 1.0 / 30.0
            next_request = time.monotonic()
            while not self._stop_event.is_set():
                graph = self._dshow_graph
                if graph is None:
                    return
                try:
                    graph.grab_frame()
                except Exception as exc:
                    self._dshow_callback_error = f"grab_frame failed: {exc}"
                    return
                next_request += period
                delay = next_request - time.monotonic()
                if delay > 0:
                    self._stop_event.wait(delay)
                else:
                    next_request = time.monotonic()
        finally:
            if comtypes is not None:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    def _grab_loop(self) -> None:
        """Linux/OpenCV capture loop; Windows uses `_native_grab_loop`."""
        while not self._stop_event.is_set():
            if self._cap is None:
                return
            with self._capture_lock:
                ok, frame = self._cap.read()
            if not ok or not self._frame_is_usable(frame):
                time.sleep(0.01)
                continue
            if time.monotonic() < self._discard_until:
                continue
            with self._frame_condition:
                self._latest_frame = frame
                self._frame_sequence += 1
                self._latest_frame_monotonic = time.monotonic()
                self._latest_frame_wall_time = time.time()
                self._frame_condition.notify_all()
            time.sleep(0.001)

    def latest_frame(self) -> Optional[np.ndarray]:
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def latest_frame_with_sequence(self) -> tuple[int, Optional[np.ndarray]]:
        """Return the published frame sequence and a safe frame copy."""
        with self._frame_lock:
            if self._latest_frame is None:
                return self._frame_sequence, None
            return self._frame_sequence, self._latest_frame.copy()

    def latest_frame_snapshot(self) -> tuple[int, Optional[np.ndarray], dict[str, object]]:
        """Return one atomic frame snapshot with acquisition timestamps."""
        with self._frame_lock:
            frame = None if self._latest_frame is None else self._latest_frame.copy()
            return self._frame_sequence, frame, {
                "sequence": int(self._frame_sequence),
                "acquired_monotonic": self._latest_frame_monotonic,
                "acquired_wall_time": self._latest_frame_wall_time,
            }

    def capture_stable_frame(
        self,
        *,
        settle_s: float = 0.30,
        discard_frames: int = 5,
        stable_frames: int = 3,
        timeout_s: float = 1.50,
        motion_threshold: float = 2.5,
        on_sample=None,
    ) -> tuple[Optional[np.ndarray], dict[str, object]]:
        """Wait for a fresh, stationary frame and select the sharpest candidate.

        This consumes the already-running preview stream; it does not create a
        second VideoCapture instance.  Unique published frames are used so a
        repeated copy of the same buffer cannot falsely satisfy stability.
        """
        started_wall = time.time()
        started = time.monotonic()
        # Record the stream position before the mechanical settle period.  A
        # classification candidate must be newer than this sequence; otherwise
        # a cached pre-movement preview frame could be selected.
        baseline_seq, _baseline_frame = self.latest_frame_with_sequence()
        time.sleep(max(0.0, settle_s))
        deadline = started + max(settle_s, timeout_s)
        last_seq = baseline_seq
        discarded = 0
        stable_count = 0
        previous_small = None
        candidates: list[tuple[float, np.ndarray, dict[str, object]]] = []
        samples: list[dict[str, object]] = []
        reached_stability = False

        while time.monotonic() < deadline:
            seq, frame = self.latest_frame_with_sequence()
            preview_active = bool(self._thread is not None and self._thread.is_alive())
            if not preview_active:
                # Match the Pi behavior: without an active preview thread, read
                # a genuinely fresh frame from VideoCapture instead of returning
                # the last cached image.  This is the path used by Run when the
                # camera tab is not streaming.
                if self._cap is None and not self.open():
                    time.sleep(0.02)
                    continue
                assert self._cap is not None
                with self._capture_lock:
                    ok, direct_frame = self._cap.read()
                if not ok or not self._frame_is_usable(direct_frame):
                    time.sleep(0.02)
                    continue
                frame = direct_frame
                seq = last_seq + 1
            elif frame is None or seq == last_seq:
                time.sleep(0.005)
                continue
            last_seq = seq
            if discarded < max(0, int(discard_frames)):
                discarded += 1
                continue

            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
            motion = None
            if previous_small is not None:
                motion = float(np.mean(cv2.absdiff(previous_small, gray)))
                stable_count = stable_count + 1 if motion <= motion_threshold else 0
            previous_small = gray
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            row: dict[str, object] = {
                "sequence": int(seq),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "motion_score": None if motion is None else round(motion, 5),
                "sharpness_score": round(sharpness, 5),
                "stable_count": stable_count,
            }
            samples.append(row)
            candidates.append((sharpness, frame, row))
            if on_sample is not None:
                try:
                    on_sample(dict(row))
                except Exception:
                    pass
            if stable_count >= max(1, int(stable_frames)):
                reached_stability = True
                break

        if candidates:
            # Prefer frames from the final stable run; otherwise use the
            # sharpest candidate observed before timeout.
            eligible = candidates[-max(1, stable_count + 1):] if reached_stability else candidates
            sharpness, selected, selected_row = max(eligible, key=lambda item: item[0])
        else:
            selected = self.capture_frame()
            selected_row = {}

        finished_wall = time.time()
        metadata: dict[str, object] = {
            "started_at": datetime.fromtimestamp(started_wall).isoformat(timespec="milliseconds"),
            "finished_at": datetime.fromtimestamp(finished_wall).isoformat(timespec="milliseconds"),
            "settle_ms": round(max(0.0, settle_s) * 1000.0, 3),
            "baseline_sequence": int(baseline_seq),
            "preview_stream_active": bool(self._thread is not None and self._thread.is_alive()),
            "discarded_frames": discarded,
            "required_stable_frames": int(stable_frames),
            "motion_threshold": float(motion_threshold),
            "timeout_ms": round(max(0.0, timeout_s) * 1000.0, 3),
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "stability_reached": reached_stability,
            "timeout_fallback": not reached_stability,
            "selected_sequence": selected_row.get("sequence"),
            "selected_motion_score": selected_row.get("motion_score"),
            "selected_sharpness_score": selected_row.get("sharpness_score"),
            "samples": samples,
        }
        if candidates:
            rejected = [item[1] for item in candidates if item[1] is not selected]
            if rejected:
                # First, middle, and final rejected views are enough to diagnose
                # motion without retaining every full-resolution frame.
                picks = [rejected[0], rejected[len(rejected) // 2], rejected[-1]]
                metadata["_rejected_frames"] = picks
        return selected, metadata

    def capture_frame_after_sequence(
        self,
        baseline_sequence: int,
        *,
        timeout_s: float = 1.20,
        frames_after_boundary: int = 1,
    ) -> tuple[Optional[np.ndarray], dict[str, object]]:
        """Wait for post-motion frames and return the second fresh frame.

        The first newly published frame can have been exposed or buffered while
        the wheel was still moving.  It is therefore retained for diagnostics
        but never classified.  Timeout never returns an older image.
        """
        started = time.monotonic()
        timeout_s = max(0.0, float(timeout_s))
        required = max(1, int(frames_after_boundary))
        deadline = started + timeout_s
        baseline_sequence = int(baseline_sequence)
        observed: list[tuple[int, np.ndarray, float | None, float | None]] = []
        last_sequence = baseline_sequence

        with self._frame_condition:
            while len(observed) < required:
                while self._frame_sequence <= last_sequence:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None, {
                            "capture_source": "post_motion_second_frame_timeout",
                            "baseline_sequence": baseline_sequence,
                            "first_post_motion_sequence": observed[0][0] if observed else None,
                            "selected_sequence": None,
                            "frames_after_boundary_required": required,
                            "frames_after_boundary_observed": len(observed),
                            "wait_ms": round((time.monotonic() - started) * 1000.0, 3),
                            "timeout_ms": round(timeout_s * 1000.0, 3),
                            "fresh_frame": False,
                            "timeout_fallback": False,
                            "retry_same_case": True,
                            "selected_acquired_monotonic": None,
                            "selected_acquired_wall_time": None,
                            "camera_requested_fourcc": self._requested_fourcc,
                            "camera_negotiated_fourcc": self._negotiated_fourcc,
                            "camera_mjpg_verified": self._mjpg_verified,
                            "camera_compatibility_mode": self._compatibility_mode,
                            "_rejected_frames": [item[1] for item in observed[:1]],
                        }
                    self._frame_condition.wait(timeout=remaining)

                frame = None if self._latest_frame is None else self._latest_frame.copy()
                if frame is None:
                    last_sequence = int(self._frame_sequence)
                    continue
                last_sequence = int(self._frame_sequence)
                observed.append((
                    last_sequence, frame, self._latest_frame_monotonic, self._latest_frame_wall_time
                ))

        sequence, frame, acquired_mono, acquired_wall = observed[-1]
        return frame, {
            "capture_source": "condition_second_post_motion_frame",
            "baseline_sequence": baseline_sequence,
            "first_post_motion_sequence": observed[0][0],
            "selected_sequence": sequence,
            "frames_after_boundary_required": required,
            "frames_after_boundary_observed": len(observed),
            "wait_ms": round((time.monotonic() - started) * 1000.0, 3),
            "timeout_ms": round(timeout_s * 1000.0, 3),
            "fresh_frame": True,
            "timeout_fallback": False,
            "retry_same_case": False,
            "selected_acquired_monotonic": acquired_mono,
            "selected_acquired_wall_time": acquired_wall,
            "camera_requested_fourcc": self._requested_fourcc,
            "camera_negotiated_fourcc": self._negotiated_fourcc,
            "camera_mjpg_verified": self._mjpg_verified,
            "camera_compatibility_mode": self._compatibility_mode,
            "_rejected_frames": [observed[0][1]],
        }

    def capture_frame(self) -> Optional[np.ndarray]:
        """Return the most recent frame. Opens & grabs once if no preview is running."""
        frame = self.latest_frame()
        if frame is not None:
            return frame
        if self._cap is None and self._dshow_graph is None and not self.open():
            return None
        if self._dshow_graph is not None:
            baseline, _ = self.latest_frame_with_sequence()
            try:
                self._dshow_graph.grab_frame()
            except Exception:
                return None
            deadline = time.monotonic() + 0.5
            with self._frame_condition:
                while self._frame_sequence <= baseline and time.monotonic() < deadline:
                    self._frame_condition.wait(timeout=deadline - time.monotonic())
                return None if self._latest_frame is None else self._latest_frame.copy()
        assert self._cap is not None
        for _ in range(3):
            with self._capture_lock:
                ok, frame = self._cap.read()
            if ok and self._frame_is_usable(frame):
                return frame
            time.sleep(0.05)
        return None


    def notify_lighting_change(self, settle_s: float = 0.8) -> None:
        """Hold the last stable preview while camera buffers/exposure settle."""
        self._discard_until = max(self._discard_until, time.monotonic() + max(0.0, settle_s))

    def diagnostic_info(self) -> dict[str, object]:
        with self._instances_lock:
            active = type(self)._active_instances
        if self._dshow_graph is not None:
            return {
                "device_index": self.device_index, "opened": True,
                "width": self.width, "height": self.height,
                "fps": 30.0, "fourcc": self._negotiated_fourcc,
                "backend_name": self._selected_backend,
                "requested_fourcc": self._requested_fourcc,
                "negotiated_fourcc": self._negotiated_fourcc,
                "source_format": self._negotiated_fourcc,
                "format_priority": list(self.WINDOWS_SOURCE_FORMAT_PRIORITY),
                "mjpg_verified": self._mjpg_verified,
                "compatibility_mode": self._compatibility_mode,
                "active_instances": active,
                "selected_backend": self._selected_backend,
                "selected_capability": dict(self._dshow_format or {}),
                "mode_attempts": list(self._mode_attempts),
                "last_error": self._dshow_callback_error,
                "published_frame_sequence": int(self._frame_sequence),
                "callback_frame_count": int(self._callback_count),
                "callback_average_fps": (
                    0.0 if self._callback_started_monotonic is None or self._callback_last_monotonic is None
                    or self._callback_last_monotonic <= self._callback_started_monotonic
                    else round((self._callback_count - 1) /
                               (self._callback_last_monotonic - self._callback_started_monotonic), 3)
                ),
            }
        cap = self._cap
        if cap is None:
            return {
                "device_index": self.device_index, "opened": False,
                "width": self.width, "height": self.height,
                "fps": 0.0, "fourcc": "", "backend_name": "closed",
                "active_instances": active, "selected_backend": self._selected_backend,
                "requested_fourcc": self._requested_fourcc,
                "negotiated_fourcc": self._negotiated_fourcc,
                "source_format": self._negotiated_fourcc,
                "format_priority": list(self.WINDOWS_SOURCE_FORMAT_PRIORITY),
                "mjpg_verified": self._mjpg_verified,
                "compatibility_mode": self._compatibility_mode,
                "mode_attempts": list(self._mode_attempts),
                "last_error": self._dshow_callback_error,
            }
        with self._capture_lock:
            try:
                backend_name = cap.getBackendName()
            except Exception:
                backend_name = str(_preferred_backend())
            fourcc_value = int(cap.get(cv2.CAP_PROP_FOURCC))
            fourcc = "".join(chr((fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00")
            return {
                "device_index": self.device_index, "opened": bool(cap.isOpened()),
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "fps": float(cap.get(cv2.CAP_PROP_FPS)), "fourcc": fourcc,
                "backend_name": backend_name, "requested_fourcc": self._requested_fourcc,
                "negotiated_fourcc": self._negotiated_fourcc or fourcc,
                "mjpg_verified": bool(self._mjpg_verified),
                "compatibility_mode": bool(self._compatibility_mode),
                "active_instances": active, "selected_backend": self._selected_backend,
                "mode_attempts": list(self._mode_attempts),
            }

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._dshow_graph is not None:
            try:
                self._dshow_graph.stop()
            except Exception:
                pass
            try:
                self._dshow_graph.remove_filters()
            except Exception:
                pass
            self._dshow_graph = None
            self._dshow_format = None
            if self._dshow_comtypes is not None:
                try:
                    self._dshow_comtypes.CoUninitialize()
                except Exception:
                    pass
                self._dshow_comtypes = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self._counted_active:
            with self._instances_lock:
                type(self)._active_instances = max(0, type(self)._active_instances - 1)
            self._counted_active = False
