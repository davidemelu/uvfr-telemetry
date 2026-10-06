"""Deterministic simulation core: vehicle model + driver + scenarios + CAN schedule.

No I/O happens here. Simulator.step() advances one physics tick and returns
the CAN frames that are due, so tests can run minutes of simulated driving in
seconds and the real-time loop in fake_ecu.py stays small.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from cantools.database.can import Database, Message

from common.config import ConfigError, check_keys
from common.dbc import counter_signal, encode_physical, load_dbc
from simulator.driver import Driver, DriverParams
from simulator.scenarios import SCENARIOS, Scenario, mask_for
from simulator.vehicle_model import FaultKnobs, ModelParams, VehicleModel

TOP_LEVEL_KEYS = {
    "can",
    "dbc",
    "physics_hz",
    "seed",
    "control",
    "status_print_interval_s",
    "environment",
    "vehicle",
    "engine",
    "gearbox",
    "cooling",
    "oil",
    "fuel",
    "electrical",
    "sensor_noise",
    "driver",
    "scenarios",
}
SIM_STATUS_SIGNALS = ("SimScenarioMask", "SimElapsed")


class Frame(NamedTuple):
    arbitration_id: int
    data: bytes
    name: str


@dataclass
class TxMessage:
    message: Message
    period_ticks: int
    phase: int
    counter_name: str | None
    counter: int = 0
    sent: int = 0
    dropped: int = 0


@dataclass
class Simulator:
    db: Database
    model: VehicleModel
    driver: Driver
    scenarios: dict[str, Scenario]
    physics_hz: int
    tx: list[TxMessage] = field(default_factory=list)
    tick: int = 0

    # ---------------------------------------------------------------- creation
    @classmethod
    def from_config(cls, cfg: dict[str, Any], seed: int | None = None) -> Simulator:
        check_keys(cfg, TOP_LEVEL_KEYS, "simulation.yaml")
        rng = random.Random(seed if seed is not None else cfg.get("seed"))
        db = load_dbc(cfg["dbc"])
        model = VehicleModel(ModelParams.from_config(cfg), rng)
        driver = Driver(DriverParams.from_config(cfg["driver"]), rng)
        scenario_cfg = check_keys(cfg["scenarios"], set(SCENARIOS), "simulation.scenarios")
        scenarios = {name: SCENARIOS[name](scenario_cfg[name], rng) for name in SCENARIOS}
        physics_hz = int(cfg["physics_hz"])
        sim = cls(db=db, model=model, driver=driver, scenarios=scenarios, physics_hz=physics_hz)
        sim._build_schedule()
        sim._validate_references()
        return sim

    @property
    def dt(self) -> float:
        return 1.0 / self.physics_hz

    @property
    def time_s(self) -> float:
        return self.tick / self.physics_hz

    def _build_schedule(self) -> None:
        tick_ms = 1000.0 / self.physics_hz
        provided = set(self.model.signals()) | set(SIM_STATUS_SIGNALS)
        for index, message in enumerate(sorted(self.db.messages, key=lambda m: m.frame_id)):
            if not message.cycle_time:
                raise ConfigError(f"DBC message {message.name} has no GenMsgCycleTime")
            period_ticks = round(message.cycle_time / tick_ms)
            if period_ticks < 1 or abs(period_ticks * tick_ms - message.cycle_time) > 1e-6:
                raise ConfigError(
                    f"{message.name}: cycle time {message.cycle_time} ms is not a multiple of the "
                    f"{tick_ms:g} ms physics tick"
                )
            counter = counter_signal(message)
            for sig in message.signals:
                if sig is not counter and sig.name not in provided:
                    raise ConfigError(f"DBC signal {message.name}.{sig.name} is not produced by the vehicle model")
            self.tx.append(
                TxMessage(
                    message=message,
                    period_ticks=period_ticks,
                    phase=index % period_ticks,  # spread frames across ticks
                    counter_name=counter.name if counter else None,
                )
            )

    def _validate_references(self) -> None:
        signals = {s.name for m in self.db.messages for s in m.signals}
        messages = {m.name for m in self.db.messages}
        for scenario in self.scenarios.values():
            sig_refs, msg_refs = scenario.references()
            for name in sig_refs:
                if name not in signals:
                    raise ConfigError(f"scenario {scenario.name}: unknown DBC signal {name!r}")
            for name in msg_refs:
                if name not in messages:
                    raise ConfigError(f"scenario {scenario.name}: unknown DBC message {name!r}")

    # --------------------------------------------------------------- scenarios
    @property
    def active_scenarios(self) -> list[str]:
        return [name for name, sc in self.scenarios.items() if sc.active]

    @property
    def scenario_mask(self) -> int:
        return mask_for(self.active_scenarios)

    def set_scenarios(self, names: list[str]) -> None:
        unknown = [n for n in names if n not in self.scenarios]
        if unknown:
            raise ValueError(f"unknown scenario(s): {unknown}")
        for name, scenario in self.scenarios.items():
            scenario.set_active(name in names, self.time_s)

    # -------------------------------------------------------------------- step
    def step(self) -> list[Frame]:
        dt, t = self.dt, self.time_s
        knobs = FaultKnobs()
        for scenario in self.scenarios.values():
            scenario.update(t, dt)
            scenario.apply_knobs(knobs, t, self.model)
        self.model.knobs = knobs

        cmd = self.driver.update(t, dt, self.model.speed_mps)
        self.model.step(dt, cmd.throttle, cmd.brake, cmd.drive)
        self.tick += 1
        t = self.time_s

        due = [m for m in self.tx if (self.tick - m.phase) % m.period_ticks == 0]
        if not due:
            return []
        signals = self.model.signals()
        signals["SimScenarioMask"] = float(self.scenario_mask)
        signals["SimElapsed"] = float(int(t) % 65536)
        for scenario in self.scenarios.values():
            scenario.filter_signals(signals, t)

        frames: list[Frame] = []
        for tx in due:
            # The counter advances even for frames lost on the bus, exactly
            # like a real ECU: the receiver then sees a gap in the sequence.
            tx.counter = (tx.counter + 1) % 16
            if tx.counter_name:
                signals[tx.counter_name] = float(tx.counter)
            if any(sc.drop_frame(tx.message.name, t) for sc in self.scenarios.values()):
                tx.dropped += 1
                continue
            data = encode_physical(tx.message, signals)
            frames.append(Frame(tx.message.frame_id, data, tx.message.name))
            tx.sent += 1
        return frames

    def run_for(self, seconds: float) -> list[Frame]:
        """Advance quickly (not real time); returns every frame produced."""
        frames: list[Frame] = []
        for _ in range(round(seconds * self.physics_hz)):
            frames.extend(self.step())
        return frames

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        m = self.model
        return {
            "time_s": round(self.time_s, 2),
            "lap": self.driver.lap,
            "segment": self.driver.segment.name,
            "in_pits": self.driver.in_pits,
            "scenarios": self.active_scenarios,
            "scenario_mask": self.scenario_mask,
            "rpm": round(m.rpm),
            "gear": m.gear,
            "throttle_pct": round(m.throttle * 100, 1),
            "speed_kmh": round(m.speed_kmh, 1),
            "coolant_c": round(m.coolant_c, 1),
            "oil_kpa": round(m.oil_kpa),
            "battery_v": round(m.battery_v, 2),
            "frames_sent": {tx.message.name: tx.sent for tx in self.tx},
            "frames_dropped": {tx.message.name: tx.dropped for tx in self.tx},
        }
