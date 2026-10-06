"""Binary telemetry protocol shared by the car node and the pit receiver.

It lives in common/ rather than car_node/ because both ends depend on it and
the pit receiver must not depend on the car node package.
"""

from common.protocol.channels import WIRE_TYPES, ChannelLayout, ChannelSpec, Missing, WireType
from common.protocol.packet import (
    OVERHEAD_BYTES,
    PROTOCOL_VERSION,
    CarStatus,
    Codec,
    DecodeError,
    Header,
    Packet,
    PacketType,
    StatusPacket,
    TelemetryPacket,
    crc16,
)

__all__ = [
    "OVERHEAD_BYTES",
    "PROTOCOL_VERSION",
    "WIRE_TYPES",
    "CarStatus",
    "ChannelLayout",
    "ChannelSpec",
    "Codec",
    "DecodeError",
    "Header",
    "Missing",
    "Packet",
    "PacketType",
    "StatusPacket",
    "TelemetryPacket",
    "WireType",
    "crc16",
]
