"""Bandwidth: stream model, LoRa airtime, and the pit-side meter."""

from __future__ import annotations

import pytest

from bandwidth.lora import LoraConfig, verdict
from bandwidth.report import StreamModel, max_channels_at, report, uniform_stream_airtime
from common.config import load_yaml
from common.protocol import ChannelLayout, Codec, Missing
from pit_receiver.bandwidth import BandwidthMeter


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


# ------------------------------------------------------------------ LoRa
@pytest.mark.parametrize(
    ("sf", "bw", "payload", "expected_ms"),
    [
        (7, 125_000, 10, 41.2),   # Semtech LoRa calculator reference values
        (12, 125_000, 10, 991.2),  # low data rate optimisation kicks in
        (7, 125_000, 25, 61.7),
        (7, 500_000, 25, 15.4),
    ],
)
def test_lora_airtime_matches_semtech(sf, bw, payload, expected_ms):
    assert LoraConfig(sf, bw).airtime_s(payload) * 1000 == pytest.approx(expected_ms, abs=0.3)


def test_lora_headline_rate():
    assert LoraConfig(7, 125_000).raw_bitrate == pytest.approx(5468.75)
    assert LoraConfig(12, 125_000).low_data_rate_opt
    assert not LoraConfig(10, 125_000).low_data_rate_opt


def test_headline_rate_overstates_usable_throughput():
    cfg = LoraConfig(7, 125_000)
    packet = 27  # a 25-byte telemetry packet plus serial framing
    usable_bps = packet * 8 / cfg.airtime_s(packet)
    assert usable_bps < 0.7 * cfg.raw_bitrate


def test_verdicts():
    assert verdict(0.3) == "fits with margin"
    assert verdict(0.7) == "tight"
    assert verdict(0.95) == "saturated"
    assert verdict(1.5) == "does not fit"


# ---------------------------------------------------------------- stream
def test_stream_model_matches_the_codec(layout):
    m = StreamModel.from_layout(layout)
    assert m.packets_per_s == 11
    assert m.payload_bytes_per_s == 121
    assert m.encoded_bytes_per_s == 275
    assert m.framed_bytes_per_s == 297


def test_uniform_streams_and_capacity():
    sizes = [uniform_stream_airtime(LoraConfig(7, 250_000), n, 10)[1] for n in (5, 10, 15, 20)]
    assert sizes == sorted(sizes)
    assert max_channels_at(LoraConfig(7, 125_000), 10) == 0  # 10 packets/s alone eats the budget
    assert max_channels_at(LoraConfig(7, 500_000), 10) > 15


def test_report_renders(layout):
    text = report(layout, None, markdown=True, modem_overhead=0)
    assert "| rpm | 10 | u16 | 2 | 20 | 160 |" in text
    assert "SF7 / 125 kHz / CR 4/5" in text
    assert "2200 bit/s" in text


# ------------------------------------------------------------ pit meter
def test_pit_bandwidth_meter(layout):
    codec = Codec(layout)
    meter = BandwidthMeter(layout)
    assert meter.window(0.0) is None  # first call only starts the window
    rpm, coolant = layout.by_name("rpm").index, layout.by_name("coolant_temperature").index
    for seq in range(10):
        data = codec.encode_telemetry(1, seq, seq, {rpm: 9000.0, coolant: Missing.STALE})
        meter.record(codec.decode(data), len(data))
    totals, per_channel = meter.window(1.0)
    packet = codec.telemetry_size([rpm, coolant])
    assert totals["telemetry_packets_per_s"] == 10
    assert totals["encoded_bytes_per_s"] == 10 * packet
    assert totals["payload_bytes_per_s"] == 10 * 4  # STALE still costs its 2 bytes
    assert totals["serial_framed_bits_per_s"] == 8 * 10 * (packet + 2)
    assert per_channel["rpm"] == 160 and per_channel["coolant_temperature"] == 160
    assert per_channel["gear"] == 0


def test_pit_receiver_writes_bandwidth_points(layout):
    from pit_receiver.channels import StalenessConfig
    from pit_receiver.link_stats import LinkThresholds
    from pit_receiver.receiver import PitReceiver
    from pit_receiver.sinks import MemorySink
    from tests.test_transport import FakeClock

    alerts = load_yaml("config/alerts.yaml")
    clock = FakeClock()
    sink = MemorySink()
    rx = PitReceiver(layout, LinkThresholds.from_config(alerts["link"]),
                     StalenessConfig.from_config(alerts["channel_staleness"]), sinks=[sink],
                     wall=lambda: 1.7e9 + clock.t, mono=clock)
    codec = Codec(layout)
    rx.health()
    for seq in range(10):
        rx.on_datagram(codec.encode_telemetry(1, seq, seq * 100, {0: 9000.0}))
        clock.t += 0.1
    rx.health()
    (bw,) = sink.by_measurement("bandwidth")
    assert bw.fields["telemetry_packets_per_s"] == pytest.approx(10, rel=0.01)
    per_channel = {p.tags["channel"]: p.fields["bits_per_s"] for p in sink.by_measurement("channel_bandwidth")}
    assert per_channel["rpm"] == pytest.approx(160, rel=0.01)
