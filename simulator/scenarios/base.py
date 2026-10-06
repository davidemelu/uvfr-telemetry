"""Base class for simulated fault scenarios.

A scenario can act at three points, matching where real faults happen:

  apply_knobs     physics: change FaultKnobs on the vehicle model
                  (a failed fan, a worn oil pump, a dead alternator)
  filter_signals  sensors: change what a sensor reports
                  (dropout, frozen reading)
  drop_frame      CAN bus: frames that never arrive
                  (ECU stops sending, loose connector)

Physical faults ramp in over ramp_s and recover over recover_s when switched
off, so switching scenarios mid-session looks continuous, not like a step.
"""

from __future__ import annotations

import random
from typing import Any, ClassVar

from common.config import build_dataclass
from simulator.vehicle_model import FaultKnobs, VehicleModel


class Scenario:
    name: ClassVar[str] = ""
    bit: ClassVar[int] = -1  # position in the SimScenarioMask CAN signal
    summary: ClassVar[str] = ""
    Params: ClassVar[type]

    def __init__(self, params: dict[str, Any], rng: random.Random) -> None:
        self.params = build_dataclass(self.Params, params, f"simulation.scenarios.{self.name}")
        self.rng = rng
        self.active = False
        self.level = 0.0  # 0 = no effect, 1 = full effect
        self.activated_at: float | None = None

    # Optional ramp parameters; subclasses with physical effects define them.
    @property
    def ramp_s(self) -> float:
        return float(getattr(self.params, "ramp_s", 0.0))

    @property
    def recover_s(self) -> float:
        return float(getattr(self.params, "recover_s", 0.0))

    def set_active(self, active: bool, t: float) -> None:
        if active and not self.active:
            self.activated_at = t
            self.on_activate(t)
        elif not active and self.active:
            self.on_deactivate(t)
        self.active = active

    def update(self, t: float, dt: float) -> None:
        if self.active:
            self.level = 1.0 if self.ramp_s <= 0 else min(1.0, self.level + dt / self.ramp_s)
        else:
            self.level = 0.0 if self.recover_s <= 0 else max(0.0, self.level - dt / self.recover_s)

    def on_activate(self, t: float) -> None:
        """Hook for subclasses."""

    def on_deactivate(self, t: float) -> None:
        """Hook for subclasses."""

    def apply_knobs(self, knobs: FaultKnobs, t: float, model: VehicleModel) -> None:
        """Change physics. Called every step, including while recovering."""

    def filter_signals(self, signals: dict[str, float | None], t: float) -> None:
        """Change sensor readings in place. None means sensor fault."""

    def drop_frame(self, message_name: str, t: float) -> bool:
        """True if this frame should not appear on the bus."""
        return False

    def references(self) -> tuple[list[str], list[str]]:
        """(signal names, message names) this scenario refers to, for validation."""
        return [], []


class BurstSchedule:
    """Random on/off bursts: wait gap_s, then 'on' for duration_s, repeat."""

    def __init__(self, rng: random.Random, gap_s: tuple[float, float], duration_s: tuple[float, float]) -> None:
        self.rng = rng
        self.gap_s = gap_s
        self.duration_s = duration_s
        self.next_start = 0.0
        self.until = -1.0

    def reset(self, t: float, first_gap: tuple[float, float] = (0.5, 2.0)) -> None:
        self.next_start = t + self.rng.uniform(*first_gap)
        self.until = -1.0

    def on(self, t: float) -> bool:
        if t >= self.next_start and t >= self.until:
            self.until = t + self.rng.uniform(*self.duration_s)
            self.next_start = self.until + self.rng.uniform(*self.gap_s)
        return t < self.until
