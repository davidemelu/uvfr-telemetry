"""Pit receiver building blocks: clock mapping, channel liveness, line protocol."""

from __future__ import annotations

import random

import pytest

from common.protocol import ChannelLayout, Missing
from pit_receiver.channels import ChannelTracker, StalenessConfig
from pit_receiver.clock import SourceClock
from pit_receiver.points import Point
from pit_receiver.status import Status, worst


# ------------------------------------------------------------------- clock
def test_clock_removes_jitter():
    """Samples are placed at the time they were measured, not when they arrived."""
    rng = random.Random(3)
    clock = SourceClock(window_s=30)
    true_offset = 1_700_000_000.0  # pit wall time when the car node started
    errors = []
    for i in range(600):  # 60 s at 10 Hz
        src_ms = i * 100
        arrival = true_offset + src_ms / 1000 + 0.080 + rng.uniform(0, 0.150)  # 80 ms latency + 0..150 ms jitter
        mapped = clock.observe(src_ms, arrival)
        if i > 50:
            errors.append(mapped - (true_offset + src_ms / 1000))
    # Mapped times sit at true time + minimum latency, with jitter removed.
    assert max(errors) - min(errors) < 0.01
    assert min(errors) == pytest.approx(0.080, abs=0.01)


def test_clock_reports_excess_delay():
    clock = SourceClock()
    clock.observe(0, 100.0)
    clock.observe(100, 100.1)
    clock.observe(200, 100.45)  # arrived 250 ms later than the best case
    assert clock.take_max_excess_delay() == pytest.approx(0.25)
    assert clock.take_max_excess_delay() == 0.0


def test_clock_unwraps_32_bit_timestamps():
    clock = SourceClock()
    a = clock.observe(2**32 - 100, 5000.0)
    b = clock.observe(50, 5000.15)  # wrapped
    assert b - a == pytest.approx(0.15)


def test_clock_follows_drift():
    clock = SourceClock(window_s=10)
    for i in range(1200):  # 120 s; the pit clock runs 1000 ppm fast
        src = i * 0.1
        mapped = clock.observe(int(src * 1000), 50.0 + src * 1.001)
    assert mapped == pytest.approx(50.0 + 119.9 * 1.001, abs=0.02)


# --------------------------------------------------------------- channels
@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


def test_channel_liveness(layout):
    tracker = ChannelTracker(layout, StalenessConfig(missed_updates=3, min_stale_after_s=0.5))
    assert tracker.view("rpm", 0.0).status is Status.NO_DATA
    tracker.update({"rpm": 9000.0, "coolant_temperature": Missing.STALE, "oil_pressure": Missing.NO_DATA}, 0.0, 10.0)
    rpm = tracker.view("rpm", 10.2)
    assert rpm.status is Status.NORMAL and rpm.value == 9000.0
    assert tracker.view("rpm", 10.6).status is Status.STALE  # 10 Hz: max(0.5, 3 x 0.1) = 0.5 s
    assert tracker.view("rpm", 10.6).last_value == 9000.0
    assert tracker.view("coolant_temperature", 10.1).status is Status.STALE  # flagged by the car
    assert tracker.view("oil_pressure", 10.1).status is Status.NO_DATA  # sensor fault on the car
    assert tracker.stale_after["intake_air_temperature"] == 3.0  # 1 Hz channel


def test_worst_status_ordering():
    assert worst([Status.NORMAL, Status.STALE, Status.WARNING]) is Status.WARNING
    assert worst([Status.NO_DATA, Status.CRITICAL]) is Status.CRITICAL
    assert worst([]) is Status.NO_DATA
    assert Status.NO_DATA.label == "NO DATA"


# ---------------------------------------------------------- line protocol
def test_line_protocol_types_and_order():
    p = Point("vehicle", {"rpm": 9000.0, "gear": 3, "ok": True, "label": "a b"}, {"car": "uvfr-sim"}, 1700000000000000000)
    assert p.to_line() == 'vehicle,car=uvfr-sim gear=3i,label="a b",ok=true,rpm=9000.0 1700000000000000000'


def test_line_protocol_escaping():
    p = Point("my measure,x", {"field key=": 'say "hi"\\'}, {"tag,key": "va lue=1"})
    assert p.to_line() == 'my\\ measure\\,x,tag\\,key=va\\ lue\\=1 field\\ key\\=="say \\"hi\\"\\\\"'


def test_non_finite_fields_are_dropped():
    assert Point("m", {"a": float("nan"), "b": 1.5}).to_line() == "m b=1.5"
    assert Point("m", {"a": float("inf")}).to_line() is None


def test_empty_tags_are_skipped():
    assert Point("m", {"a": 1}, {"car": ""}).to_line() == "m a=1i"
