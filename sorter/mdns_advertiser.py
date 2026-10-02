"""Optional mDNS advertisement for the enabled Web Interface.

The dependency and its worker are loaded only while Web Interface is running.
Direct IP addresses remain usable if advertisement cannot be established.
"""
from __future__ import annotations

import socket
from typing import Any


class MdnsAdvertiser:
    def __init__(self, sorter_name: str, port: int, addresses: list[str]) -> None:
        self.sorter_name = sorter_name
        self.port = int(port)
        self.addresses = list(addresses)
        self._zeroconf: Any | None = None
        self._service_info: Any | None = None
        self.error = ""

    @property
    def active(self) -> bool:
        return self._zeroconf is not None and self._service_info is not None

    def start(self) -> bool:
        if self.active:
            return True
        if not self.addresses:
            self.error = "No private IPv4 address is available for mDNS."
            return False
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf

            host = f"{self.sorter_name}.local."
            info = ServiceInfo(
                "_http._tcp.local.",
                f"{self.sorter_name}._http._tcp.local.",
                addresses=[socket.inet_aton(value) for value in self.addresses],
                port=self.port,
                properties={
                    b"product": b"ShibbyPrints Kiosk Sorter",
                    b"interface": b"web",
                },
                server=host,
            )
            zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            zeroconf.register_service(info, allow_name_change=False)
        except Exception as exc:
            try:
                zeroconf.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        self._zeroconf = zeroconf
        self._service_info = info
        self.error = ""
        return True

    def stop(self) -> None:
        zeroconf, self._zeroconf = self._zeroconf, None
        info, self._service_info = self._service_info, None
        if zeroconf is None:
            return
        try:
            if info is not None:
                zeroconf.unregister_service(info)
        finally:
            zeroconf.close()
