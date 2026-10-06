"""Failure scenarios: each fault shows up where it would on a real car."""

from __future__ import annotations

from collections import Counter

import pytest

from common.config import ConfigError
from common.dbc import decode_physical
from simulator.scenarios import SCENARIOS, mask_for, names_from_mask, parse_scenarios

REQUESTED = [
    "overheating",
    "low_oil_pressure",
    "battery_voltage_sag",
    "sensor_dropout",
    "frozen_sensor",
    "can_message_timeout",
    "intermittent_can",
    "engine_overrev",
]


def warmed(make_sim, seconds: float = 120, seed: int = 1):
    sim = make_sim(seed=seed)
    sim.run_for(seconds)
    return sim


def decoded(dbc, frames, message_name):
    message = dbc.get_message_by_name(message_name)
    return [decode_physical(message, f.data) for f in frames if f.name == message_name]


# ----------------------------------------------------------------- registry
def test_all_requested_scenarios_exist():
    assert set(REQUESTED) == set(SCENARIOS)


def test_parse_scenarios():
    assert parse_scenarios("normal") == []
    assert parse_scenarios("overheating,intermittent_can") == ["overheating", "intermittent_can"]
    assert parse_scenarios(["Low-Oil-Pressure", "overheating,low_oil_pressure"]) == ["low_oil_pressure", "overheating"]
    with pytest.raises(ValueError, match="unknown scenario"):
        parse_scenarios("engine_explodes")


def test_scenario_mask_roundtrip():
    assert mask_for([]) == 0
    assert mask_for(REQUESTED) == 0xFF
    assert names_from_mask(mask_for(["overheating", "engine_overrev"])) == ["overheating", "engine_overrev"]


def test_unknown_scenario_rejected_by_simulator(make_sim):
    with pytest.raises(ValueError):
        make_sim().set_scenarios(["nope"])


def test_scenario_config_references_are_validated(make_sim, sim_config):
    bad = dict(sim_config["scenarios"])
    bad["sensor_dropout"] = {**bad["sensor_dropout"], "signals": ["NoSuchSignal"]}
    with pytest.raises(ConfigError, match="NoSuchSignal"):
        make_sim(overrides={"scenarios": bad})


# ---------------------------------------------------------- physical faults
def test_overheating_raises_coolant_and_recovers(make_sim):
    sim = warmed(make_sim)
    assert sim.model.coolant_c < 95
    sim.set_scenarios(["overheating"])
    sim.run_for(60)
    assert sim.model.coolant_c > 110
    assert sim.model.head_c > sim.model.coolant_c
    peak = sim.model.coolant_c
    sim.set_scenarios([])
    sim.run_for(120)
    assert sim.model.coolant_c < peak - 10


def test_low_oil_pressure(make_sim):
    sim = warmed(make_sim)
    sim.set_scenarios(["low_oil_pressure"])
    sim.run_for(15)
    samples = []
    for _ in range(3000):
        sim.step()
        if sim.model.rpm > 3000:
            samples.append(sim.model.oil_kpa)
    assert max(samples) < 200
    assert min(samples) < 100


def test_battery_voltage_sag(make_sim):
    sim = warmed(make_sim)
    assert sim.model.battery_v > 13.5
    sim.set_scenarios(["battery_voltage_sag"])
    sim.run_for(30)
    assert sim.model.battery_v < 12.4
    sim.run_for(60)
    assert sim.model.battery_v < 11.5


def test_engine_overrev(make_sim):
    sim = warmed(make_sim)
    sim.set_scenarios(["engine_overrev"])
    peak = 0.0
    for _ in range(90 * sim.physics_hz):
        sim.step()
        peak = max(peak, sim.model.rpm)
    assert 14000 < peak < 16000  # above the alarm, inside the DBC range


def test_combined_scenarios_both_take_effect(make_sim):
    sim = warmed(make_sim)
    sim.set_scenarios(["overheating", "battery_voltage_sag"])
    sim.run_for(60)
    assert sim.model.coolant_c > 105
    assert sim.model.battery_v < 12.2


# ------------------------------------------------------ sensor and bus faults
def test_sensor_dropout_sends_fault_values(make_sim, dbc):
    sim = warmed(make_sim, 30)
    sim.set_scenarios(["sensor_dropout"])
    rows = decoded(dbc, sim.run_for(60), "SIM_ENGINE_PRESSURES")
    assert len(rows) == 60 * 50  # frames keep arriving at full rate
    faults = sum(r["OilPressure"] is None for r in rows)
    assert 0.05 * len(rows) < faults < 0.6 * len(rows)
    assert all(r["FuelPressure"] is not None for r in rows)


def test_frozen_sensor_holds_its_value(make_sim, dbc):
    sim = warmed(make_sim, 30)  # still warming up, so coolant is moving
    sim.set_scenarios(["frozen_sensor"])
    rows = decoded(dbc, sim.run_for(30), "SIM_ENGINE_TEMPS")
    assert len(rows) == 300
    assert len({r["CoolantTemp"] for r in rows}) == 1
    assert len({r["EngineTemp"] for r in rows}) > 10
    assert len({r["EngineTemps_Counter"] for r in rows}) == 16
    assert abs(rows[-1]["CoolantTemp"] - sim.model.coolant_c) > 0.5


def test_can_message_timeout_stops_one_message(make_sim):
    sim = warmed(make_sim, 10)
    sim.set_scenarios(["can_message_timeout"])
    counts = Counter(f.name for f in sim.run_for(10))
    assert counts["SIM_ENGINE_TEMPS"] == 0
    assert counts["SIM_ENGINE_FAST"] == 1000
    assert counts["SIM_ENGINE_PRESSURES"] == 500


def test_intermittent_can_drops_frames_and_breaks_counters(make_sim, dbc):
    sim = warmed(make_sim, 10)
    sim.set_scenarios(["intermittent_can"])
    frames = sim.run_for(60)
    expected = sum(60 * 1000 // tx.message.cycle_time for tx in sim.tx)
    loss = 1 - len(frames) / expected
    assert 0.12 < loss < 0.45
    counters = [r["EngineFast_Counter"] for r in decoded(dbc, frames, "SIM_ENGINE_FAST")]
    gaps = sum(1 for a, b in zip(counters, counters[1:]) if (b - a) % 16 != 1)
    assert gaps > 50


def test_runtime_switch_is_broadcast_in_sim_status(make_sim, dbc):
    sim = make_sim()
    masks = [r["SimScenarioMask"] for r in decoded(dbc, sim.run_for(2), "SIM_STATUS")]
    assert masks == [0.0, 0.0]
    sim.set_scenarios(["overheating", "low_oil_pressure"])
    masks = [r["SimScenarioMask"] for r in decoded(dbc, sim.run_for(3), "SIM_STATUS")]
    assert masks == [float(mask_for(["overheating", "low_oil_pressure"]))] * 3
