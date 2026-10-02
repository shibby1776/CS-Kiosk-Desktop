"""Fail-closed PyTorch version checks for local checkpoint loading."""
from __future__ import annotations

import re
from typing import Any


MIN_TORCH_VERSION = (2, 10, 0)
MIN_TORCH_VERSION_TEXT = ".".join(str(part) for part in MIN_TORCH_VERSION)
_STABLE_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?(?:\+[^\s]+)?$"
)


class UnsafeTorchVersionError(RuntimeError):
    """Raised when local model loading cannot be performed safely."""


def stable_release_tuple(version: str) -> tuple[int, int, int] | None:
    """Return a stable release tuple, rejecting prerelease/unknown versions."""
    match = _STABLE_VERSION_RE.fullmatch(str(version).strip())
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
    )


def require_safe_torch(torch_module: Any) -> str:
    """Validate the imported module before any checkpoint deserialization."""
    version = str(getattr(torch_module, "__version__", "")).strip()
    parsed = stable_release_tuple(version)
    if parsed is None or parsed < MIN_TORCH_VERSION:
        shown = version or "unknown"
        raise UnsafeTorchVersionError(
            "Local model loading is disabled because the installed PyTorch "
            f"version ({shown}) is not an approved stable release. Install "
            f"PyTorch {MIN_TORCH_VERSION_TEXT} or newer, then restart the "
            "application."
        )
    return version
