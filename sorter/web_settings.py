"""Persisted policy for the optional private-LAN Web Interface.

Reading these settings starts no threads, sockets, timers, or image work.
Browser sessions, CSRF protection, control leasing, and private-network
restrictions are enforced by :mod:`sorter.web_interface`; no sorter password
is required.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


WEB_SETTINGS_KEY = "web_interface"
DEFAULT_PORT = 8080
_SORTER_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class WebSettings:
    enabled: bool = False
    desktop_enabled: bool = True
    sorter_name: str = "shibbyprints"
    port: int = DEFAULT_PORT

    @classmethod
    def load(cls, settings_repo: Any) -> "WebSettings":
        raw = settings_repo.get(WEB_SETTINGS_KEY, {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            port = int(raw.get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        name = str(raw.get("sorter_name", "shibbyprints") or "shibbyprints")
        try:
            name = normalize_sorter_name(name)
        except ValueError:
            name = "shibbyprints"
        return cls(
            enabled=bool(raw.get("enabled", False)),
            desktop_enabled=bool(raw.get("desktop_enabled", True)),
            sorter_name=name,
            port=max(1024, min(65535, port)),
        )

    def save(self, settings_repo: Any) -> None:
        settings_repo.set(WEB_SETTINGS_KEY, {
            "enabled": bool(self.enabled),
            "desktop_enabled": bool(self.desktop_enabled),
            "sorter_name": normalize_sorter_name(self.sorter_name),
            "port": max(1024, min(65535, int(self.port))),
        })


def normalize_sorter_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if not _SORTER_NAME_RE.fullmatch(name):
        raise ValueError(
            "Sorter name must contain only letters, numbers, and interior "
            "hyphens, and must be 1 to 63 characters."
        )
    return name
