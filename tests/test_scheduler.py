"""Channel scheduler: configured rates, spreading and status cadence."""

from __future__ import annotations

from collections import Counter

import pytest

from car_node.scheduler import ChannelScheduler
from common.protocol import ChannelLayout, Codec


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


def test_each_channel_runs_at_its_configured_rate(layout):
    sched = ChannelScheduler(layout)
    seconds = 30
    counts = Counter()
    for tick in range(seconds * layout.base_rate_hz):
        for i in sched.due(tick):
            counts[layout.channels[i].name] += 1
    for spec in layout.channels:
        assert counts[spec.name] == spec.rate_hz * seconds, spec.name


def test_status_cadence(layout):
    sched = ChannelScheduler(layout)
    ticks = [t for t in range(100) if sched.status_due(t)]
    assert len(ticks) == 100 // layout.base_rate_hz * layout.status_rate_hz
    assert ticks[0] == 0


def test_slow_channels_are_spread_across_ticks(layout):
    """Phase offsets keep packet sizes even, which keeps radio airtime even."""
    sched = ChannelScheduler(layout)
    codec = Codec(layout)
    sizes = [codec.telemetry_size(sched.due(t)) for t in range(layout.base_rate_hz)]
    no_phase = [
        codec.telemetry_size([i for i, p in enumerate(sched.periods) if t % p == 0])
        for t in range(layout.base_rate_hz)
    ]
    assert max(sizes) - min(sizes) < max(no_phase) - min(no_phase)


def test_every_tick_sends_something(layout):
    sched = ChannelScheduler(layout)
    assert all(sched.due(t) for t in range(100))
