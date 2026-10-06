"""Per-channel rate scheduler.

The scheduler ticks at protocol.base_rate_hz. A channel at rate r is due
every base/r ticks. Channels are phase-shifted (channel i starts on tick
i mod period) so slower channels are spread across ticks instead of all
landing in the same packet, which keeps packet sizes, and so radio airtime,
even.
"""

from __future__ import annotations

from common.protocol.channels import ChannelLayout


class ChannelScheduler:
    def __init__(self, layout: ChannelLayout) -> None:
        self.layout = layout
        base = layout.base_rate_hz
        self.periods = [round(base / c.rate_hz) for c in layout.channels]
        self.phases = [c.index % p for c, p in zip(layout.channels, self.periods)]
        self.status_period = round(base / layout.status_rate_hz)

    @property
    def tick_s(self) -> float:
        return 1.0 / self.layout.base_rate_hz

    def due(self, tick: int) -> list[int]:
        return [i for i, (p, ph) in enumerate(zip(self.periods, self.phases)) if tick % p == ph]

    def status_due(self, tick: int) -> bool:
        return tick % self.status_period == 0
