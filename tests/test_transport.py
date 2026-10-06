"""Transports: UDP, serial (loopback) and the impaired-link simulator."""

from __future__ import annotations

import random
import time

import pytest

from common.config import ConfigError
from common.transport import ImpairedTransport, Impairment, Transport, UdpTransport, parse_hostport, transport_from_config


class MemoryTransport(Transport):
    """Collects sent packets; used as the inner end of an impaired link."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[bytes] = []

    def describe(self) -> str:
        return "memory"

    def send(self, packet: bytes) -> bool:
        self.sent.append(packet)
        self.stats.packets_sent += 1
        return True

    def recv(self, timeout=None):
        return None


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def impaired(**kwargs):
    clock = FakeClock()
    inner = MemoryTransport()
    link = ImpairedTransport(inner, Impairment(**kwargs), rng=random.Random(42), clock=clock, threaded=False)
    return link, inner, clock


# ---------------------------------------------------------------------- UDP
def test_parse_hostport():
    assert parse_hostport("127.0.0.1:47001") == ("127.0.0.1", 47001)
    with pytest.raises(ValueError):
        parse_hostport("47001")


def test_udp_roundtrip():
    rx = UdpTransport(bind=("127.0.0.1", 0))
    tx = UdpTransport(remote=rx.local_address)
    try:
        for i in range(20):
            assert tx.send(bytes([i]) * 30)
        got = [rx.recv(timeout=1.0) for _ in range(20)]
        assert got == [bytes([i]) * 30 for i in range(20)]
        assert rx.recv(timeout=0.05) is None
        assert tx.stats.packets_sent == 20 and rx.stats.bytes_received == 600
    finally:
        tx.close()
        rx.close()


def test_udp_send_with_nobody_listening_does_not_raise():
    probe = UdpTransport(bind=("127.0.0.1", 0))
    addr = probe.local_address
    probe.close()
    tx = UdpTransport(remote=addr)
    try:
        for _ in range(5):
            tx.send(b"x" * 10)  # must never raise: a dead pit must not stop the car node
    finally:
        tx.close()


def test_transport_factory():
    t = transport_from_config({"type": "udp", "remote": "127.0.0.1:9"}, "t", role="sender")
    t.close()
    with pytest.raises(ConfigError):
        transport_from_config({"type": "carrier_pigeon"}, "t", role="sender")
    with pytest.raises(ConfigError):
        transport_from_config({"type": "udp", "listen": "127.0.0.1:9"}, "t", role="sender")


# ------------------------------------------------------------------- serial
def test_serial_transport_over_loopback():
    serial = pytest.importorskip("serial")
    from common.transport.serial_transport import SerialTransport

    port = serial.serial_for_url("loop://", timeout=0)
    link = SerialTransport("loop://", 57600, serial_obj=port)
    packets = [b"\x00\x01\x02", bytes(range(36)), b"\x00" * 10, b"telemetry"]
    for p in packets:
        assert link.send(p)
    assert [link.recv(timeout=1.0) for _ in packets] == packets
    assert link.recv(timeout=0.05) is None
    assert link.stats.bytes_sent == sum(len(p) + 2 for p in packets)  # COBS byte + delimiter
    link.close()


def test_serial_transport_survives_line_noise():
    serial = pytest.importorskip("serial")
    from common.protocol.framing import frame
    from common.transport.serial_transport import SerialTransport

    port = serial.serial_for_url("loop://", timeout=0)
    link = SerialTransport("loop://", 57600, serial_obj=port)
    port.write(b"\x07\x99\x98")  # noise without a delimiter
    port.write(b"\x00" + frame(b"good"))
    assert link.recv(timeout=1.0) == b"good"
    link.close()


# --------------------------------------------------------------- impairment
def test_perfect_link_delivers_everything_immediately():
    link, inner, clock = impaired()
    for i in range(100):
        link.send(bytes([i]))
        assert link.pump() == 1
    assert inner.sent == [bytes([i]) for i in range(100)]


def test_undelivered_packets_are_bounded_by_the_queue():
    link, inner, clock = impaired(max_queue_packets=10)
    for _ in range(50):
        link.send(b"p")  # nothing pumps: the radio buffer fills
    assert link.queue_length == 10
    assert link.link.dropped_queue == 40


@pytest.mark.parametrize("loss", [1.0, 5.0, 10.0, 50.0])
def test_packet_loss_rate(loss):
    link, inner, _ = impaired(loss_pct=loss)
    n = 20000
    for _ in range(n):
        link.send(b"p")
        link.pump()
    measured = 100.0 * (1 - len(inner.sent) / n)
    assert measured == pytest.approx(loss, abs=max(0.5, loss * 0.15))
    assert link.link.dropped_loss == n - len(inner.sent)


def test_latency_and_jitter():
    link, inner, clock = impaired(latency_ms=200, jitter_ms=50, allow_reorder=True)
    link.send(b"a")
    clock.t += 0.149
    assert link.pump() == 0  # earliest possible arrival is 150 ms
    clock.t += 0.102
    assert link.pump() == 1  # latest possible is 250 ms


def test_jitter_without_reordering_keeps_order():
    link, inner, clock = impaired(latency_ms=50, jitter_ms=40)
    for i in range(200):
        link.send(bytes([i]))
        clock.t += 0.005
        link.pump()
    clock.t += 1.0
    link.pump()
    assert inner.sent == [bytes([i]) for i in range(200)]


def test_jitter_with_reordering_allowed_can_reorder():
    link, inner, clock = impaired(latency_ms=50, jitter_ms=40, allow_reorder=True)
    for i in range(200):
        link.send(bytes([i]))
        clock.t += 0.005
        link.pump()
    clock.t += 1.0
    link.pump()
    assert sorted(inner.sent) == [bytes([i]) for i in range(200)]
    assert inner.sent != sorted(inner.sent)


def test_scheduled_outage_drops_everything_inside_the_window():
    link, inner, clock = impaired(outage_every_s=10, outage_duration_s=2)
    delivered_in_outage = 0
    for step in range(1000):  # 10 s at 100 Hz
        before = len(inner.sent)
        in_outage = link.in_outage()
        link.send(b"x")
        link.pump()
        if in_outage:
            delivered_in_outage += len(inner.sent) - before
        clock.t += 0.01
    assert delivered_in_outage == 0
    assert link.link.dropped_outage == pytest.approx(200, abs=2)


def test_forced_outage():
    link, inner, clock = impaired()
    link.force_outage(1.5)
    link.send(b"lost")
    clock.t += 1.6
    link.send(b"kept")
    link.pump()
    assert inner.sent == [b"kept"]


def test_corruption_is_caught_by_the_protocol_crc(dbc):
    from common.protocol import ChannelLayout, Codec, DecodeError

    layout = ChannelLayout.load("config/channels.yaml", dbc)
    codec = Codec(layout)
    link, inner, _ = impaired(corrupt_pct=30)
    for seq in range(1000):
        link.send(codec.encode_telemetry(1, seq, seq, {0: 9000.0, 5: 88.0}))
        link.pump()
    bad = 0
    for packet in inner.sent:
        try:
            codec.decode(packet)
        except DecodeError:
            bad += 1
    assert bad == link.link.corrupted
    assert 200 < bad < 400


def test_bandwidth_cap_queues_then_tail_drops():
    # 100-byte packets at 100/s = 80 kbit/s offered into a 40 kbit/s link
    link, inner, clock = impaired(bandwidth_bps=40_000, max_queue_packets=20)
    for _ in range(100):
        link.send(b"x" * 100)
        clock.t += 0.01
        link.pump()
    clock.t += 5
    link.pump()
    assert link.link.dropped_queue > 30
    assert link.link.queue_peak == 20
    # what got through was paced at the link rate
    assert len(inner.sent) * 100 * 8 <= 40_000 * (1.0 + 0.5) + 800 * 20


def test_per_packet_airtime_overhead():
    link, inner, clock = impaired(bandwidth_bps=8000, packet_overhead_ms=20)
    assert link.airtime_s(25) == pytest.approx(25 * 8 / 8000 + 0.020)
    link.send(b"x" * 25)
    clock.t += 0.044
    assert link.pump() == 0
    clock.t += 0.002
    assert link.pump() == 1


def test_switching_profile_retimes_the_queue():
    link, inner, clock = impaired(bandwidth_bps=800, max_queue_packets=50)  # 10 packets = 1 s each
    for _ in range(10):
        link.send(b"x" * 100)
    assert link.pump() == 0
    link.set_impairment(Impairment())  # suddenly a perfect link
    assert link.pump() == 10  # delivered now, not 10 s later


def test_queued_packets_are_lost_during_an_outage():
    link, inner, clock = impaired(latency_ms=500)
    for _ in range(5):
        link.send(b"in flight")
    link.force_outage(1.0)
    clock.t += 0.6
    link.pump()
    assert inner.sent == []
    assert link.link.dropped_outage == 5


def test_runtime_impairment_change():
    link, inner, clock = impaired()
    link.set_impairment(Impairment(loss_pct=100))
    link.send(b"gone")
    link.set_impairment(Impairment())
    link.send(b"here")
    link.pump()
    assert inner.sent == [b"here"]


def test_threaded_delivery_with_real_clock():
    inner = MemoryTransport()
    link = ImpairedTransport(inner, Impairment(latency_ms=30), rng=random.Random(1))
    try:
        link.send(b"a")
        assert inner.sent == []
        deadline = time.monotonic() + 1
        while not inner.sent and time.monotonic() < deadline:
            time.sleep(0.005)
        assert inner.sent == [b"a"]
    finally:
        link.close()


@pytest.mark.parametrize(
    "bad",
    [{"loss_pct": 101}, {"latency_ms": -1}, {"max_queue_packets": 0}, {"outage_every_s": 2, "outage_duration_s": 3}, {"lag": 1}],
)
def test_invalid_impairment_rejected(bad):
    with pytest.raises(ConfigError):
        Impairment.from_dict(bad)
