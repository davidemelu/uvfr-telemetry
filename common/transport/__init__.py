"""Packet transports. The protocol layer never knows which one is in use.

  udp_transport       Phase 0 links between processes (and to the simulated radio)
  serial_transport    USB radio modems, COBS framed (later phases)
  impaired_transport  wraps any transport to simulate an unreliable radio
"""

from __future__ import annotations

from typing import Any

from common.config import ConfigError, check_keys
from common.transport.base import Transport, TransportStats
from common.transport.impaired_transport import ImpairedTransport, Impairment
from common.transport.udp_transport import UdpTransport, parse_hostport


def transport_from_config(cfg: dict[str, Any], where: str, *, role: str) -> Transport:
    """Build a transport. role is "sender" (car node) or "receiver" (pit)."""
    kind = cfg.get("type") if isinstance(cfg, dict) else None
    if kind == "udp":
        if role == "sender":
            check_keys(cfg, {"type", "remote"}, where)
            return UdpTransport(remote=parse_hostport(cfg["remote"]))
        check_keys(cfg, {"type", "listen"}, where)
        return UdpTransport(bind=parse_hostport(cfg["listen"]))
    if kind == "serial":
        check_keys(cfg, {"type", "port", "baudrate"}, where)
        from common.transport.serial_transport import SerialTransport

        return SerialTransport(cfg["port"], int(cfg["baudrate"]))
    raise ConfigError(f"{where}: type must be 'udp' or 'serial', got {kind!r}")


__all__ = [
    "ImpairedTransport",
    "Impairment",
    "Transport",
    "TransportStats",
    "UdpTransport",
    "parse_hostport",
    "transport_from_config",
]
