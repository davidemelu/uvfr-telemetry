"""Transmit statistics: what the telemetry stream actually costs on the link.

Two numbers matter for sizing a radio link:
  raw payload  bytes of channel values only (what we want to move)
  encoded      every byte of every packet: header, bitmap, values, CRC,
               plus STATUS packets (what the radio must actually carry)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from common.protocol import OVERHEAD_BYTES, ChannelLayout, Missing


@dataclass
class TxTotals:
    packets: Counter = field(default_factory=Counter)  # by packet type
    bytes: Counter = field(default_factory=Counter)  # by packet type
    value_bytes: Counter = field(default_factory=Counter)  # by channel
    samples: Counter = field(default_factory=Counter)  # by channel
    not_sent_as_value: Counter = field(default_factory=Counter)  # by channel: STALE / NO DATA


class TxStats:
    def __init__(self, layout: ChannelLayout) -> None:
        self.layout = layout
        self.totals = TxTotals()
        self._window_start: TxTotals = TxTotals()
        self._window_t: float | None = None

    def record_telemetry(self, packet_len: int, values: dict[int, float | Missing]) -> None:
        t = self.totals
        t.packets["telemetry"] += 1
        t.bytes["telemetry"] += packet_len
        for index, value in values.items():
            spec = self.layout.channels[index]
            t.value_bytes[spec.name] += spec.wire.size
            t.samples[spec.name] += 1
            if isinstance(value, Missing):
                t.not_sent_as_value[spec.name] += 1

    def record_status(self, packet_len: int) -> None:
        self.totals.packets["status"] += 1
        self.totals.bytes["status"] += packet_len

    @property
    def packets_sent(self) -> int:
        return sum(self.totals.packets.values())

    def window(self, now: float) -> dict[str, Any]:
        """Rates since the previous call, then start a new window."""
        start_t = self._window_t
        self._window_t = now
        prev, cur = self._window_start, self.totals
        self._window_start = TxTotals(
            Counter(cur.packets), Counter(cur.bytes), Counter(cur.value_bytes), Counter(cur.samples),
            Counter(cur.not_sent_as_value),
        )
        if start_t is None or now <= start_t:
            return {}
        dt = now - start_t

        def rate(c_now: Counter, c_prev: Counter, key: str) -> float:
            return (c_now[key] - c_prev[key]) / dt

        packets = {k: rate(cur.packets, prev.packets, k) for k in ("telemetry", "status")}
        sizes = {k: rate(cur.bytes, prev.bytes, k) for k in ("telemetry", "status")}
        channel_bytes = {c.name: rate(cur.value_bytes, prev.value_bytes, c.name) for c in self.layout.channels}
        channel_rates = {c.name: rate(cur.samples, prev.samples, c.name) for c in self.layout.channels}
        total_bytes = sum(sizes.values())
        payload = sum(channel_bytes.values())
        return {
            "window_s": round(dt, 3),
            "packets_per_s": {**packets, "total": sum(packets.values())},
            "bytes_per_s": {**sizes, "total": total_bytes},
            "bits_per_s": total_bytes * 8,
            "payload_bytes_per_s": payload,
            "payload_bits_per_s": payload * 8,
            "overhead_bytes_per_s": total_bytes - payload,
            "channel_value_bytes_per_s": channel_bytes,
            "channel_samples_per_s": channel_rates,
            "fixed_overhead_bytes_per_packet": OVERHEAD_BYTES,
        }
