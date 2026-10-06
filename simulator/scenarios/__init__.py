"""Fault scenario registry.

Scenarios can be combined ("overheating,intermittent_can"). "normal" means
no faults. Each scenario has a fixed bit in the SimScenarioMask CAN signal so
the pit side can show which faults are being simulated.
"""

from __future__ import annotations

from collections.abc import Iterable

from simulator.scenarios.base import Scenario
from simulator.scenarios.faults import (
    BatteryVoltageSag,
    CanMessageTimeout,
    EngineOverrev,
    FrozenSensor,
    IntermittentCan,
    LowOilPressure,
    Overheating,
    SensorDropout,
)

NORMAL = "normal"

SCENARIOS: dict[str, type[Scenario]] = {
    cls.name: cls
    for cls in (
        Overheating,
        LowOilPressure,
        BatteryVoltageSag,
        SensorDropout,
        FrozenSensor,
        CanMessageTimeout,
        IntermittentCan,
        EngineOverrev,
    )
}

assert len({cls.bit for cls in SCENARIOS.values()}) == len(SCENARIOS), "duplicate scenario bits"


def parse_scenarios(values: str | Iterable[str]) -> list[str]:
    """Normalise "a,b" / ["a", "b,c"] / "normal" into a list of fault names."""
    if isinstance(values, str):
        values = [values]
    names: list[str] = []
    for value in values:
        for part in str(value).split(","):
            name = part.strip().lower().replace("-", "_")
            if not name or name == NORMAL:
                continue
            if name not in SCENARIOS:
                valid = ", ".join([NORMAL, *SCENARIOS])
                raise ValueError(f"unknown scenario {name!r}; valid: {valid}")
            if name not in names:
                names.append(name)
    return names


def mask_for(names: Iterable[str]) -> int:
    mask = 0
    for name in names:
        mask |= 1 << SCENARIOS[name].bit
    return mask


def names_from_mask(mask: int) -> list[str]:
    return [name for name, cls in SCENARIOS.items() if mask & (1 << cls.bit)]


__all__ = ["NORMAL", "SCENARIOS", "Scenario", "mask_for", "names_from_mask", "parse_scenarios"]
