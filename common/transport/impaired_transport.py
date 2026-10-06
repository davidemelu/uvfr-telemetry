"""Impaired link: wraps a transport and degrades its send path like a radio.

Models, per packet:
  loss         random drops (loss_pct)
  corruption   one random bit flipped (corrupt_pct); the CRC must catch it
  latency      fixed delay (latency_ms) plus uniform jitter (+/- jitter_ms)
  bandwidth    serialisation time at bandwidth_bps; packets queue behind each
               other and are tail-dropped when max_queue_packets is exceeded,
               which is what a serial radio's buffer does when it is
               offered more than it can send
  outages      scheduled (outage_every_s / outage_duration_s) or forced at
               runtime (force_outage): everything is dropped

Delivery happens on a background thread, or by calling pump() with an
explicit clock for deterministic tests.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from common.config import ConfigError
from common.transport.base import Transport


@dataclass(frozen=True)
class Impairment:
    loss_pct: float = 0.0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    corrupt_pct: float = 0.0
    bandwidth_bps: float = 0.0  # 0 = unlimited
    max_queue_packets: int = 64
    allow_reorder: bool = False
    outage_every_s: float = 0.0  # 0 = no scheduled outages
    outage_duration_s: float = 0.0

    def __post_init__(self) -> None:
        for name in ("loss_pct", "corrupt_pct"):
            if not 0.0 <= getattr(self, name) <= 100.0:
                raise ValueError(f"{name} must be between 0 and 100")
        for name in ("latency_ms", "jitter_ms", "bandwidth_bps", "outage_every_s", "outage_duration_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.max_queue_packets < 1:
            raise ValueError("max_queue_packets must be at least 1")
        if self.outage_every_s and self.outage_duration_s >= self.outage_every_s:
            raise ValueError("outage_duration_s must be shorter than outage_every_s")

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str = "impairment") -> Impairment:
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: {exc}") from None

    def updated(self, **changes: Any) -> Impairment:
        return replace(self, **changes)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImpairmentStats:
    offered: int = 0
    delivered: int = 0
    dropped_loss: int = 0
    dropped_outage: int = 0
    dropped_queue: int = 0
    corrupted: int = 0
    queue_peak: int = 0
    bytes_offered: int = 0
    bytes_delivered: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class ImpairedTransport(Transport):
    def __init__(
        self,
        inner: Transport,
        impairment: Impairment | None = None,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        super().__init__()
        self.inner = inner
        self.impairment = impairment or Impairment()
        self.link = ImpairmentStats()
        self._rng = rng or random.Random()
        self._clock = clock
        self._t0 = clock()
        self._queue: list[tuple[float, int, bytes]] = []
        self._order = itertools.count()
        self._link_free_at = 0.0
        self._last_delivery = 0.0
        self._forced_outage_until = 0.0
        self._cond = threading.Condition()
        self._closed = False
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="impaired-link", daemon=True)
            self._thread.start()

    def describe(self) -> str:
        i = self.impairment
        return (
            f"impaired({self.inner.describe()}; loss {i.loss_pct:g}%, latency {i.latency_ms:g}"
            f"+/-{i.jitter_ms:g} ms, bw {i.bandwidth_bps:g} bps)"
        )

    # ----------------------------------------------------------- runtime control
    def set_impairment(self, impairment: Impairment) -> None:
        with self._cond:
            self.impairment = impairment
            self._cond.notify()

    def force_outage(self, duration_s: float) -> None:
        with self._cond:
            self._forced_outage_until = self._clock() + duration_s

    def in_outage(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        if now < self._forced_outage_until:
            return True
        i = self.impairment
        if i.outage_every_s > 0 and i.outage_duration_s > 0:
            phase = (now - self._t0) % i.outage_every_s
            return phase >= i.outage_every_s - i.outage_duration_s
        return False

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    # ------------------------------------------------------------------- send
    def send(self, packet: bytes) -> bool:
        with self._cond:
            now = self._clock()
            i = self.impairment
            self.link.offered += 1
            self.link.bytes_offered += len(packet)
            # From the sender's point of view the radio accepted the bytes in
            # every case below; losses are only visible at the far end.
            if self.in_outage(now):
                self.link.dropped_outage += 1
                return True
            if i.loss_pct and self._rng.random() * 100.0 < i.loss_pct:
                self.link.dropped_loss += 1
                return True
            if len(self._queue) >= i.max_queue_packets:
                self.link.dropped_queue += 1
                return True
            if i.corrupt_pct and self._rng.random() * 100.0 < i.corrupt_pct:
                data = bytearray(packet)
                bit = self._rng.randrange(len(data) * 8)
                data[bit // 8] ^= 1 << (bit % 8)
                packet = bytes(data)
                self.link.corrupted += 1

            start = max(now, self._link_free_at)
            airtime = len(packet) * 8 / i.bandwidth_bps if i.bandwidth_bps else 0.0
            self._link_free_at = start + airtime
            delay = i.latency_ms / 1000.0
            if i.jitter_ms:
                delay += self._rng.uniform(-i.jitter_ms, i.jitter_ms) / 1000.0
            deliver_at = start + airtime + max(0.0, delay)
            if not i.allow_reorder:
                deliver_at = max(deliver_at, self._last_delivery)
            self._last_delivery = max(self._last_delivery, deliver_at)
            heapq.heappush(self._queue, (deliver_at, next(self._order), packet))
            self.link.queue_peak = max(self.link.queue_peak, len(self._queue))
            self.stats.packets_sent += 1
            self.stats.bytes_sent += len(packet)
            self._cond.notify()
            return True

    def pump(self, now: float | None = None) -> int:
        """Deliver every packet that is due. Returns how many were delivered."""
        due: list[bytes] = []
        with self._cond:
            now = self._clock() if now is None else now
            while self._queue and self._queue[0][0] <= now:
                due.append(heapq.heappop(self._queue)[2])
        for packet in due:
            if self.inner.send(packet):
                self.link.delivered += 1
                self.link.bytes_delivered += len(packet)
        return len(due)

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                if self._queue:
                    wait = self._queue[0][0] - self._clock()
                else:
                    wait = None
                if wait is None or wait > 0:
                    self._cond.wait(timeout=wait if wait is None else min(wait, 0.5))
                    continue
            self.pump()

    # ------------------------------------------------------------------- recv
    def recv(self, timeout: float | None = None) -> bytes | None:
        return self.inner.recv(timeout)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.inner.close()
