"""Link health measured at the pit from sequence numbers and arrival times.

Everything here is derived from what arrives; nothing depends on the radio
reporting its own statistics, so the same code works for UDP, LoRa or RFD900x.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from common.config import build_dataclass
from common.protocol import Packet, StatusPacket
from pit_receiver.status import Status

SEQ_MOD = 1 << 16
HALF = SEQ_MOD // 2


class SequenceTracker:
    """Unwraps 16-bit sequence numbers and counts what arrived.

    Late (reordered) packets fill their gap rather than being counted lost.
    Duplicates are recognised within a window of recent sequence numbers.
    """

    def __init__(self, window: int = 4096) -> None:
        self.window = window
        self.first: int | None = None
        self.highest: int | None = None
        self.received = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.too_old = 0
        self._seen: set[int] = set()

    def add(self, seq: int) -> str:
        """Returns "new", "late", "duplicate" or "too_old"."""
        if self.highest is None:
            self.first = self.highest = seq
            self.received = 1
            self._seen = {seq}
            return "new"
        delta = (seq - self.highest + HALF) % SEQ_MOD - HALF
        unwrapped = self.highest + delta
        if unwrapped > self.highest:
            kind = "new"
            self.highest = unwrapped
            low = self.highest - self.window
            if len(self._seen) > 2 * self.window:
                self._seen = {s for s in self._seen if s > low}
        elif unwrapped in self._seen:
            self.duplicates += 1
            return "duplicate"
        elif unwrapped <= self.highest - self.window or unwrapped < self.first:
            self.too_old += 1
            return "too_old"
        else:
            kind = "late"
            self.out_of_order += 1
        self._seen.add(unwrapped)
        self.received += 1
        return kind

    @property
    def expected(self) -> int:
        return 0 if self.highest is None else self.highest - self.first + 1

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.received)

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.missing / self.expected if self.expected else 0.0

    def window_loss_pct(self, last_n: int) -> float:
        """Loss over the most recent last_n sequence numbers."""
        if self.highest is None:
            return 0.0
        n = min(last_n, self.expected)
        low = self.highest - n
        got = sum(1 for s in self._seen if s > low)
        return 100.0 * (n - got) / n


@dataclass(frozen=True)
class LinkThresholds:
    stale_after_s: float
    lost_after_s: float
    degraded_loss_pct: float
    critical_loss_pct: float

    def __post_init__(self) -> None:
        if not 0 < self.stale_after_s < self.lost_after_s:
            raise ValueError("need 0 < stale_after_s < lost_after_s")
        if not 0 < self.degraded_loss_pct < self.critical_loss_pct <= 100:
            raise ValueError("need 0 < degraded_loss_pct < critical_loss_pct <= 100")

    @classmethod
    def from_config(cls, data: Any) -> LinkThresholds:
        return build_dataclass(cls, data, "alerts.link")


@dataclass(frozen=True)
class LinkSnapshot:
    status: Status
    reason: str
    session: int | None
    sessions: int
    packets_received: int
    packets_expected: int
    packets_missing: int
    loss_pct: float
    loss_pct_window: float
    loss_window_ready: bool  # enough packets seen for the recent loss to mean something
    duplicates: int
    out_of_order: int
    rejected: dict[str, int]
    packet_rate: float
    byte_rate: float
    last_packet_age_s: float | None
    session_uptime_s: float
    availability_pct: float
    car_tx_packets: int | None
    config_mismatch: bool

    def fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status.label,
            "status_code": int(self.status),
            "reason": self.reason,
            "sessions": self.sessions,
            "packets_received": self.packets_received,
            "packets_expected": self.packets_expected,
            "packets_missing": self.packets_missing,
            "loss_pct": round(self.loss_pct, 3),
            "loss_pct_window": round(self.loss_pct_window, 3),
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "rejected_total": sum(self.rejected.values()),
            "packet_rate": round(self.packet_rate, 3),
            "byte_rate": round(self.byte_rate, 1),
            "bits_per_s": round(self.byte_rate * 8, 1),
            "session_uptime_s": round(self.session_uptime_s, 1),
            "availability_pct": round(self.availability_pct, 2),
            "config_mismatch": self.config_mismatch,
        }
        for reason, count in self.rejected.items():
            out[f"rejected_{reason}"] = count
        if self.last_packet_age_s is not None:
            out["last_packet_age_ms"] = round(self.last_packet_age_s * 1000, 1)
        if self.car_tx_packets is not None:
            out["car_tx_packets"] = self.car_tx_packets
        if self.session is not None:
            out["session"] = self.session
        return out


class LinkStats:
    def __init__(self, thresholds: LinkThresholds, rate_window_s: float = 5.0, loss_window_packets: int = 100) -> None:
        self.t = thresholds
        self.rate_window_s = rate_window_s
        self.loss_window_packets = loss_window_packets
        self.session: int | None = None
        self.sessions = 0
        self.rejected: Counter[str] = Counter()
        self._arrivals: deque[tuple[float, int]] = deque()
        self._reset_session()

    def _reset_session(self) -> None:
        self.seq = SequenceTracker()
        self.session_start: float | None = None
        self.last_arrival: float | None = None
        self.car_tx_packets: int | None = None
        self.config_mismatch = False
        self._seconds_total_with_data = 0
        self._last_bucket: int | None = None

    def _note_arrival(self, now: float, nbytes: int) -> None:
        self._arrivals.append((now, nbytes))

    def record_rejected(self, reason: str, now: float, nbytes: int) -> None:
        self.rejected[reason] += 1
        self._note_arrival(now, nbytes)

    def record_packet(self, packet: Packet, now: float, nbytes: int) -> str:
        """Returns the sequence classification, or "new_session"."""
        new_session = packet.header.session != self.session
        if new_session:
            self.session = packet.header.session
            self.sessions += 1
            self._reset_session()
            self.session_start = now
        kind = self.seq.add(packet.header.seq)
        self._note_arrival(now, nbytes)
        if kind in ("duplicate", "too_old"):
            return kind
        self.last_arrival = now
        bucket = int(now - self.session_start)
        if bucket != self._last_bucket:
            self._last_bucket = bucket
            self._seconds_total_with_data += 1
        if isinstance(packet, StatusPacket):
            self.car_tx_packets = packet.status.tx_packets
            self.config_mismatch = not packet.config_match
        return "new_session" if new_session else kind

    def snapshot(self, now: float) -> LinkSnapshot:
        while self._arrivals and self._arrivals[0][0] < now - self.rate_window_s:
            self._arrivals.popleft()
        span = self.rate_window_s
        if self.session_start is not None:
            span = max(1e-3, min(self.rate_window_s, now - self.session_start))
        rate = len(self._arrivals) / span
        byte_rate = sum(n for _, n in self._arrivals) / span
        age = None if self.last_arrival is None else max(0.0, now - self.last_arrival)
        uptime = 0.0 if self.session_start is None else now - self.session_start
        seconds = int(uptime) + 1 if self.session_start is not None else 0
        availability = 100.0 * min(1.0, self._seconds_total_with_data / seconds) if seconds else 0.0
        window_loss = self.seq.window_loss_pct(self.loss_window_packets)
        # 1 lost out of the first 10 packets is "10%" but means nothing yet.
        ready = self.seq.expected >= max(1, self.loss_window_packets // 2)
        status, reason = self._classify(age, window_loss if ready else 0.0)
        return LinkSnapshot(
            status=status,
            reason=reason,
            session=self.session,
            sessions=self.sessions,
            packets_received=self.seq.received,
            packets_expected=self.seq.expected,
            packets_missing=self.seq.missing,
            loss_pct=self.seq.loss_pct,
            loss_pct_window=window_loss,
            loss_window_ready=ready,
            duplicates=self.seq.duplicates,
            out_of_order=self.seq.out_of_order,
            rejected=dict(self.rejected),
            packet_rate=rate,
            byte_rate=byte_rate,
            last_packet_age_s=age,
            session_uptime_s=uptime,
            availability_pct=availability,
            car_tx_packets=self.car_tx_packets,
            config_mismatch=self.config_mismatch,
        )

    def _classify(self, age: float | None, window_loss: float) -> tuple[Status, str]:
        t = self.t
        if age is None:
            return Status.NO_DATA, "no telemetry received yet"
        if age >= t.lost_after_s:
            return Status.CRITICAL, f"telemetry lost: no packets for {age:.1f} s"
        if age >= t.stale_after_s:
            return Status.STALE, f"no packets for {age * 1000:.0f} ms"
        if window_loss >= t.critical_loss_pct:
            return Status.CRITICAL, f"packet loss {window_loss:.1f}%"
        if window_loss >= t.degraded_loss_pct:
            return Status.WARNING, f"packet loss {window_loss:.1f}%"
        if self.config_mismatch:
            return Status.WARNING, "car and pit use different channels.yaml"
        return Status.NORMAL, "ok"
