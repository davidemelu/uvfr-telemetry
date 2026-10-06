"""Binary telemetry protocol: encode/decode, validation and corruption handling."""

from __future__ import annotations

import pytest

from common.protocol import (
    OVERHEAD_BYTES,
    CarStatus,
    ChannelLayout,
    Codec,
    DecodeError,
    Missing,
    PacketType,
    StatusPacket,
    TelemetryPacket,
    crc16,
)
from common.protocol.packet import HEADER


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


@pytest.fixture
def codec(layout):
    return Codec(layout)


def idx(layout, name):
    return layout.by_name(name).index


def all_values(layout):
    return {c.index: (c.physical_min + c.physical_max) / 4 for c in layout.channels}


def status(layout, **overrides):
    base = dict(
        config_hash=layout.config_hash,
        tx_packets=1234,
        can_frames=99999,
        can_fps=231,
        can_counter_gaps=3,
        can_errors=1,
        cpu_pct=7,
        message_ages_ms=tuple([12.0] * len(layout.messages)),
    )
    base.update(overrides)
    return CarStatus(**base)


def test_crc16_ccitt_false_check_value():
    assert crc16(b"123456789") == 0x29B1


def test_header_layout(codec, layout):
    data = codec.encode_telemetry(0xAB, 0x1234, 0x01020304, {0: 5000.0})
    ver_type, session, seq, ts = HEADER.unpack_from(data)
    assert ver_type == 0x11  # version 1, TELEMETRY
    assert (session, seq, ts) == (0xAB, 0x1234, 0x01020304)
    assert len(data) == OVERHEAD_BYTES + layout.bitmap_bytes + 2


def test_telemetry_roundtrip_all_channels(codec, layout):
    values = all_values(layout)
    packet = codec.decode(codec.encode_telemetry(7, 42, 123456, values))
    assert isinstance(packet, TelemetryPacket)
    assert packet.header.packet_type is PacketType.TELEMETRY
    assert (packet.header.session, packet.header.seq, packet.header.timestamp_ms) == (7, 42, 123456)
    for spec in layout.channels:
        assert packet.values[spec.name] == pytest.approx(values[spec.index], abs=spec.scale / 2)


def test_telemetry_subset_only_carries_listed_channels(codec, layout):
    values = {idx(layout, "rpm"): 9876.0, idx(layout, "coolant_temperature"): 88.3}
    data = codec.encode_telemetry(1, 1, 1, values)
    assert len(data) == codec.telemetry_size(list(values))
    packet = codec.decode(data)
    assert packet.values == pytest.approx({"rpm": 9876.0, "coolant_temperature": 88.3})


@pytest.mark.parametrize(
    ("name", "value", "resolution"),
    [
        ("rpm", 12841.0, 1),
        ("throttle_position", 57.7, 0.5),
        ("vehicle_speed", 63.27, 0.01),
        ("oil_pressure", 412.37, 0.1),
        ("coolant_temperature", -12.34, 0.1),
        ("lambda", 0.873, 0.001),
        ("battery_voltage", 13.86, 0.01),
        ("gear", 3.0, 1),
    ],
)
def test_quantisation(codec, layout, name, value, resolution):
    packet = codec.decode(codec.encode_telemetry(1, 1, 1, {idx(layout, name): value}))
    assert packet.values[name] == pytest.approx(value, abs=resolution / 2 + 1e-9)


def test_missing_values_roundtrip(codec, layout):
    values = {idx(layout, "rpm"): Missing.STALE, idx(layout, "oil_pressure"): Missing.NO_DATA,
              idx(layout, "throttle_position"): Missing.STALE, idx(layout, "coolant_temperature"): Missing.NO_DATA}
    packet = codec.decode(codec.encode_telemetry(1, 1, 1, values))
    assert packet.values == {"rpm": Missing.STALE, "oil_pressure": Missing.NO_DATA,
                             "throttle_position": Missing.STALE, "coolant_temperature": Missing.NO_DATA}


def test_out_of_range_values_saturate_and_are_counted(codec, layout):
    i = idx(layout, "throttle_position")  # u8 x 0.5: max valid 253 * 0.5 = 126.5
    packet = codec.decode(codec.encode_telemetry(1, 1, 1, {i: 500.0}))
    assert packet.values["throttle_position"] == pytest.approx(126.5)
    assert codec.saturated == 1
    # never mistaken for a sentinel
    packet = codec.decode(codec.encode_telemetry(1, 1, 1, {idx(layout, "rpm"): 1e9}))
    assert packet.values["rpm"] == 65533


