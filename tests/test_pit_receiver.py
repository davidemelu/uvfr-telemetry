"""Pit receiver: packets in, points out, link health measured from what arrives."""

from __future__ import annotations

import random
import subprocess
import sys
import time

import pytest

from common.config import REPO_ROOT, load_yaml
from common.protocol import CarStatus, ChannelLayout, Codec, Missing
from common.transport import ImpairedTransport, Impairment, UdpTransport
from pit_receiver.channels import StalenessConfig
from pit_receiver.link_stats import LinkThresholds
from pit_receiver.receiver import PitReceiver, scenario_label
from pit_receiver.sinks import JsonlSink, MemorySink
from pit_receiver.status import Status
from tests.test_transport import FakeClock, MemoryTransport


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


def make_rx(layout, sinks=None):
    alerts = load_yaml("config/alerts.yaml")
    clock = FakeClock()
    rx = PitReceiver(
        layout,
        LinkThresholds.from_config(alerts["link"]),
        StalenessConfig.from_config(alerts["channel_staleness"]),
        car_id="test-car",
        sinks=sinks if sinks is not None else [MemorySink()],
        wall=lambda: 1_700_000_000.0 + clock.t,
        mono=clock,
    )
    return rx, clock


def idx(layout, name):
    return layout.by_name(name).index


def test_telemetry_becomes_vehicle_points(layout):
    rx, clock = make_rx(layout)
    sink = rx.sinks[0]
    codec = Codec(layout)
    data = codec.encode_telemetry(5, 0, 1000, {idx(layout, "rpm"): 9123.0, idx(layout, "coolant_temperature"): 88.4,
                                               idx(layout, "oil_pressure"): Missing.NO_DATA})
    rx.on_datagram(data)
    (point,) = sink.by_measurement("vehicle")
    assert point.tags == {"car": "test-car"}
    assert point.fields == pytest.approx({"rpm": 9123.0, "coolant_temperature": 88.4})  # NO DATA is not written
    assert point.time_ns is not None


def test_status_becomes_car_status_points(layout):
    rx, clock = make_rx(layout)
    codec = Codec(layout)
    ages = [10.0, 0.0, 20.0, 480.0, 60.0, 80.0, 900.0]
    st = CarStatus(layout.config_hash, 11, 2310, 231, 2, 0, 3, tuple(ages))
    rx.on_datagram(codec.encode_status(5, 0, 1000, st))
    (point,) = rx.sinks[0].by_measurement("car_status")
    f = point.fields
    assert f["config_match"] is True and f["can_fps"] == 231 and f["cpu_pct"] == 3
    assert f["can_age_SIM_ENGINE_TEMPS_ms"] == 480.0
    assert f["can_age_max_ms"] == 480.0  # SIM_STATUS (lab only, 1 Hz) does not count


def test_health_points(layout):
    rx, clock = make_rx(layout)
    codec = Codec(layout)
    for seq in range(30):
        values = {i: 1.0 for i in range(len(layout.channels))}
        values[idx(layout, "sim_scenario")] = 1.0  # overheating
        rx.on_datagram(codec.encode_telemetry(5, seq, seq * 100, values))
        clock.t += 0.1
    snap, points = rx.health()
    assert snap.status is Status.NORMAL
    kinds = {p.measurement for p in points}
    assert kinds == {"link", "channel_state", "sim"}
    states = [p for p in points if p.measurement == "channel_state"]
    assert len(states) == len(layout.channels)
    assert all(p.fields["status"] == "NORMAL" for p in states)
    sim = next(p for p in points if p.measurement == "sim")
    assert sim.fields["scenario"] == "overheating"


def test_corrupt_and_duplicate_packets_are_not_written(layout):
    rx, clock = make_rx(layout)
    codec = Codec(layout)
    good = codec.encode_telemetry(5, 0, 0, {0: 9000.0})
    bad = bytearray(good)
    bad[12] ^= 0x10
    rx.on_datagram(good)
    rx.on_datagram(good)  # duplicate
    rx.on_datagram(bytes(bad))
    rx.on_datagram(b"garbage")
    assert len(rx.sinks[0].by_measurement("vehicle")) == 1
    snap, _ = rx.health()
    assert snap.duplicates == 1
    assert snap.rejected == {"bad_crc": 1, "too_short": 1}


def test_link_loss_after_an_impaired_link_matches_configuration(layout):
    """Car-side encoder -> 5% loss radio -> pit: the pit measures about 5%."""
    rx, clock = make_rx(layout, sinks=[])
    codec = Codec(layout)

    class ToPit(MemoryTransport):
        def send(self, packet):
            rx.on_datagram(packet)
            return True

    radio = ImpairedTransport(ToPit(), Impairment(loss_pct=5.0), rng=random.Random(9), clock=clock, threaded=False)
    for seq in range(5000):
        radio.send(codec.encode_telemetry(5, seq & 0xFFFF, seq * 100, {0: 9000.0}))
        radio.pump()
        clock.t += 0.1
    snap = rx.link.snapshot(clock.t)
    assert snap.loss_pct == pytest.approx(5.0, abs=0.8)
    # Every packet the radio dropped shows up as missing (bar any lost at the
    # very end, which no later packet can reveal).
    assert radio.link.dropped_loss - 3 <= snap.packets_missing <= radio.link.dropped_loss


