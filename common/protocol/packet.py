"""Compact binary telemetry protocol, version 1. See docs/protocol.md.

Every packet (all fields little-endian):

    offset  size  field
    0       1     version (high nibble) | packet type (low nibble)
    1       1     session id: random per car-node start, detects restarts
    2       2     sequence number: +1 per packet of any type, wraps at 65536
    4       4     source timestamp: ms since the car node started, wraps
    8       n     payload (depends on type)
    8+n     2     CRC-16/CCITT-FALSE over bytes 0 .. 8+n-1

TELEMETRY payload: a channel bitmap (1 bit per configured channel, bit i =
channel i present) followed by the present channels' values in index order,
each in its configured wire type.

STATUS payload: car-node health (layout hash, transmit and CAN counters,
CAN message ages); see StatusPacket.
"""

from __future__ import annotations

import binascii
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum

from common.protocol.channels import ChannelLayout, Missing

PROTOCOL_VERSION = 1

HEADER = struct.Struct("<BBHI")
CRC = struct.Struct("<H")
OVERHEAD_BYTES = HEADER.size + CRC.size  # 10

# STATUS fixed part: layout hash, tx packets, CAN frames, CAN frames/s,
# CAN counter gaps, CAN decode errors, CPU %.
STATUS_FIXED = struct.Struct("<HIIHHHB")
AGE_UNIT_MS = 20  # CAN message ages are sent in 20 ms units
AGE_SATURATED = 254  # >= 5.08 s
AGE_NEVER = 255  # never received
CPU_UNKNOWN = 255


class PacketType(IntEnum):
    TELEMETRY = 1
    STATUS = 2


class DecodeError(ValueError):
    """Packet rejected. `reason` is a short stable code for counters."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF). crc16(b"123456789") == 0x29B1."""
    return binascii.crc_hqx(data, 0xFFFF)


@dataclass(frozen=True)
class Header:
    version: int
    packet_type: PacketType
    session: int
    seq: int
    timestamp_ms: int


@dataclass(frozen=True)
class TelemetryPacket:
    header: Header
    values: dict[str, float | Missing]
    size: int


@dataclass(frozen=True)
class CarStatus:
    config_hash: int
    tx_packets: int
    can_frames: int
    can_fps: int
    can_counter_gaps: int
    can_errors: int
    cpu_pct: int | None
    message_ages_ms: tuple[float | None, ...]  # None = never received


@dataclass(frozen=True)
class StatusPacket:
    header: Header
    status: CarStatus
    config_match: bool
    size: int


Packet = TelemetryPacket | StatusPacket


