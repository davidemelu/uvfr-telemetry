"""InfluxDB 2 sink: batched line-protocol writes over HTTP, resilient to outages.

Points are buffered and written by a background thread every flush_interval_s
(or sooner when a batch fills). If InfluxDB is unreachable or slow, points
stay buffered and are retried with backoff, so telemetry captured during a
database outage is written once it comes back. The buffer is bounded: when
full, the oldest points are dropped and counted, so a long outage can never
exhaust memory. Nothing here can block or crash the receive loop.

Standard library only (urllib), so the pit laptop needs no extra packages.
"""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from typing import Any

from common.config import ConfigError
from pit_receiver.points import Point
from pit_receiver.sinks import Sink


@dataclass(frozen=True)
class InfluxConfig:
    url: str
    org: str
    bucket: str
    token: str
    batch_size: int = 1000
    flush_interval_s: float = 0.5
    max_buffer_points: int = 200_000
    timeout_s: float = 3.0
    max_backoff_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"influxdb url must start with http:// or https://, got {self.url!r}")
        for name in ("org", "bucket", "token"):
            if not getattr(self, name):
                raise ConfigError(f"influxdb {name} is empty (check .env)")
        if self.batch_size < 1 or self.max_buffer_points < self.batch_size:
            raise ConfigError("influxdb: need 1 <= batch_size <= max_buffer_points")


class InfluxSink(Sink):
    name = "influxdb"

    def __init__(self, cfg: InfluxConfig, start_thread: bool = True) -> None:
        self.cfg = cfg
        query = urllib.parse.urlencode({"org": cfg.org, "bucket": cfg.bucket, "precision": "ns"})
        self._write_url = f"{cfg.url.rstrip('/')}/api/v2/write?{query}"
        self._buf: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0
        self.rejected = 0  # points InfluxDB refused as malformed (not retried)
        self.write_errors = 0
        self.last_error = ""
        self.connected = False
        self._backoff = 0.0
        self._thread: threading.Thread | None = None
        if start_thread:
            self._thread = threading.Thread(target=self._run, name="influx-writer", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------- sink API
    def write(self, points: list[Point]) -> None:
        lines = [line for line in (p.to_line() for p in points) if line]
        if not lines:
            return
        with self._lock:
            self._buf.extend(lines)
            overflow = len(self._buf) - self.cfg.max_buffer_points
            for _ in range(max(0, overflow)):
                self._buf.popleft()
            if overflow > 0:
                self.dropped += overflow
            full = len(self._buf) >= self.cfg.batch_size
        if full:
            self._wake.set()

    def flush(self) -> None:
        self._wake.set()

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def health(self) -> dict[str, Any]:
        return {
            "influx_connected": self.connected,
            "influx_written": self.written,
            "influx_buffered": self.buffered,
            "influx_dropped": self.dropped,
            "influx_rejected": self.rejected,
            "influx_write_errors": self.write_errors,
        }

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop the writer, trying to flush what is buffered for up to timeout_s."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s + 1)
        deadline = time.monotonic() + timeout_s
        while self._buf and time.monotonic() < deadline:
            if not self.flush_once():
                break

    # --------------------------------------------------------------- writer
    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=max(self.cfg.flush_interval_s, self._backoff))
            self._wake.clear()
            if self._stop.is_set():
                return
            while self._buf and not self._stop.is_set():
                if not self.flush_once():
                    break
                if len(self._buf) < self.cfg.batch_size:
                    break

    def flush_once(self) -> bool:
        """Write one batch. Returns True on success (or nothing to do)."""
        with self._lock:
            n = min(len(self._buf), self.cfg.batch_size)
            batch = [self._buf.popleft() for _ in range(n)]
        if not batch:
            return True
        status, detail = self._post("\n".join(batch).encode("utf-8"))
        if status == 204:
            self.written += len(batch)
            self.connected = True
            self._backoff = 0.0
            return True
        if status in (400, 413, 422):
            # Malformed or too large: retrying will not help. Count and move on.
            self.rejected += len(batch)
            self.write_errors += 1
            self.last_error = f"HTTP {status}: {detail}"
            return True
        # Network error, auth problem, 429 or 5xx: keep the points and back off.
        with self._lock:
            self._buf.extendleft(reversed(batch))
            overflow = len(self._buf) - self.cfg.max_buffer_points
            for _ in range(max(0, overflow)):
                self._buf.popleft()
            if overflow > 0:
                self.dropped += overflow
        self.write_errors += 1
        self.connected = False
        self.last_error = f"HTTP {status}: {detail}" if status else detail
        self._backoff = min(self.cfg.max_backoff_s, max(0.5, self._backoff * 2))
        return False

    def _post(self, body: bytes) -> tuple[int, str]:
        req = urllib.request.Request(
            self._write_url,
            data=gzip.compress(body, compresslevel=5),
            method="POST",
            headers={
                "Authorization": f"Token {self.cfg.token}",
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(300).decode("utf-8", "replace")
            except OSError:
                detail = exc.reason
            return exc.code, str(detail)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return 0, f"cannot reach InfluxDB: {getattr(exc, 'reason', exc)}"
