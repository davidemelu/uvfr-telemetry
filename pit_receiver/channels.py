"""Latest value and liveness of every telemetry channel, as seen at the pit.

A channel is not live when:
  NO DATA  it has never arrived, or the car reports the sensor as faulty
  STALE    the car flagged it STALE (CAN timeout on the car), or the pit has
           not received it for missed_updates of its own update periods
           (radio loss or outage)

Alarm states (WARNING / CRITICAL) are layered on top by the alert engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.config import build_dataclass
from common.protocol import ChannelLayout, Missing
from pit_receiver.status import Status


@dataclass(frozen=True)
class StalenessConfig:
    missed_updates: float
    min_stale_after_s: float

    def __post_init__(self) -> None:
        if self.missed_updates < 1 or self.min_stale_after_s <= 0:
            raise ValueError("missed_updates must be >= 1 and min_stale_after_s > 0")

    @classmethod
    def from_config(cls, data: Any) -> StalenessConfig:
        return build_dataclass(cls, data, "alerts.channel_staleness")


@dataclass
class Reading:
    value: float | None = None  # last numeric value ever received
    marker: Missing | None = None  # STALE / NO DATA flag on the latest update
    updated_at: float | None = None  # pit monotonic time of the latest update
    source_time: float | None = None  # pit wall time when the car measured it


@dataclass(frozen=True)
class ChannelView:
    name: str
    status: Status
    value: float | None  # current value, None when not live
    last_value: float | None  # last known value, even if stale
    age_s: float | None


class ChannelTracker:
    def __init__(self, layout: ChannelLayout, staleness: StalenessConfig) -> None:
        self.layout = layout
        self.readings = {c.name: Reading() for c in layout.channels}
        self.stale_after = {
            c.name: max(staleness.min_stale_after_s, staleness.missed_updates / c.rate_hz) for c in layout.channels
        }

    def update(self, values: dict[str, float | Missing], source_time: float, now: float) -> None:
        for name, value in values.items():
            r = self.readings[name]
            r.updated_at = now
            r.source_time = source_time
            if isinstance(value, Missing):
                r.marker = value
            else:
                r.marker = None
                r.value = value

    def view(self, name: str, now: float) -> ChannelView:
        r = self.readings[name]
        if r.updated_at is None:
            return ChannelView(name, Status.NO_DATA, None, None, None)
        age = max(0.0, now - r.updated_at)
        if r.marker is Missing.NO_DATA:
            status = Status.NO_DATA
        elif r.marker is Missing.STALE or age > self.stale_after[name]:
            status = Status.STALE
        else:
            status = Status.NORMAL
        current = r.value if status is Status.NORMAL else None
        return ChannelView(name, status, current, r.value, age)

    def views(self, now: float) -> dict[str, ChannelView]:
        return {name: self.view(name, now) for name in self.readings}
