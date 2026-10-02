"""Small, on-demand helpers for displaying usable local network addresses."""
from __future__ import annotations

import ipaddress
import socket


def _usable_private_ipv4(value: object) -> str | None:
    try:
        parsed = ipaddress.ip_address(str(value))
    except ValueError:
        return None
    if (
        parsed.version != 4
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_global
        or parsed.is_unspecified
    ):
        return None
    return str(parsed)


def local_ipv4_addresses() -> list[str]:
    """Return non-loopback, non-public IPv4 addresses advertised by this PC."""
    addresses: set[str] = set()
    try:
        records = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    for record in records:
        address = _usable_private_ipv4(record[4][0])
        if address is not None:
            addresses.add(address)
    return sorted(addresses)


def primary_local_ipv4_address() -> str | None:
    """Return the IPv4 address selected by the operating system's default route.

    The UDP connect selects an interface without sending application data and
    without starting a subprocess, worker, timer, or persistent socket.  This
    keeps virtual adapters out of the normal UI while diagnostics may still use
    :func:`local_ipv4_addresses` to report every candidate.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        selected = _usable_private_ipv4(sock.getsockname()[0])
    except OSError:
        selected = None
    finally:
        sock.close()
    if selected is not None:
        return selected
    addresses = local_ipv4_addresses()
    return addresses[0] if len(addresses) == 1 else None


def primary_local_ipv4_addresses() -> list[str]:
    """List form used by address-rendering and mDNS call sites."""
    address = primary_local_ipv4_address()
    return [address] if address else []
