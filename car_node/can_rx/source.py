"""Receive-only access to the vehicle CAN bus.

SAFETY: the telemetry node is a passive tap on the vehicle bus. It must never
transmit, and on real hardware the CAN controller is put in listen-only
(silent) mode so it cannot even acknowledge frames:

    ip link set can0 type can bitrate 500000 listen-only on

This module enforces both:
  * ReceiveOnlyBus exposes recv() and shutdown() only. There is no send().
  * check_listen_only() refuses to start on a real CAN interface that is not
    in listen-only mode (vcan has no controller, so it is exempt).

The subpackage is called can_rx rather than can so it can never shadow the
python-can library's `import can`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import can

from common.canbus import open_bus


class ListenOnlyError(RuntimeError):
    """A real CAN interface is not in listen-only mode."""


@dataclass(frozen=True)
class InterfaceMode:
    kind: str  # "vcan", "can", "unknown", ...
    listen_only: bool
    description: str


def parse_ip_link_json(text: str) -> InterfaceMode:
    """Interpret `ip -details -json link show <iface>` output."""
    try:
        info = json.loads(text)[0]
    except (ValueError, IndexError, TypeError):
        return InterfaceMode("unknown", False, "could not parse ip link output")
    linkinfo = info.get("linkinfo", {})
    kind = linkinfo.get("info_kind", "unknown")
    ctrl = linkinfo.get("info_data", {}).get("ctrlmode", []) or []
    listen_only = "LISTEN-ONLY" in ctrl
    if kind == "vcan":
        return InterfaceMode(kind, False, "virtual CAN (vcan): no controller, listen-only not applicable")
    if kind == "can":
        state = "LISTEN-ONLY" if listen_only else "NOT listen-only"
        return InterfaceMode(kind, listen_only, f"CAN controller, {state}")
    return InterfaceMode(kind, listen_only, f"interface kind {kind!r}")


def interface_mode(channel: str) -> InterfaceMode:
    try:
        out = subprocess.run(
            ["ip", "-details", "-json", "link", "show", channel], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return InterfaceMode("unknown", False, "ip command not available")
    if out.returncode != 0:
        return InterfaceMode("missing", False, out.stderr.strip() or f"{channel} not found")
    return parse_ip_link_json(out.stdout)


def check_listen_only(interface: str, channel: str, require: bool) -> str:
    """Return a description of the bus mode; raise if a real bus is not silent."""
    if interface != "socketcan":
        return f"{interface}:{channel} (non-SocketCAN interface; set listen-only in its driver)"
    mode = interface_mode(channel)
    if mode.kind == "missing":
        raise ListenOnlyError(f"{channel}: {mode.description}")
    if mode.kind == "can" and not mode.listen_only and require:
        raise ListenOnlyError(
            f"{channel} is a real CAN interface but is NOT in listen-only mode. The telemetry node must be "
            f"passive. Run: sudo ip link set {channel} down; sudo ip link set {channel} type can "
            f"bitrate <rate> listen-only on; sudo ip link set {channel} up"
        )
    return f"{channel}: {mode.description}"


class ReceiveOnlyBus:
    """Read-only view of a python-can bus. Deliberately has no send()."""

    def __init__(self, bus: can.BusABC) -> None:
        self._bus = bus

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return self._bus.recv(timeout)

    def shutdown(self) -> None:
        self._bus.shutdown()


def open_receive_only(
    interface: str, channel: str, can_ids: list[int], require_listen_only: bool
) -> tuple[ReceiveOnlyBus, str]:
    """Open the bus for listening only, with kernel filters for the needed IDs."""
    description = check_listen_only(interface, channel, require_listen_only)
    filters = [{"can_id": cid, "can_mask": 0x7FF, "extended": False} for cid in sorted(set(can_ids))]
    bus = open_bus(interface, channel, receive_own_messages=False, can_filters=filters or None)
    return ReceiveOnlyBus(bus), description
