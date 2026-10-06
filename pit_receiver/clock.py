"""Map car timestamps onto the pit's clock.

Packets carry the car node's time (ms since it started), not wall-clock time:
the Raspberry Pi has no real-time clock and may not have NTP at the track, so
its wall clock cannot be trusted.

offset = min(arrival_time - source_time) over a sliding window. A packet can
never arrive before it was sent, so the least-delayed packet gives the best
estimate of the offset. Each sample is then timestamped at
source_time + offset: when it was measured, not when it happened to arrive.
Radio jitter therefore does not distort the plots, and the window lets the
estimate follow slow drift between the two clocks.

What is left over (arrival - mapped time) is the delay above the best case,
which measures jitter. Absolute one-way latency would need synchronised clocks
(for example GPS time on the car) and is not claimed here.
"""

from __future__ import annotations

from collections import deque

U32 = 1 << 32


class SourceClock:
    def __init__(self, window_s: float = 30.0) -> None:
        self.window_s = window_s
        self.reset()

    def reset(self) -> None:
        self._mins: deque[tuple[float, float]] = deque()  # (arrival, offset), offsets increasing
        self._wraps = 0
        self._last_raw: int | None = None
        self.last_excess_delay_s = 0.0
        self.max_excess_delay_s = 0.0

    def _unwrap(self, ts_ms: int) -> int:
        if self._last_raw is not None and ts_ms < self._last_raw - U32 // 2:
            self._wraps += 1
        self._last_raw = ts_ms
        return ts_ms + self._wraps * U32

    @property
    def offset(self) -> float | None:
        return self._mins[0][1] if self._mins else None

    def observe(self, ts_ms: int, arrival: float) -> float:
        """Record a packet and return its measurement time on the pit clock."""
        source = self._unwrap(ts_ms) / 1000.0
        sample = arrival - source
        while self._mins and self._mins[-1][1] >= sample:
            self._mins.pop()
        self._mins.append((arrival, sample))
        while len(self._mins) > 1 and self._mins[0][0] < arrival - self.window_s:
            self._mins.popleft()
        mapped = source + self._mins[0][1]
        self.last_excess_delay_s = arrival - mapped
        self.max_excess_delay_s = max(self.max_excess_delay_s, self.last_excess_delay_s)
        return mapped

    def take_max_excess_delay(self) -> float:
        """Largest excess delay since the previous call (for 1 Hz reporting)."""
        value, self.max_excess_delay_s = self.max_excess_delay_s, 0.0
        return value
