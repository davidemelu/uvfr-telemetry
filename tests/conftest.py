from __future__ import annotations

import copy
import os
import subprocess
from collections.abc import Callable

import pytest

from common.config import load_yaml
from common.dbc import load_dbc
from simulator.simulation import Simulator

# Tests get their own virtual bus so they can run while the demo uses vcan0.
CAN_CHANNEL = os.environ.get("UVFR_TEST_CAN_CHANNEL", "vcan1")


@pytest.fixture(scope="session")
def sim_config() -> dict:
    return load_yaml("config/simulation.yaml")


@pytest.fixture(scope="session")
def dbc():
    return load_dbc("dbc/simulated_uvfr.dbc")


@pytest.fixture
def make_sim(sim_config) -> Callable[..., Simulator]:
    """Simulator factory; pass overrides as {"section": {"key": value}}."""

    def _make(seed: int = 1, overrides: dict | None = None) -> Simulator:
        cfg = copy.deepcopy(sim_config)
        for section, values in (overrides or {}).items():
            if isinstance(values, dict):
                cfg[section].update(values)
            else:
                cfg[section] = values
        return Simulator.from_config(cfg, seed=seed)

    return _make


def _vcan_available(channel: str) -> bool:
    try:
        out = subprocess.run(["ip", "link", "show", channel], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    return out.returncode == 0 and "UP" in out.stdout


@pytest.fixture(scope="session")
def can_channel() -> str:
    if not _vcan_available(CAN_CHANNEL):
        pytest.skip(f"{CAN_CHANNEL} not available (run scripts/setup-vcan.sh {CAN_CHANNEL})")
    return CAN_CHANNEL
