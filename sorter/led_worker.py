"""Coalescing background worker for camera LED level updates."""
from __future__ import annotations
import threading
from typing import Any

class LedCommandWorker:
    def __init__(self, *, app: Any, config: Any, bus: Any) -> None:
        self.app = app; self.config = config; self.bus = bus
        self._cv = threading.Condition(); self._pending: tuple[int, bool] | None = None
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="CameraLedWorker", daemon=True)
        self._thread.start()

    def submit(self, value: int, *, persist: bool = False) -> None:
        value = max(0, min(255, int(value)))
        with self._cv:
            # Coalesce rapid preview changes, but retain an explicit save request.
            if self._pending is not None and self._pending[1]:
                persist = True
            self._pending = (value, bool(persist))
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stopped = True; self._cv.notify_all()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                value, persist = self._pending; self._pending = None
            try:
                if persist:
                    init_settings = dict(self.config.serial.get("init_settings", {}))
                    init_settings["cameraledlevel"] = value
                    self.config.serial["init_settings"] = init_settings
                    self.config.save()
                broker = self.app.broker
                if broker is None or not broker.is_connected:
                    suffix = "saved; not connected" if persist else "preview; not connected"
                    self.bus.post("status", f"Camera LED level = {value} ({suffix}).")
                else:
                    # MSI-style behavior: every explicit operator/startup request is
                    # written to the controller. Do not trust a local duplicate cache,
                    # because the board may have reset since the previous command.
                    broker.send_command(f"cameraledlevel:{value}")
                    suffix = " (saved)" if persist else ""
                    self.bus.post("status", f"Camera LED level → {value}{suffix}")
            except Exception as exc:
                self.bus.post("status", f"LED send failed: {exc}")
