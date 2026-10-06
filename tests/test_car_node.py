"""Car node: CAN in, scheduled telemetry out, without depending on the pit side."""

from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter

import can
import pytest

from car_node.node import CarNode
from common.config import REPO_ROOT
from common.dbc import encode_physical
from common.protocol import ChannelLayout, Codec, Missing, StatusPacket, TelemetryPacket
from common.transport import UdpTransport
from tests.test_transport import FakeClock, MemoryTransport


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


def make_node(layout, dbc):
    clock = FakeClock()
    out = MemoryTransport()
    node = CarNode(layout, dbc, out, clock=clock, session=0x42)
    return node, out, clock


def feed_all_messages(node, dbc, t, counter=0, **overrides):
    for message in dbc.messages:
        values = {s.name: float(s.minimum) for s in message.signals}
        values.update({k: v for k, v in overrides.items() if k in values})
        for s in message.signals:
            if s.name.endswith("_Counter"):
                values[s.name] = counter
        msg = can.Message(arbitration_id=message.frame_id, data=encode_physical(message, values), is_extended_id=False)
        node.on_frame(msg, now=t)


def test_scheduled_packets_carry_decoded_values(layout, dbc):
    node, out, clock = make_node(layout, dbc)
    codec = Codec(layout)
    feed_all_messages(node, dbc, clock.t, EngineSpeed=9000, CoolantTemp=88.3, BatteryVoltage=13.9)
    node.run_tick(clock.t)
    telemetry = [codec.decode(p) for p in out.sent]
    t0 = next(p for p in telemetry if isinstance(p, TelemetryPacket))
    assert t0.header.session == 0x42 and t0.header.seq == 0
    assert t0.values["rpm"] == 9000
    status = next(p for p in telemetry if isinstance(p, StatusPacket))
    assert status.config_match and status.header.seq == 1
    assert status.status.tx_packets == 2


def test_never_forwards_frames_one_for_one(layout, dbc):
    node, out, clock = make_node(layout, dbc)
    for i in range(100):  # 1 s of heavy CAN traffic between two scheduler ticks
        feed_all_messages(node, dbc, clock.t + i * 0.001, counter=i % 16)
    node.run_tick(clock.t)
    assert len(out.sent) <= 2  # one telemetry (+ one status) packet, not hundreds


def test_stale_and_missing_channels_are_flagged(layout, dbc):
    node, out, clock = make_node(layout, dbc)
    codec = Codec(layout)
    feed_all_messages(node, dbc, clock.t)
    clock.t += 1.0  # nothing new for 1 s: everything is past its stale timeout
    sent = node.run_tick(clock.t)
    packet = codec.decode(sent[0])
    assert set(packet.values.values()) == {Missing.STALE}
    assert node.unhealthy_channels(clock.t)["rpm"] == "STALE"

    node2, out2, clock2 = make_node(layout, dbc)
    packet = codec.decode(node2.run_tick(clock2.t)[0])  # no CAN at all yet
    assert set(packet.values.values()) == {Missing.NO_DATA}


def test_sequence_numbers_increment_and_wrap(layout, dbc):
    node, out, clock = make_node(layout, dbc)
    codec = Codec(layout)
    node.seq = 65534
    for _ in range(3):
        node.run_tick(clock.t)
        clock.t += 0.1
    seqs = [codec.decode(p).header.seq for p in out.sent]
    assert seqs == [65534, 65535, 0, 1]  # tick 0 sends telemetry + status


def test_rates_over_ten_seconds(layout, dbc):
    node, out, clock = make_node(layout, dbc)
    codec = Codec(layout)
    counts: Counter[str] = Counter()
    for tick in range(100):
        feed_all_messages(node, dbc, clock.t, counter=tick % 16)
        node.run_tick(clock.t)
        clock.t += 0.1
    kinds = Counter()
    for p in out.sent:
        packet = codec.decode(p)
        kinds[type(packet).__name__] += 1
        if isinstance(packet, TelemetryPacket):
            counts.update(packet.values.keys())
    assert kinds == {"TelemetryPacket": 100, "StatusPacket": 10}
    for spec in layout.channels:
        assert counts[spec.name] == spec.rate_hz * 10


def test_cli_rejects_bad_config(tmp_path, capsys):
    from car_node.node import main

    bad = tmp_path / "telemetry.yaml"
    bad.write_text("car_node:\n  dbc: dbc/simulated_uvfr.dbc\n", encoding="utf-8")
    assert main(["--config", str(bad)]) == 2
    assert "missing key" in capsys.readouterr().err


@pytest.mark.vcan
def test_car_node_on_vcan_end_to_end(can_channel, layout):
    """Fake ECU -> vcan -> car node process -> UDP, decoded here."""
    rx = UdpTransport(bind=("127.0.0.1", 0))
    port = rx.local_address[1]
    ecu = subprocess.Popen(
        [sys.executable, "-m", "simulator", "--channel", can_channel, "--duration", "12",
         "--no-control", "--quiet", "--seed", "2"],
        cwd=REPO_ROOT,
    )
    node = subprocess.Popen(
        [sys.executable, "-m", "car_node", "--channel", can_channel, "--remote", f"127.0.0.1:{port}",
         "--duration", "10", "--quiet"],
        cwd=REPO_ROOT,
    )
    codec = Codec(layout)
    try:
        packets = []
        start = time.monotonic()
        while time.monotonic() - start < 9.5:
            data = rx.recv(timeout=0.5)
            if data is not None:
                packets.append((time.monotonic() - start, codec.decode(data)))
        assert node.wait(timeout=10) == 0
    finally:
        for p in (node, ecu):
            p.kill()
        rx.close()

    window = [p for t, p in packets if 2.0 <= t < 8.0]  # skip start-up
    telemetry = [p for p in window if isinstance(p, TelemetryPacket)]
    statuses = [p for p in window if isinstance(p, StatusPacket)]
    assert len(telemetry) == pytest.approx(60, abs=3)
    assert len(statuses) == pytest.approx(6, abs=1)
    assert all(s.config_match for s in statuses)
    assert statuses[-1].status.can_fps == pytest.approx(231, abs=10)
    seqs = [p.header.seq for _, p in packets]
    assert all((b - a) % 65536 == 1 for a, b in zip(seqs, seqs[1:])), "no gaps on a perfect link"
    late = [p for t, p in packets if t > 8.0 and isinstance(p, TelemetryPacket)]
    assert late and late[-1].values["rpm"] > 1000  # car is driving by then
    assert all(not isinstance(v, Missing) for p in late for v in p.values.values() if v is not None)
