"""Bandwidth actually used by the telemetry stream, measured at the pit.

Two views of the same stream:
  payload  bytes of channel values only (what we want to move)
  encoded  every byte of every valid packet: header, bitmap, values, CRC and
           STATUS packets (what the radio has to carry)

Serial radio links add COBS framing (2 bytes per packet), reported as
serial_framed_bits_per_s. Values flagged STALE or NO DATA still occupy their
bytes on the air, so they are counted too.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from common.protocol import ChannelLayout, Packet, TelemetryPacket

SERIAL_FRAMING_BYTES = 2  # COBS code byte + 0x00 delimiter


class BandwidthMeter:
    def __init__(self, layout: ChannelLayout) -> None:
        self._sizes = {c.name: c.wire.size for c in layout.channels}
        self.channels = [c.name for c in layout.channels]
        self._reset()
        self._t0: float | None = None

    def _reset(self) -> None:
        self.telemetry_packets = 0
        self.status_packets = 0
        self.encoded_bytes = 0
        self.payload_bytes = 0
        self.channel_bytes: Counter[str] = Counter()

    def record(self, packet: Packet, nbytes: int) -> None:
        self.encoded_bytes += nbytes
        if isinstance(packet, TelemetryPacket):
            self.telemetry_packets += 1
            for name in packet.values:
                size = self._sizes[name]
                self.payload_bytes += size
                self.channel_bytes[name] += size
        else:
            self.status_packets += 1

    def window(self, now: float) -> tuple[dict[str, Any], dict[str, float]] | None:
        """(totals, per-channel bits/s) since the previous call, then reset."""
        t0, self._t0 = self._t0, now
        if t0 is None or now <= t0:
            self._reset()
            return None
        dt = now - t0
        packets = self.telemetry_packets + self.status_packets
        totals: dict[str, Any] = {
            "packets_per_s": round(packets / dt, 3),
            "telemetry_packets_per_s": round(self.telemetry_packets / dt, 3),
            "status_packets_per_s": round(self.status_packets / dt, 3),
            "encoded_bytes_per_s": round(self.encoded_bytes / dt, 2),
            "encoded_bits_per_s": round(8 * self.encoded_bytes / dt, 1),
            "payload_bytes_per_s": round(self.payload_bytes / dt, 2),
            "payload_bits_per_s": round(8 * self.payload_bytes / dt, 1),
            "serial_framed_bits_per_s": round(8 * (self.encoded_bytes + SERIAL_FRAMING_BYTES * packets) / dt, 1),
        }
        if self.encoded_bytes:
            totals["overhead_pct"] = round(100.0 * (self.encoded_bytes - self.payload_bytes) / self.encoded_bytes, 2)
        if packets:
            totals["avg_packet_bytes"] = round(self.encoded_bytes / packets, 2)
        if self.telemetry_packets:
            totals["avg_payload_bytes_per_telemetry_packet"] = round(self.payload_bytes / self.telemetry_packets, 2)
        per_channel = {name: round(8 * self.channel_bytes[name] / dt, 1) for name in self.channels}
        self._reset()
        return totals, per_channel
