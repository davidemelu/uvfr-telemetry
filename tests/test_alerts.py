"""Alert engine: thresholds from config, hysteresis, holds, gating, rates, staleness, link."""

from __future__ import annotations

import copy

import pytest

from common.config import ConfigError, load_yaml
from common.protocol import ChannelLayout
from pit_receiver.alerts import AlertEngine, parse_rules
from pit_receiver.channels import ChannelView
from pit_receiver.link_stats import LinkSnapshot
from pit_receiver.status import Status


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


@pytest.fixture(scope="module")
def alerts_cfg():
    return load_yaml("config/alerts.yaml")


@pytest.fixture
def engine(alerts_cfg, layout):
    return AlertEngine.from_config(alerts_cfg, layout)


def link(age=0.05, loss=0.0, ready=True, mismatch=False) -> LinkSnapshot:
    return LinkSnapshot(Status.NORMAL, "ok", 1, 1, 100, 100, 0, loss, loss, ready, 0, 0, {}, 11.0, 275.0, age,
                        60.0, 100.0, 100, mismatch)


HEALTHY = {"rpm": 9000.0, "oil_pressure": 420.0, "battery_voltage": 13.8, "coolant_temperature": 86.0,
           "engine_temperature": 110.0}


def views(layout, now, **values):
    """Live views for every channel; pass name=value, or name=Status for a non-live channel."""
    out = {}
    for c in layout.channels:
        v = values.get(c.name, HEALTHY.get(c.name, 1.0))
        if isinstance(v, Status):
            out[c.name] = ChannelView(c.name, v, None, 1.0, 1.0, "test")
        else:
            out[c.name] = ChannelView(c.name, Status.NORMAL, v, v, 0.05)
    return out


def feed(engine, layout, t, **values):
    numeric = {k: v for k, v in values.items() if not isinstance(v, Status)}
    engine.observe(numeric, t)
    return views(layout, t, **values)


def active(engine):
    return {(a.rule.id, a.channel): a.severity for a in engine.active()}


# ---------------------------------------------------------------- config
def test_shipped_rules_cover_the_requested_alarms(alerts_cfg, layout):
    ids = {r.id for r in parse_rules(alerts_cfg["rules"], layout)}
    for required in ("coolant_high", "coolant_rising_fast", "oil_pressure_low", "battery_voltage_low",
                     "rpm_excessive", "signal_stale", "telemetry_lost"):
        assert required in ids


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r[0].update(type="magic"), "type must be"),
        (lambda r: r[0].update(channel="warp_core"), "unknown channel"),
        (lambda r: r[0].update(above={"warning": 110, "critical": 105}), "beyond"),
        (lambda r: r[0].pop("above"), "above and/or below"),
        (lambda r: r.append(dict(r[0])), "duplicate"),
        (lambda r: r[0].update(typo=1), "unknown key"),
        (lambda r: r[0].update(when={"channel": "nope", "above": 1}), "unknown channel"),
        (lambda r: r[0].update(severity="apocalyptic"), "severity"),
    ],
)
def test_invalid_rules_are_rejected(alerts_cfg, layout, mutate, match):
    rules = copy.deepcopy(alerts_cfg["rules"])
    mutate(rules)
    with pytest.raises(ConfigError, match=match):
        parse_rules(rules, layout)


# ------------------------------------------------------------- threshold
def test_coolant_warning_critical_and_hysteresis(engine, layout):
    t = 0.0
    for temp, expected in [(100, None), (105.2, Status.WARNING), (110.5, Status.CRITICAL),
                           (109.8, Status.CRITICAL),  # within 1.0 degC hysteresis: stays critical
                           (108.5, Status.WARNING), (104.5, Status.WARNING), (103.5, None)]:
        t += 1
        engine.evaluate(t, feed(engine, layout, t, coolant_temperature=temp), link())
        assert active(engine).get(("coolant_high", "coolant_temperature")) == expected, temp


def test_spike_between_evaluations_is_caught_and_held(engine, layout):
    t = 0.0
    engine.observe({"rpm": 9000.0}, t)
    engine.observe({"rpm": 14600.0}, t + 0.3)  # 0.3 s over-rev between two 1 Hz evaluations
    engine.observe({"rpm": 9100.0}, t + 0.6)
    engine.evaluate(1.0, views(layout, 1.0, rpm=9100.0), link())
    assert active(engine)[("rpm_excessive", "rpm")] is Status.CRITICAL
    engine.evaluate(3.0, feed(engine, layout, 3.0, rpm=9000.0), link())
    assert ("rpm_excessive", "rpm") in active(engine)  # hold_s 5
    engine.evaluate(7.0, feed(engine, layout, 7.0, rpm=9000.0), link())
    assert ("rpm_excessive", "rpm") not in active(engine)


def test_oil_pressure_is_only_judged_above_3000_rpm(engine, layout):
    engine.evaluate(1.0, feed(engine, layout, 1.0, rpm=1800.0, oil_pressure=130.0), link())
    assert ("oil_pressure_low", "oil_pressure") not in active(engine)  # idle: normal for 130 kPa
    engine.evaluate(2.0, feed(engine, layout, 2.0, rpm=8000.0, oil_pressure=130.0), link())
    assert active(engine)[("oil_pressure_low", "oil_pressure")] is Status.WARNING
    engine.evaluate(3.0, feed(engine, layout, 3.0, rpm=8000.0, oil_pressure=80.0), link())
    assert active(engine)[("oil_pressure_low", "oil_pressure")] is Status.CRITICAL


