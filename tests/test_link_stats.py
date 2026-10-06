"""Pit link statistics: sequence gaps, loss, rate, staleness, uptime, restarts."""

from __future__ import annotations

import pytest

from common.config import ConfigError, load_yaml
from common.protocol import ChannelLayout, Codec
from pit_receiver.link_stats import LinkStats, LinkThresholds, SequenceTracker
from pit_receiver.status import Status

THRESHOLDS = LinkThresholds(stale_after_s=0.5, lost_after_s=2.0, degraded_loss_pct=5.0, critical_loss_pct=20.0)


# --------------------------------------------------------------- sequences
def test_in_order_sequence():
    t = SequenceTracker()
    for s in range(100):
        assert t.add(s) == "new"
    assert (t.expected, t.received, t.missing, t.loss_pct) == (100, 100, 0, 0.0)


def test_gaps_are_counted_as_missing():
    t = SequenceTracker()
    for s in [0, 1, 2, 5, 6, 10]:
        t.add(s)
    assert t.expected == 11
    assert t.missing == 5
    assert t.loss_pct == pytest.approx(100 * 5 / 11)


def test_wraparound_is_not_a_gap():
    t = SequenceTracker()
    for s in [65533, 65534, 65535, 0, 1, 2]:
        t.add(s)
    assert (t.expected, t.missing) == (6, 0)


def test_late_packets_fill_their_gap():
    t = SequenceTracker()
    for s in [0, 1, 3, 4, 2]:
        t.add(s)
    assert t.missing == 0
    assert t.out_of_order == 1


def test_duplicates_are_ignored():
    t = SequenceTracker()
    for s in [0, 1, 1, 2, 2, 2]:
        t.add(s)
    assert t.duplicates == 3
    assert (t.received, t.missing) == (3, 0)


def test_packets_older_than_the_window_are_rejected():
    t = SequenceTracker(window=50)
    for s in range(200):
        t.add(s)
    assert t.add(10) == "too_old"


def test_window_loss_only_counts_recent_packets():
    t = SequenceTracker()
    for s in range(0, 100, 2):  # 50% loss early on
        t.add(s)
    for s in range(100, 300):  # then perfect
        t.add(s)
    assert t.loss_pct == pytest.approx(100 * 50 / 300)
    assert t.window_loss_pct(100) == 0.0


@pytest.mark.parametrize("loss_every", [100, 20, 10, 2])
def test_loss_percentage_matches_reality(loss_every):
    t = SequenceTracker()
    for s in range(10000):
        if s % loss_every:
            t.add(s % 65536)
    assert t.loss_pct == pytest.approx(100 / loss_every, abs=0.05)


# --------------------------------------------------------------- link state
@pytest.fixture(scope="module")
def codec(dbc):
    return Codec(ChannelLayout.load("config/channels.yaml", dbc))


def packet(codec, seq, session=1, ts=0):
    return codec.decode(codec.encode_telemetry(session, seq, ts, {0: 9000.0}))


def test_thresholds_load_from_alerts_yaml():
    t = LinkThresholds.from_config(load_yaml("config/alerts.yaml")["link"])
    assert t.lost_after_s > t.stale_after_s
    with pytest.raises(ConfigError):
        LinkThresholds.from_config({"stale_after_s": 3, "lost_after_s": 2, "degraded_loss_pct": 5, "critical_loss_pct": 20})


def test_no_data_before_first_packet():
    snap = LinkStats(THRESHOLDS).snapshot(now=100.0)
    assert snap.status is Status.NO_DATA


def test_link_state_progression(codec):
    link = LinkStats(THRESHOLDS)
    t = 100.0
    for seq in range(50):
        link.record_packet(packet(codec, seq), t, 25)
        t += 0.1
    assert link.snapshot(t).status is Status.NORMAL
    assert link.snapshot(t + 0.6).status is Status.STALE
    snap = link.snapshot(t + 2.5)
    assert snap.status is Status.CRITICAL and "lost" in snap.reason
    assert snap.last_packet_age_s == pytest.approx(2.6)


def test_loss_drives_warning_and_critical(codec):
    for keep_every, expected in [(1, Status.NORMAL), (10, Status.WARNING), (3, Status.CRITICAL)]:
        link = LinkStats(THRESHOLDS)
        t = 0.0
        for seq in range(300):
            if keep_every == 1 or seq % keep_every:
                link.record_packet(packet(codec, seq), t, 25)
            t += 0.1
        assert link.snapshot(t).status is expected, keep_every


def test_rate_and_uptime(codec):
    link = LinkStats(THRESHOLDS, rate_window_s=5.0)
    t = 0.0
    for seq in range(110):  # 11 packets/s for 10 s
        link.record_packet(packet(codec, seq), t, 25)
        t += 1 / 11
    snap = link.snapshot(t)
    assert snap.packet_rate == pytest.approx(11.0, rel=0.05)
    assert snap.byte_rate == pytest.approx(275.0, rel=0.05)
    assert snap.session_uptime_s == pytest.approx(10.0, abs=0.1)
    assert snap.availability_pct == pytest.approx(100.0, abs=10)


def test_outage_reduces_availability(codec):
    link = LinkStats(THRESHOLDS)
    seq = 0
    for second in range(20):
        if 5 <= second < 15:
            continue  # 10 s outage
        for k in range(10):
            link.record_packet(packet(codec, seq), second + k / 10, 25)
            seq += 1
        seq += 0
    snap = link.snapshot(20.0)
    assert snap.availability_pct == pytest.approx(50.0, abs=6)


def test_car_restart_starts_a_new_session(codec):
    link = LinkStats(THRESHOLDS)
    for seq in range(1000, 1100):
        link.record_packet(packet(codec, seq, session=7), seq / 10, 25)
    assert link.record_packet(packet(codec, 0, session=8), 200.0, 25) == "new_session"
    snap = link.snapshot(200.0)
    assert snap.sessions == 2
    assert snap.packets_missing == 0  # the sequence reset is not counted as loss


def test_rejected_packets_are_counted_by_reason():
    link = LinkStats(THRESHOLDS)
    link.record_rejected("bad_crc", 1.0, 25)
    link.record_rejected("bad_crc", 1.1, 25)
    link.record_rejected("too_short", 1.2, 3)
    fields = link.snapshot(1.3).fields()
    assert fields["rejected_bad_crc"] == 2 and fields["rejected_too_short"] == 1 and fields["rejected_total"] == 3


def test_config_mismatch_is_a_warning(codec, dbc):
    layout = ChannelLayout.load("config/channels.yaml", dbc)
    from common.protocol import CarStatus

    status = CarStatus(layout.config_hash ^ 0xFF, 1, 1, 1, 0, 0, None, tuple([0.0] * len(layout.messages)))
    link = LinkStats(THRESHOLDS)
    link.record_packet(codec.decode(codec.encode_status(1, 0, 0, status)), 0.0, 34)
    snap = link.snapshot(0.1)
    assert snap.status is Status.WARNING and "channels.yaml" in snap.reason
