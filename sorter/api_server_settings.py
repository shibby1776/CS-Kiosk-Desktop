"""Persisted settings for the optional integrated inference API server."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass
from typing import Any

from .repository import SettingsRepo


SETTINGS_KEY = "integrated_api_server"


@dataclass
class ApiServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    api_key_hash: str = ""
    on_demand_cache_slots: int = 1
    remote_queue_limit: int = 4

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ApiServerSettings":
        data = dict(raw or {})
        settings = cls(
            host=str(data.get("host", "127.0.0.1") or "127.0.0.1"),
            port=int(data.get("port", 8000) or 8000),
            api_key_hash=str(data.get("api_key_hash", "") or ""),
            on_demand_cache_slots=int(data.get("on_demand_cache_slots", 1)),
            remote_queue_limit=int(data.get("remote_queue_limit", 4)),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.host not in {"127.0.0.1", "0.0.0.0"}:
            raise ValueError("Server address must be Local computer or Local network.")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("Server port must be between 1 and 65535.")
        if not 0 <= int(self.on_demand_cache_slots) <= 32:
            raise ValueError("On-demand cache slots must be between 0 and 32.")
        if not 1 <= int(self.remote_queue_limit) <= 32:
            raise ValueError("Remote queue limit must be between 1 and 32.")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def set_api_key(self, raw_key: str) -> None:
        key = str(raw_key or "").strip()
        self.api_key_hash = (
            hashlib.sha256(key.encode("utf-8")).hexdigest()
            if key else ""
        )

    def verify_api_key(self, raw_key: str) -> bool:
        if not self.api_key_hash:
            return True
        candidate = hashlib.sha256(
            str(raw_key or "").strip().encode("utf-8")
        ).hexdigest()
        return hmac.compare_digest(candidate, self.api_key_hash)


class ApiServerSettingsRepo:
    def __init__(self, db: Any) -> None:
        self.settings = SettingsRepo(db)

    def load(self) -> ApiServerSettings:
        try:
            return ApiServerSettings.from_dict(self.settings.get(SETTINGS_KEY, {}))
        except (TypeError, ValueError):
            return ApiServerSettings()

    def save(self, value: ApiServerSettings) -> None:
        self.settings.set(SETTINGS_KEY, value.to_dict())

    def is_configured(self) -> bool:
        return bool(self.load().api_key_hash)