def test_outage_makes_telemetry_lost_then_recovers(layout):
    rx, clock = make_rx(layout, sinks=[])
    codec = Codec(layout)
    seq = 0
    for _ in range(20):
        rx.on_datagram(codec.encode_telemetry(5, seq, seq * 100, {0: 9000.0}))
        seq += 1
        clock.t += 0.1
    clock.t += 3.0  # 3 s outage: the car keeps sending, nothing arrives
    seq += 30
    snap, _ = rx.health()
    assert snap.status is Status.CRITICAL
    assert rx.channels.view("rpm", clock.t).status is Status.STALE
    for _ in range(20):
        rx.on_datagram(codec.encode_telemetry(5, seq, seq * 100, {0: 9000.0}))
        seq += 1
        clock.t += 0.1
    snap, _ = rx.health()
    assert snap.packets_missing == 30
    assert snap.status is Status.CRITICAL  # 30 of the last 70 packets lost
    assert rx.channels.view("rpm", clock.t).status is Status.NORMAL


def test_jsonl_sink(tmp_path, layout):
    path = tmp_path / "points.jsonl"
    rx, clock = make_rx(layout, sinks=[JsonlSink(path)])
    rx.on_datagram(Codec(layout).encode_telemetry(5, 0, 0, {0: 9000.0}))
    rx.close()
    assert '"rpm": 9000.0' in path.read_text(encoding="utf-8")


def test_a_failing_sink_does_not_stop_the_receiver(layout, capsys):
    class Broken(MemorySink):
        name = "broken"

        def write(self, points):
            raise RuntimeError("database down")

    good = MemorySink()
    rx, clock = make_rx(layout, sinks=[Broken(), good])
    rx.on_datagram(Codec(layout).encode_telemetry(5, 0, 0, {0: 9000.0}))
    assert len(good.points) == 1
    assert "database down" in capsys.readouterr().err


def test_scenario_labels():
    assert scenario_label(0) == "normal"
    assert scenario_label(1 | 64) == "overheating+intermittent_can"


@pytest.mark.vcan
def test_full_chain_over_impaired_radio(can_channel, tmp_path):
    """Fake ECU -> vcan -> car node -> link_sim (lora_marginal) -> pit receiver."""
    probe = UdpTransport(bind=("127.0.0.1", 0))
    pit_port = probe.local_address[1]
    probe.close()
    probe = UdpTransport(bind=("127.0.0.1", 0))
    radio_port = probe.local_address[1]
    probe.close()
    points = tmp_path / "points.jsonl"
    py = sys.executable
    procs = [
        subprocess.Popen([py, "-m", "simulator", "--channel", can_channel, "--duration", "24", "--no-control",
                          "--quiet", "--seed", "4"], cwd=REPO_ROOT),
        subprocess.Popen([py, "-m", "link_sim", "--profile", "lora_marginal", "--listen", f"127.0.0.1:{radio_port}",
                          "--forward", f"127.0.0.1:{pit_port}", "--control-port", "0", "--seed", "4",
                          "--duration", "23", "--quiet"], cwd=REPO_ROOT),
        subprocess.Popen([py, "-m", "pit_receiver", "--listen", f"127.0.0.1:{pit_port}", "--jsonl", str(points),
                          "--duration", "22", "--quiet"], cwd=REPO_ROOT),
    ]
    time.sleep(0.5)
    procs.append(subprocess.Popen([py, "-m", "car_node", "--channel", can_channel, "--remote",
                                   f"127.0.0.1:{radio_port}", "--duration", "21", "--quiet"], cwd=REPO_ROOT))
    try:
        for p in procs:
            assert p.wait(timeout=40) == 0
    finally:
        for p in procs:
            p.kill()

    import json

    rows = [json.loads(line) for line in points.read_text(encoding="utf-8").splitlines()]
    link = [r["fields"] for r in rows if r["measurement"] == "link"]
    final = link[-1]
    assert 1.5 < final["loss_pct"] < 10.0, final  # lora_marginal is configured at 5%
    assert final["packets_received"] > 180
    vehicle = [r["fields"] for r in rows if r["measurement"] == "vehicle"]
    assert len(vehicle) > 180
    assert max(v.get("rpm", 0) for v in vehicle) > 6000  # car drove away from the pits
    assert any(r["measurement"] == "car_status" and r["fields"]["config_match"] for r in rows)