def _u16(value: int) -> int:
    return min(int(value), 0xFFFF)


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _encode_age(age_ms: float | None) -> int:
    if age_ms is None:
        return AGE_NEVER
    return min(AGE_SATURATED, int(age_ms // AGE_UNIT_MS))


def _decode_age(raw: int) -> float | None:
    if raw == AGE_NEVER:
        return None
    return float(raw * AGE_UNIT_MS)


class Codec:
    def __init__(self, layout: ChannelLayout) -> None:
        self.layout = layout
        self.saturated = 0  # values clamped to the wire range while encoding
        self._channels = layout.channels
        self._n = len(layout.channels)
        self._bitmap_bytes = layout.bitmap_bytes
        self._status_size = STATUS_FIXED.size + len(layout.messages)

    # ----------------------------------------------------------------- sizes
    def telemetry_size(self, indices: list[int]) -> int:
        return OVERHEAD_BYTES + self._bitmap_bytes + sum(self._channels[i].wire.size for i in indices)

    @property
    def status_size(self) -> int:
        return OVERHEAD_BYTES + self._status_size

    # --------------------------------------------------------------- encoding
    @staticmethod
    def _header(packet_type: PacketType, session: int, seq: int, timestamp_ms: int) -> bytes:
        return HEADER.pack((PROTOCOL_VERSION << 4) | packet_type, session & 0xFF, seq & 0xFFFF, _u32(timestamp_ms))

    @staticmethod
    def _seal(body: bytes) -> bytes:
        return body + CRC.pack(crc16(body))

    def encode_telemetry(
        self, session: int, seq: int, timestamp_ms: int, values: Mapping[int, float | Missing]
    ) -> bytes:
        bitmap = 0
        parts = []
        for index in sorted(values):
            if not 0 <= index < self._n:
                raise ValueError(f"channel index {index} out of range")
            spec = self._channels[index]
            raw, saturated = spec.to_raw(values[index])
            self.saturated += saturated
            bitmap |= 1 << index
            parts.append(struct.pack("<" + spec.wire.fmt, raw))
        body = self._header(PacketType.TELEMETRY, session, seq, timestamp_ms)
        body += bitmap.to_bytes(self._bitmap_bytes, "little") + b"".join(parts)
        return self._seal(body)

    def encode_status(self, session: int, seq: int, timestamp_ms: int, status: CarStatus) -> bytes:
        if len(status.message_ages_ms) != len(self.layout.messages):
            raise ValueError("message_ages_ms must have one entry per layout message")
        body = self._header(PacketType.STATUS, session, seq, timestamp_ms)
        body += STATUS_FIXED.pack(
            status.config_hash & 0xFFFF,
            _u32(status.tx_packets),
            _u32(status.can_frames),
            _u16(status.can_fps),
            _u16(status.can_counter_gaps),
            _u16(status.can_errors),
            CPU_UNKNOWN if status.cpu_pct is None else max(0, min(254, int(status.cpu_pct))),
        )
        body += bytes(_encode_age(a) for a in status.message_ages_ms)
        return self._seal(body)

    # --------------------------------------------------------------- decoding
    def decode(self, data: bytes) -> Packet:
        size = len(data)
        if size < OVERHEAD_BYTES:
            raise DecodeError("too_short", f"{size} bytes, need at least {OVERHEAD_BYTES}")
        body, (crc,) = data[:-CRC.size], CRC.unpack_from(data, size - CRC.size)
        if crc16(body) != crc:
            raise DecodeError("bad_crc", f"CRC {crc:#06x} does not match {crc16(body):#06x}")
        ver_type, session, seq, ts = HEADER.unpack_from(body)
        version, ptype = ver_type >> 4, ver_type & 0x0F
        if version != PROTOCOL_VERSION:
            raise DecodeError("bad_version", f"protocol version {version}, expected {PROTOCOL_VERSION}")
        try:
            packet_type = PacketType(ptype)
        except ValueError:
            raise DecodeError("unknown_type", f"packet type {ptype}") from None
        header = Header(version, packet_type, session, seq, ts)
        payload = body[HEADER.size :]
        if packet_type is PacketType.TELEMETRY:
            return TelemetryPacket(header, self._decode_telemetry(payload), size)
        status = self._decode_status(payload)
        return StatusPacket(header, status, status.config_hash == self.layout.config_hash, size)

    def _decode_telemetry(self, payload: bytes) -> dict[str, float | Missing]:
        if len(payload) < self._bitmap_bytes:
            raise DecodeError("bad_length", "telemetry payload shorter than the channel bitmap")
        bitmap = int.from_bytes(payload[: self._bitmap_bytes], "little")
        if bitmap >> self._n:
            raise DecodeError("bad_bitmap", "bitmap names channels this layout does not have")
        present = [c for c in self._channels if bitmap & (1 << c.index)]
        expected = self._bitmap_bytes + sum(c.wire.size for c in present)
        if len(payload) != expected:
            raise DecodeError("bad_length", f"telemetry payload is {len(payload)} bytes, expected {expected}")
        values: dict[str, float | Missing] = {}
        offset = self._bitmap_bytes
        for spec in present:
            (raw,) = struct.unpack_from("<" + spec.wire.fmt, payload, offset)
            offset += spec.wire.size
            values[spec.name] = spec.from_raw(raw)
        return values

    def _decode_status(self, payload: bytes) -> CarStatus:
        if len(payload) != self._status_size:
            raise DecodeError("bad_length", f"status payload is {len(payload)} bytes, expected {self._status_size}")
        cfg_hash, tx, frames, fps, gaps, errors, cpu = STATUS_FIXED.unpack_from(payload)
        ages = tuple(_decode_age(b) for b in payload[STATUS_FIXED.size :])
        return CarStatus(cfg_hash, tx, frames, fps, gaps, errors, None if cpu == CPU_UNKNOWN else cpu, ages)
