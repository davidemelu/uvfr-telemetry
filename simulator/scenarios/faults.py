"""The simulated fault scenarios. Parameters live in config/simulation.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.config import float_pair
from simulator.scenarios.base import BurstSchedule, Scenario
from simulator.vehicle_model import FaultKnobs, VehicleModel


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"expected a list of names, got {value!r}")
    return tuple(value)


class Overheating(Scenario):
    name = "overheating"
    bit = 0
    summary = "Fan failure plus partial coolant loss; coolant and head temperature climb"

    @dataclass(frozen=True)
    class Params:
        cooling_efficiency: float
        coolant_capacity_factor: float
        ramp_s: float
        recover_s: float

    def apply_knobs(self, knobs: FaultKnobs, t: float, model: VehicleModel) -> None:
        p = self.params
        knobs.cooling_efficiency *= 1.0 - self.level * (1.0 - p.cooling_efficiency)
        knobs.coolant_capacity_factor *= 1.0 - self.level * (1.0 - p.coolant_capacity_factor)


class LowOilPressure(Scenario):
    name = "low_oil_pressure"
    bit = 1
    summary = "Worn oil pump and oil surge under braking; pressure drops at every RPM"

    @dataclass(frozen=True)
    class Params:
        pump_factor: float
        slosh_drop: float
        ramp_s: float
        recover_s: float

    def apply_knobs(self, knobs: FaultKnobs, t: float, model: VehicleModel) -> None:
        p = self.params
        knobs.oil_pump_factor *= 1.0 - self.level * (1.0 - p.pump_factor)
        knobs.oil_slosh_drop = max(knobs.oil_slosh_drop, self.level * p.slosh_drop)


class BatteryVoltageSag(Scenario):
    name = "battery_voltage_sag"
    bit = 2
    summary = "Alternator stops charging; the battery drains and voltage sags under load"

    @dataclass(frozen=True)
    class Params:
        alternator_output: float
        ramp_s: float
        recover_s: float

    def apply_knobs(self, knobs: FaultKnobs, t: float, model: VehicleModel) -> None:
        knobs.alternator_output *= 1.0 - self.level * (1.0 - self.params.alternator_output)


class SensorDropout(Scenario):
    name = "sensor_dropout"
    bit = 3
    summary = "Intermittent sensor wiring: signals report 'not available' in random bursts"

    @dataclass(frozen=True)
    class Params:
        signals: tuple[str, ...]
        gap_s: tuple[float, float]
        duration_s: tuple[float, float]

        def __post_init__(self) -> None:
            object.__setattr__(self, "signals", _tuple_of_str(self.signals))
            object.__setattr__(self, "gap_s", float_pair(self.gap_s, "gap_s"))
            object.__setattr__(self, "duration_s", float_pair(self.duration_s, "duration_s"))

    def __init__(self, params: dict[str, Any], rng) -> None:
        super().__init__(params, rng)
        self._bursts = BurstSchedule(rng, self.params.gap_s, self.params.duration_s)

    def on_activate(self, t: float) -> None:
        self._bursts.reset(t)

    def filter_signals(self, signals: dict[str, float | None], t: float) -> None:
        if self.active and self._bursts.on(t):
            for name in self.params.signals:
                signals[name] = None

    def references(self) -> tuple[list[str], list[str]]:
        return list(self.params.signals), []


class FrozenSensor(Scenario):
    name = "frozen_sensor"
    bit = 4
    summary = "Sensor output sticks at its last value while frames keep arriving"

    @dataclass(frozen=True)
    class Params:
        signals: tuple[str, ...]

        def __post_init__(self) -> None:
            object.__setattr__(self, "signals", _tuple_of_str(self.signals))

    def __init__(self, params: dict[str, Any], rng) -> None:
        super().__init__(params, rng)
        self._held: dict[str, float | None] = {}

    def on_activate(self, t: float) -> None:
        self._held = {}

    def on_deactivate(self, t: float) -> None:
        self._held = {}

    def filter_signals(self, signals: dict[str, float | None], t: float) -> None:
        if not self.active:
            return
        for name in self.params.signals:
            if name not in self._held:
                self._held[name] = signals[name]
            signals[name] = self._held[name]

    def references(self) -> tuple[list[str], list[str]]:
        return list(self.params.signals), []


class CanMessageTimeout(Scenario):
    name = "can_message_timeout"
    bit = 5
    summary = "The ECU stops transmitting selected messages entirely"

    @dataclass(frozen=True)
    class Params:
        messages: tuple[str, ...]

        def __post_init__(self) -> None:
            object.__setattr__(self, "messages", _tuple_of_str(self.messages))

    def drop_frame(self, message_name: str, t: float) -> bool:
        return self.active and message_name in self.params.messages

    def references(self) -> tuple[list[str], list[str]]:
        return [], list(self.params.messages)


class IntermittentCan(Scenario):
    name = "intermittent_can"
    bit = 6
    summary = "Loose CAN connector: random frame loss plus short total bus outages"

    @dataclass(frozen=True)
    class Params:
        drop_probability: float
        burst_gap_s: tuple[float, float]
        burst_duration_s: tuple[float, float]

        def __post_init__(self) -> None:
            if not 0.0 <= self.drop_probability <= 1.0:
                raise ValueError("drop_probability must be between 0 and 1")
            object.__setattr__(self, "burst_gap_s", float_pair(self.burst_gap_s, "burst_gap_s"))
            object.__setattr__(self, "burst_duration_s", float_pair(self.burst_duration_s, "burst_duration_s"))

    def __init__(self, params: dict[str, Any], rng) -> None:
        super().__init__(params, rng)
        self._bursts = BurstSchedule(rng, self.params.burst_gap_s, self.params.burst_duration_s)

    def on_activate(self, t: float) -> None:
        self._bursts.reset(t, first_gap=self.params.burst_gap_s)

    def drop_frame(self, message_name: str, t: float) -> bool:
        if not self.active:
            return False
        if self._bursts.on(t):
            return True
        return self.rng.random() < self.params.drop_probability


class EngineOverrev(Scenario):
    name = "engine_overrev"
    bit = 7
    summary = "Late upshifts past the normal limiter and occasional mis-shifts into a low gear"

    @dataclass(frozen=True)
    class Params:
        upshift_rpm: float
        rev_limit_rpm: float
        money_shift_gap_s: tuple[float, float]
        money_shift_duration_s: float
        money_shift_min_kmh: float
        money_shift_gear_drop: int

        def __post_init__(self) -> None:
            object.__setattr__(self, "money_shift_gap_s", float_pair(self.money_shift_gap_s, "money_shift_gap_s"))
            if self.money_shift_gear_drop < 1:
                raise ValueError("money_shift_gear_drop must be at least 1")

    def __init__(self, params: dict[str, Any], rng) -> None:
        super().__init__(params, rng)
        self._next_event = 0.0
        self._event_until = -1.0

    def on_activate(self, t: float) -> None:
        self._next_event = t + self.rng.uniform(2.0, 5.0)
        self._event_until = -1.0

    def apply_knobs(self, knobs: FaultKnobs, t: float, model: VehicleModel) -> None:
        if not self.active:
            return
        p = self.params
        knobs.upshift_rpm = p.upshift_rpm
        knobs.rev_limit_rpm = p.rev_limit_rpm
        if t >= self._next_event and t >= self._event_until:
            if model.speed_kmh >= p.money_shift_min_kmh and model.gear >= 3:
                self._event_until = t + p.money_shift_duration_s
                self._next_event = self._event_until + self.rng.uniform(*p.money_shift_gap_s)
        if t < self._event_until:
            knobs.forced_gear_drop = p.money_shift_gear_drop
