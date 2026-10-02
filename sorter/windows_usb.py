"""On-demand Windows removable-drive discovery.

Importing this module has no side effects.  Windows is queried only when an
operator explicitly asks to import from or export to USB.
"""
from __future__ import annotations

import os
from pathlib import Path


def removable_usb_roots() -> list[Path]:
    """Return mounted Windows removable-drive roots in drive-letter order."""
    if os.name != "nt":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetLogicalDrives.restype = ctypes.c_uint32
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        drive_mask = int(kernel32.GetLogicalDrives())
        roots: list[Path] = []
        for index in range(26):
            if not drive_mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            candidate = Path(root)
            if int(kernel32.GetDriveTypeW(root)) == 2 and candidate.is_dir():
                roots.append(candidate)
        return roots
    except (AttributeError, OSError, TypeError, ValueError):
        return []


def resolve_usb_root(requested: str) -> Path:
    """Resolve a browser-selected root against the current removable set."""
    available = {str(root).casefold(): root for root in removable_usb_roots()}
    selected = available.get(str(requested or "").strip().casefold())
    if selected is None:
        raise ValueError("The selected removable USB drive is not available.")
    return selected
