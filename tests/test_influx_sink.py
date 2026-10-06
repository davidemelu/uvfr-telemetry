"""InfluxDB sink: batching, retries, outage buffering, and a real round trip."""

from __future__ import annotations

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from common.config import ConfigError
from pit_receiver.influx_sink import InfluxConfig, InfluxSink
from pit_receiver.points import Point


class FakeInflux:
    """Minimal /api/v2/write endpoint recording what it receives."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: list[int] = []  # queued status codes; default 204
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                      "lines": body.decode().splitlines()})
                code = fake.responses.pop(0) if fake.responses else 204
                self.send_response(code)
                self.end_headers()
                if code != 204:
                    self.wfile.write(b'{"message":"nope"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def lines(self) -> list[str]:
        return [line for r in self.requests for line in r["lines"]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeInflux()
    yield f
    f.close()


def cfg(url: str, **kw) -> InfluxConfig:
    return InfluxConfig(url=url, org="uvfr", bucket="telemetry", token="secret", **kw)


def pts(n: int, start: int = 0) -> list[Point]:
    return [Point("vehicle", {"rpm": float(i)}, {"car": "t"}, 1_700_000_000_000_000_000 + i) for i in range(start, start + n)]


def test_config_validation():
    with pytest.raises(ConfigError):
        InfluxConfig(url="localhost:8086", org="o", bucket="b", token="t")
    with pytest.raises(ConfigError, match="token"):
        InfluxConfig(url="http://x", org="o", bucket="b", token="")


def test_batches_are_gzipped_authenticated_and_ordered(fake):
    sink = InfluxSink(cfg(fake.url, batch_size=100), start_thread=False)
    sink.write(pts(250))
    while sink.buffered:
        assert sink.flush_once()
    assert len(fake.requests) == 3
    assert fake.requests[0]["auth"] == "Token secret"
    assert "org=uvfr" in fake.requests[0]["path"] and "bucket=telemetry" in fake.requests[0]["path"]
    assert "precision=ns" in fake.requests[0]["path"]
    assert fake.lines == [p.to_line() for p in pts(250)]
    assert sink.written == 250 and sink.connected


def test_server_errors_keep_points_for_retry(fake):
    sink = InfluxSink(cfg(fake.url, batch_size=10), start_thread=False)
    fake.responses = [503, 500]
    sink.write(pts(10))
    assert not sink.flush_once()
    assert not sink.flush_once()
    assert sink.buffered == 10 and sink.write_errors == 2 and not sink.connected
    assert sink.flush_once()
    assert sink.written == 10 and sink.buffered == 0


def test_malformed_batches_are_not_retried_forever(fake):
    sink = InfluxSink(cfg(fake.url, batch_size=10), start_thread=False)
    fake.responses = [400]
    sink.write(pts(10))
    assert sink.flush_once()
    assert sink.rejected == 10 and sink.buffered == 0


def test_influx_down_keeps_points_buffered():
    probe = FakeInflux()
    url = probe.url
    probe.close()  # nothing listening: InfluxDB is down
    sink = InfluxSink(cfg(url, batch_size=50), start_thread=False)
    sink.write(pts(120))
    assert not sink.flush_once()
    assert sink.buffered == 120 and "cannot reach" in sink.last_error


def test_buffer_is_bounded_and_drops_oldest(fake):
    sink = InfluxSink(cfg(fake.url, batch_size=10, max_buffer_points=100), start_thread=False)
    sink.write(pts(150))
    assert sink.buffered == 100 and sink.dropped == 50
    while sink.buffered:
        sink.flush_once()
    assert fake.lines[0] == pts(1, start=50)[0].to_line()  # the oldest 50 were dropped


def test_background_thread_flushes_and_close_drains(fake):
    sink = InfluxSink(cfg(fake.url, batch_size=1000, flush_interval_s=0.05))
    sink.write(pts(5))
    deadline = time.monotonic() + 2
    while sink.written < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sink.written == 5
    sink.write(pts(3, start=5))
    sink.close()
    assert sink.written == 8


def test_health_fields(fake):
    sink = InfluxSink(cfg(fake.url), start_thread=False)
    assert set(sink.health()) == {"influx_connected", "influx_written", "influx_buffered", "influx_dropped",
                                  "influx_rejected", "influx_write_errors"}


# ------------------------------------------------------------ real InfluxDB
@pytest.mark.influx
def test_real_influxdb_round_trip(influx):
    sink = InfluxSink(InfluxConfig(url=influx["url"], org=influx["org"], bucket=influx["bucket"], token=influx["token"]),
                      start_thread=False)
    now_ns = time.time_ns()
    points = [Point("vehicle", {"rpm": 9000.0 + i, "coolant_temperature": 88.5}, {"car": "pytest"}, now_ns - i * 100_000_000)
              for i in range(50)]
    points.append(Point("alert", {"severity": "CRITICAL", "message": 'Coolant "high"', "active": True}, {"car": "pytest", "rule": "x", "channel": "-"}, now_ns))
    sink.write(points)
    while sink.buffered:
        assert sink.flush_once(), sink.last_error
    rows = influx["query"](f'from(bucket: "{influx["bucket"]}") |> range(start: -5m) '
                           f'|> filter(fn: (r) => r.car == "pytest") |> group(columns: ["_field"]) |> count()')
    counts = {r["_field"]: int(r["_value"]) for r in rows if r.get("_field")}
    assert counts.get("rpm") == 50 and counts.get("coolant_temperature") == 50
    assert counts.get("message") == 1
