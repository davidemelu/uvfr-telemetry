"""Simulation configuration loading and validation."""

from __future__ import annotations

import copy

import pytest

from common.config import ConfigError, load_yaml
from simulator.simulation import Simulator


def test_shipped_config_loads(sim_config):
    sim = Simulator.from_config(sim_config, seed=1)
    assert sim.physics_hz == 100
    assert {tx.message.name for tx in sim.tx} >= {"SIM_ENGINE_FAST", "SIM_ENGINE_TEMPS", "SIM_STATUS"}


def test_missing_file_is_a_config_error():
    with pytest.raises(ConfigError, match="not found"):
        load_yaml("config/does_not_exist.yaml")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c: c.update(typo_section={}), "unknown key"),
        (lambda c: c["engine"].update(idle_rmp=1800), "unknown key"),
        (lambda c: c["engine"].pop("idle_rpm"), "missing key"),
        (lambda c: c.pop("cooling"), "missing key"),
        (lambda c: c["gearbox"].update(upshift_rpm=20000), "upshift_rpm"),
        (lambda c: c["cooling"].update(fan_off_c=99.0), "fan_off_c"),
        (lambda c: c["driver"].update(track=[]), "at least one segment"),
        (lambda c: c["scenarios"]["intermittent_can"].update(drop_probability=1.5), "drop_probability"),
        (lambda c: c["scenarios"]["sensor_dropout"].update(gap_s=[5, 1]), "greater than"),
        (lambda c: c.update(physics_hz=30), "multiple of"),
    ],
)
def test_invalid_config_is_rejected(sim_config, mutate, match):
    cfg = copy.deepcopy(sim_config)
    mutate(cfg)
    with pytest.raises(ConfigError, match=match):
        Simulator.from_config(cfg, seed=1)
