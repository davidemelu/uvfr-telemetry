"""Opening CAN buses through python-can.

Supported interfaces:

  socketcan      Linux SocketCAN: vcan0 in the lab, can0 on the Raspberry Pi.
  udp_multicast  python-can's network virtual bus for machines without
                 SocketCAN (macOS, Windows, most WSL2 kernels). Every process
                 on the host that joins the same multicast group sees the same
                 frames. Needs the optional 'msgpack' package.
  virtual        In-process bus; used by unit tests.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import can

# python-can's documented IPv4 default group for the udp_multicast interface.
UDP_MULTICAST_DEFAULT_GROUP = "239.74.163.2"


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def open_bus(
    interface: str,
    channel: str,
    *,
    receive_own_messages: bool = False,
    can_filters: list[dict[str, Any]] | None = None,
) -> can.BusABC:
    if interface == "udp_multicast" and not _is_ip(channel):
        # Lets the same config (channel: vcan0) work on non-Linux machines.
        channel = UDP_MULTICAST_DEFAULT_GROUP
    kwargs: dict[str, Any] = {
        "interface": interface,
        "channel": channel,
        "receive_own_messages": receive_own_messages,
    }
    if can_filters is not None:
        kwargs["can_filters"] = can_filters
    return can.Bus(**kwargs)