def test_battery_low(engine, layout):
    engine.evaluate(1.0, feed(engine, layout, 1.0, battery_voltage=11.9), link())
    assert active(engine)[("battery_voltage_low", "battery_voltage")] is Status.WARNING
    engine.evaluate(2.0, feed(engine, layout, 2.0, battery_voltage=11.4), link())
    assert active(engine)[("battery_voltage_low", "battery_voltage")] is Status.CRITICAL


def test_alarm_is_not_cleared_when_data_goes_stale(engine, layout):
    engine.evaluate(1.0, feed(engine, layout, 1.0, coolant_temperature=112.0), link())
    assert active(engine)[("coolant_high", "coolant_temperature")] is Status.CRITICAL
    engine.evaluate(2.0, views(layout, 2.0, coolant_temperature=Status.STALE), link())
    assert active(engine)[("coolant_high", "coolant_temperature")] is Status.CRITICAL


# ------------------------------------------------------------------ rate
def test_coolant_rising_rapidly(engine, layout):
    t, temp = 0.0, 91.0
    while t < 20:  # 0.5 degC/s ramp, sampled at 5 Hz
        engine.observe({"coolant_temperature": temp}, t)
        t += 0.2
        temp += 0.1
    engine.evaluate(t, views(layout, t, coolant_temperature=temp), link())
    alarm = next(a for a in engine.active() if a.rule.id == "coolant_rising_fast")
    assert alarm.severity is Status.WARNING
    assert alarm.value == pytest.approx(0.5, abs=0.02)


def test_rate_rule_ignores_slow_warm_up_below_gate(engine, layout):
    t, temp = 0.0, 75.0
    while t < 20:  # fast rise, but below 90 degC
        engine.observe({"coolant_temperature": temp}, t)
        t += 0.2
        temp += 0.2
    engine.evaluate(t, views(layout, t, coolant_temperature=min(temp, 89.0)), link())
    assert not any(a.rule.id == "coolant_rising_fast" for a in engine.active())


# -------------------------------------------------------- liveness rules
def test_stale_and_sensor_fault_need_the_channel_to_have_been_live(engine, layout):
    engine.evaluate(1.0, views(layout, 1.0, oil_pressure=Status.NO_DATA, gear=Status.STALE), link())
    assert not active(engine)  # never live: not an alarm, just NO DATA
    engine.observe({"oil_pressure": 400.0, "gear": 3.0}, 1.5)
    engine.evaluate(2.0, views(layout, 2.0, oil_pressure=Status.NO_DATA, gear=Status.STALE), link())
    assert active(engine)[("sensor_fault", "oil_pressure")] is Status.WARNING
    assert active(engine)[("signal_stale", "gear")] is Status.WARNING


def test_frozen_sensor(engine, layout):
    t = 0.0
    while t < 10:
        engine.observe({"coolant_temperature": 88.3, "rpm": 9000.0, "battery_voltage": 13.8 + (t % 1) / 100}, t)
        t += 0.2
    v = views(layout, t, coolant_temperature=88.3, rpm=9000.0, battery_voltage=13.81)
    engine.evaluate(t, v, link())
    assert ("frozen_sensor", "coolant_temperature") in active(engine)
    assert ("frozen_sensor", "battery_voltage") not in active(engine)
    assert engine.channel_frozen("coolant_temperature")


# ------------------------------------------------------------------ link
def test_link_alarms(engine, layout):
    v = views(layout, 1.0)
    engine.evaluate(1.0, v, link(age=0.8))
    assert active(engine)[("telemetry_stale", "")] is Status.WARNING
    engine.evaluate(2.0, v, link(age=3.0))
    assert active(engine)[("telemetry_lost", "")] is Status.CRITICAL
    engine.evaluate(3.0, v, link(age=0.05, loss=8.0))
    assert active(engine)[("packet_loss_high", "")] is Status.WARNING
    assert ("telemetry_lost", "") not in active(engine)
    engine.evaluate(4.0, v, link(age=0.05, loss=30.0))
    assert active(engine)[("packet_loss_high", "")] is Status.CRITICAL
    engine.evaluate(5.0, v, link(age=0.05, loss=30.0, ready=False))
    assert ("packet_loss_high", "") not in active(engine)
    engine.evaluate(6.0, v, link(mismatch=True))
    assert active(engine)[("channel_layout_mismatch", "")] is Status.WARNING


def test_can_age_alarm(engine, layout):
    v = views(layout, 1.0)
    engine.evaluate(1.0, v, link(), can_age_ms=300.0)
    assert active(engine)[("can_data_delayed", "")] is Status.WARNING
    engine.evaluate(2.0, v, link(), can_age_ms=5080.0)
    assert active(engine)[("can_data_delayed", "")] is Status.CRITICAL
    engine.evaluate(3.0, v, link(), can_age_ms=None)  # no recent STATUS: hold
    assert ("can_data_delayed", "") in active(engine)
    engine.evaluate(4.0, v, link(), can_age_ms=40.0)
    assert ("can_data_delayed", "") not in active(engine)


def test_events_describe_transitions(engine, layout):
    events = engine.evaluate(1.0, feed(engine, layout, 1.0, coolant_temperature=111.0), link())
    (ev,) = [e for e in events if e.rule_id == "coolant_high"]
    assert (ev.previous, ev.severity) == (Status.NORMAL, Status.CRITICAL)
    assert "Coolant temperature high" in ev.message and "111" in ev.message
    events = engine.evaluate(2.0, feed(engine, layout, 2.0, coolant_temperature=90.0), link())
    assert [(e.rule_id, e.severity) for e in events] == [("coolant_high", Status.NORMAL)]