def test_status_roundtrip(codec, layout):
    st = status(layout, message_ages_ms=(0.0, 19.0, 20.0, 999.0, None, 5080.0, 1e6))
    packet = codec.decode(codec.encode_status(9, 65535, 1000, st))
    assert isinstance(packet, StatusPacket)
    assert packet.config_match
    s = packet.status
    assert (s.tx_packets, s.can_frames, s.can_fps, s.can_counter_gaps, s.can_errors, s.cpu_pct) == (1234, 99999, 231, 3, 1, 7)
    assert s.message_ages_ms == (0.0, 0.0, 20.0, 980.0, None, 5080.0, 5080.0)  # 20 ms units, saturating
    assert len(codec.encode_status(9, 1, 1, st)) == codec.status_size


def test_status_with_unknown_cpu_and_big_counters(codec, layout):
    st = status(layout, cpu_pct=None, can_counter_gaps=10**6, tx_packets=2**33 + 5)
    s = codec.decode(codec.encode_status(1, 1, 1, st)).status
    assert s.cpu_pct is None
    assert s.can_counter_gaps == 0xFFFF  # saturates
    assert s.tx_packets == 5  # u32 wraps


def test_config_mismatch_is_flagged(codec, layout):
    packet = codec.decode(codec.encode_status(1, 1, 1, status(layout, config_hash=layout.config_hash ^ 1)))
    assert packet.config_match is False


def test_sequence_and_timestamp_wrap(codec):
    packet = codec.decode(codec.encode_telemetry(1, 65536 + 5, 2**32 + 7, {0: 1.0}))
    assert packet.header.seq == 5
    assert packet.header.timestamp_ms == 7


# ------------------------------------------------------------------ rejects
def test_rejects_short_packets(codec):
    with pytest.raises(DecodeError) as exc:
        codec.decode(b"\x11\x00\x00")
    assert exc.value.reason == "too_short"


def test_every_single_bit_flip_is_detected(codec, layout):
    data = codec.encode_telemetry(3, 77, 5555, all_values(layout))
    for bit in range(len(data) * 8):
        corrupted = bytearray(data)
        corrupted[bit // 8] ^= 1 << (bit % 8)
        with pytest.raises(DecodeError) as exc:
            codec.decode(bytes(corrupted))
        assert exc.value.reason == "bad_crc"


def test_truncated_packet_is_rejected(codec, layout):
    data = codec.encode_telemetry(3, 77, 5555, all_values(layout))
    for cut in range(1, len(data) - OVERHEAD_BYTES):
        with pytest.raises(DecodeError):
            codec.decode(data[:-cut])


def _reseal(body: bytes) -> bytes:
    return body + crc16(body).to_bytes(2, "little")


def test_rejects_wrong_version(codec):
    data = bytearray(codec.encode_telemetry(1, 1, 1, {0: 1.0}))
    data[0] = (2 << 4) | 1
    with pytest.raises(DecodeError) as exc:
        codec.decode(_reseal(bytes(data[:-2])))
    assert exc.value.reason == "bad_version"


def test_rejects_unknown_type(codec):
    data = bytearray(codec.encode_telemetry(1, 1, 1, {0: 1.0}))
    data[0] = (1 << 4) | 9
    with pytest.raises(DecodeError) as exc:
        codec.decode(_reseal(bytes(data[:-2])))
    assert exc.value.reason == "unknown_type"


def test_rejects_payload_length_mismatch(codec):
    data = codec.encode_telemetry(1, 1, 1, {0: 1.0})
    with pytest.raises(DecodeError) as exc:
        codec.decode(_reseal(data[:-2] + b"\x00"))  # one extra byte, valid CRC
    assert exc.value.reason == "bad_length"


def test_rejects_bitmap_naming_unknown_channels(codec, layout):
    body = bytearray(codec.encode_telemetry(1, 1, 1, {})[:-2])
    body[HEADER.size + layout.bitmap_bytes - 1] |= 0x80  # bit 15: channel 15 does not exist
    with pytest.raises(DecodeError) as exc:
        codec.decode(_reseal(bytes(body)))
    assert exc.value.reason == "bad_bitmap"


def test_rejects_garbage(codec):
    import random

    rng = random.Random(1)
    for _ in range(2000):
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 60)))
        with pytest.raises(DecodeError):
            codec.decode(blob)


def test_encoder_validates_inputs(codec, layout):
    with pytest.raises(ValueError):
        codec.encode_telemetry(1, 1, 1, {len(layout.channels): 1.0})
    with pytest.raises(ValueError):
        codec.encode_status(1, 1, 1, status(layout, message_ages_ms=(1.0,)))


def test_maximum_packet_is_small(codec, layout):
    full = codec.telemetry_size(list(range(len(layout.channels))))
    assert full <= 40, "a full telemetry packet should stay tiny for LoRa"
    assert codec.status_size <= 40
