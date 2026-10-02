"""Minimal public system information page."""
from __future__ import annotations

from pathlib import Path
import platform
import tkinter as tk
from tkinter import ttk

from sorter.version import PUBLIC_VERSION


SOFTWARE_VERSION = PUBLIC_VERSION
FIRMWARE_VERSION = "CS7.2"


def _computer_model() -> str:
    if platform.system() == "Windows":
        return platform.processor() or platform.machine() or "Windows PC"
    for path in (Path("/proc/device-tree/model"), Path("/sys/firmware/devicetree/base/model")):
        try:
            value = path.read_bytes().decode("utf-8", "replace").rstrip("\x00").strip()
            if value:
                return value
        except OSError:
            pass
    return platform.machine() or "Unavailable"


def _installed_ram() -> str:
    if platform.system() == "Windows":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return f"{status.ullTotalPhys / (1024 ** 3):.1f} GB"
        except Exception:
            pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kib = int(line.split()[1])
                gib = kib / (1024 * 1024)
                return f"{gib:.1f} GB"
    except (OSError, ValueError, IndexError):
        pass
    return "Unavailable"


class SystemInfoTab(ttk.Frame):
    """Show only the approved public software, firmware, and hardware fields."""

    def __init__(self, parent, *, config=None, bus=None, app=None) -> None:
        super().__init__(parent, padding=24)
        self.config = config
        self.app = app
        self.columnconfigure(1, weight=1)

        ttk.Label(self, text="System Information", style="Header.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 22)
        )
        rows = (
            ("Software", SOFTWARE_VERSION),
            ("Firmware", FIRMWARE_VERSION),
            ("Computer", _computer_model()),
            ("Installed RAM", _installed_ram()),
        )
        for row, (label, value) in enumerate(rows, start=1):
            ttk.Label(self, text=label, style="KioskCaption.TLabel").grid(
                row=row, column=0, sticky="nw", padx=(0, 30), pady=10
            )
            ttk.Label(self, text=value, style="KioskBody.TLabel").grid(
                row=row, column=1, sticky="nw", pady=10
            )

        ttk.Separator(self, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(22, 16)
        )
        ttk.Label(
            self,
            text=(
                "Kiosk Edition by ShibbyPrints, based on GPL-3.0-or-later "
                "source code originally created by SJSeth Solutions."
            ),
            style="KioskBody.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w")

        ttk.Separator(self, orient="horizontal").grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(22, 16)
        )
        ttk.Label(self, text="Input Devices", style="KioskCaption.TLabel").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )
        self.input_devices_var = tk.StringVar(value="Detecting input devices…")
        ttk.Label(
            self, textvariable=self.input_devices_var, style="KioskBody.TLabel",
            justify="left", wraplength=720,
        ).grid(row=9, column=0, columnspan=2, sticky="w")
        if app is not None and hasattr(app, "input_state"):
            self.refresh_input_devices(app.input_state)

    def refresh_input_devices(self, state) -> None:
        mouse = "Detected" if state.mouse_present else "Not connected"
        touch = "Detected" if state.touchscreen_present else "Not detected"
        keyboard = "Detected" if state.keyboard_present else "Not connected"
        cursor = "Visible" if state.mouse_present else "Hidden"
        mouse_detail = f" ({', '.join(state.mouse_names)})" if state.mouse_names else ""
        touch_detail = f" ({', '.join(state.touchscreen_names)})" if state.touchscreen_names else ""
        self.input_devices_var.set(
            f"Touchscreen: {touch}{touch_detail}\n"
            f"Mouse or trackball: {mouse}{mouse_detail}\n"
            f"Keyboard: {keyboard}\n"
            f"Operator cursor: {cursor} (Automatic)"
        )
