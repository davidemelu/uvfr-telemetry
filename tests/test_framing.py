"""COBS framing used on serial radio links."""

from __future__ import annotations

import random

import pytest

from common.protocol.framing import FrameReader, cobs_decode, cobs_encode, frame


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00", b"\x00\x00", b"\x11\x22\x00\x33", b"\x01" * 253, b"\x01" * 254, b"\x01" * 255,
     b"\x01" * 254 + b"\x00", bytes(range(256)), bytes(range(1, 256)) * 3],
)
def test_roundtrip_edge_cases(data):
    encoded = cobs_encode(data)
    assert 0 not in encoded
    assert cobs_decode(encoded) == data


def test_roundtrip_random():
    rng = random.Random(4)
    for _ in range(500):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 600)))
        encoded = cobs_encode(data)
        assert 0 not in encoded
        assert cobs_decode(encoded) == data
        assert len(encoded) <= len(data) + 1 + len(data) // 254 + 1


def test_overhead_is_tiny_for_telemetry_sized_packets():
    assert len(frame(bytes(range(36)))) == 36 + 2  # one COBS byte + delimiter


def test_malformed_frames_are_rejected():
    with pytest.raises(ValueError):
        cobs_decode(b"\x05\x11\x22")  # claims 4 data bytes, has 2
    with pytest.raises(ValueError):
        cobs_decode(b"\x03\x00\x11")  # zero inside a block


def test_reader_splits_a_stream_and_resyncs():
    packets = [b"\x01\x02\x00\x03", b"", b"\xff" * 300, b"telemetry"]
    stream = b"".join(frame(p) for p in packets)
    reader = FrameReader()
    out = []
    for i in range(0, len(stream), 7):  # arbitrary chunking, like a serial port
        out += reader.feed(stream[i : i + 7])
    assert out == packets  # an empty packet survives framing; the protocol rejects it later

    reader = FrameReader()
    garbage = b"\x05\x01\x02"  # truncated frame, then a clean one
    assert reader.feed(garbage + b"\x00" + frame(b"ok")) == [b"ok"]
    assert reader.frame_errors == 1


def test_reader_drops_runaway_frames():
    reader = FrameReader(max_frame=64)
    # A frame glued to junk with no delimiter in between is discarded, never
    # delivered half-corrupted.
    assert reader.feed(b"\x01" * 200 + frame(b"lost")) == []
    assert reader.overflows == 1
    assert reader.feed(frame(b"fine")) == [b"fine"]  # back in sync after the delimiter
