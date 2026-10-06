"""Simplified longitudinal, thermal and electrical model of an FSAE car.

SIMULATION ONLY. This is not a validated vehicle model and none of the numbers
come from the real UVFR car. It exists so simulated telemetry behaves like a
car on track instead of independent random numbers:

  throttle -> engine torque -> wheel force -> speed -> (gearing) -> RPM
  engine power -> coolant / cylinder head / oil temperature
  RPM + oil temperature -> oil pressure
  RPM + electrical loads -> battery voltage

Fault scenarios act on the model through FaultKnobs instead of overwriting
signals, so a fault propagates the way it would on a car: a failed fan raises
coolant temperature, which raises head and oil temperature, which lowers oil
pressure, and the fan's own current draw shows up on battery voltage.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass

from common.config import ConfigError, build_dataclass

G = 9.81
AIR_DENSITY = 1.2
POWER_REF_KW = 60.0  # power treated as "full load" for temperature rise


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _lag(current: float, target: float, tau_s: float, dt: float) -> float:
    """First-order lag (exact discretisation, stable for any dt)."""
    if tau_s <= 0:
        return target
    return target + (current - target) * math.exp(-dt / tau_s)


def _approach(current: float, target: float, max_step: float) -> float:
    if target > current:
        return min(target, current + max_step)
    return max(target, current - max_step)


@dataclass(frozen=True)
class EnvironmentParams:
    ambient_c: float
    initial_coolant_c: float
    initial_oil_c: float


@dataclass(frozen=True)
class VehicleParams:
    mass_kg: float
    wheel_radius_m: float
    cda_m2: float
    rolling_coeff: float
    tyre_mu: float
    driven_axle_load_fraction: float
    max_brake_decel_g: float
    driveline_efficiency: float


@dataclass(frozen=True)
class EngineParams:
    idle_rpm: float
    rev_limit_rpm: float
    launch_rpm: float
    peak_torque_nm: float
    peak_torque_rpm: float
    torque_curve_width_rpm: float
    friction_torque_nm: float
    friction_torque_per_krpm: float
    free_rev_time_constant_s: float


@dataclass(frozen=True)
class GearboxParams:
    primary_ratio: float
    final_drive_ratio: float
    gear_ratios: tuple[float, ...]
    upshift_rpm: float
    downshift_rpm: float
    shift_time_s: float

    def __post_init__(self) -> None:
        ratios = tuple(float(r) for r in self.gear_ratios)
        if not ratios or any(r <= 0 for r in ratios):
            raise ValueError("gear_ratios must be a non-empty list of positive numbers")
        object.__setattr__(self, "gear_ratios", ratios)


@dataclass(frozen=True)
class CoolingParams:
    heat_to_coolant_fraction: float
    idle_heat_kw: float
    thermal_capacity_kj_per_k: float
    thermostat_open_c: float
    thermostat_full_c: float
    thermostat_bypass_fraction: float
    radiator_ua_static_kw_per_k: float
    radiator_ua_per_mps_kw_per_k: float
    fan_ua_kw_per_k: float
    fan_on_c: float
    fan_off_c: float
    block_loss_kw_per_k: float
    head_offset_c: float
    head_load_rise_c: float
    head_time_constant_s: float
    oil_offset_c: float
    oil_load_rise_c: float
    oil_time_constant_s: float
    intake_heat_soak_c: float
    intake_time_constant_s: float
    boil_over_c: float  # coolant cannot run away past this: it boils and vents


@dataclass(frozen=True)
class OilParams:
    base_kpa: float
    kpa_per_rpm: float
    relief_kpa: float
    viscosity_coeff_per_c: float
    reference_temp_c: float
    time_constant_s: float


@dataclass(frozen=True)
class FuelParams:
    regulated_kpa: float
    flow_drop_kpa: float
    lambda_wot: float
    lambda_cruise: float
    lambda_overrun: float
    lambda_time_constant_s: float


@dataclass(frozen=True)
class ElectricalParams:
    battery_rest_v: float
    regulator_v: float
    alternator_cut_in_rpm: float
    base_load_a: float
    fan_load_a: float
    shift_load_a: float
    system_resistance_ohm: float
    discharge_v_per_s: float
    min_battery_v: float
    time_constant_s: float


@dataclass(frozen=True)
class SensorNoise:
    rpm: float
    throttle_pct: float
    oil_kpa: float
    fuel_kpa: float
    lambda_ratio: float
    coolant_c: float
    engine_c: float
    intake_c: float
    battery_v: float
    speed_kmh: float
    gps_speed_kmh: float
    gps_delay_s: float


@dataclass(frozen=True)
class ModelParams:
    environment: EnvironmentParams
    vehicle: VehicleParams
    engine: EngineParams
    gearbox: GearboxParams
    cooling: CoolingParams
    oil: OilParams
    fuel: FuelParams
    electrical: ElectricalParams
    sensor_noise: SensorNoise

    @classmethod
    def from_config(cls, cfg: dict) -> ModelParams:
        sections = {
            "environment": EnvironmentParams,
            "vehicle": VehicleParams,
            "engine": EngineParams,
            "gearbox": GearboxParams,
            "cooling": CoolingParams,
            "oil": OilParams,
            "fuel": FuelParams,
            "electrical": ElectricalParams,
            "sensor_noise": SensorNoise,
        }
        built = {}
        for key, section_cls in sections.items():
            if key not in cfg:
                raise ConfigError(f"simulation config: missing section '{key}'")
            built[key] = build_dataclass(section_cls, cfg[key], f"simulation.{key}")
        params = cls(**built)
        params.validate()
        return params

    def validate(self) -> None:
        e, gb, c = self.engine, self.gearbox, self.cooling
        if not e.idle_rpm < e.launch_rpm < e.rev_limit_rpm:
            raise ConfigError("engine: need idle_rpm < launch_rpm < rev_limit_rpm")
        if not gb.downshift_rpm < gb.upshift_rpm <= e.rev_limit_rpm:
            raise ConfigError("gearbox: need downshift_rpm < upshift_rpm <= engine.rev_limit_rpm")
        if not c.thermostat_open_c < c.thermostat_full_c:
            raise ConfigError("cooling: need thermostat_open_c < thermostat_full_c")
        if not c.fan_off_c < c.fan_on_c:
            raise ConfigError("cooling: need fan_off_c < fan_on_c")


@dataclass
class FaultKnobs:
    """Fault injection points. The defaults are a healthy car."""

    cooling_efficiency: float = 1.0  # radiator + fan effectiveness
    coolant_capacity_factor: float = 1.0  # < 1 means coolant has been lost
    oil_pump_factor: float = 1.0
    oil_slosh_drop: float = 0.0  # extra pressure loss under heavy braking
    alternator_output: float = 1.0
    upshift_rpm: float | None = None  # driver shift point override
    rev_limit_rpm: float | None = None  # limiter override
    forced_gear_drop: int = 0  # >0 during a mis-shift ("money shift")


class VehicleModel:
    def __init__(self, params: ModelParams, rng: random.Random) -> None:
        self.p = params
        self.rng = rng
        self.knobs = FaultKnobs()
        env, eng = params.environment, params.engine

        self.time_s = 0.0
        self.speed_mps = 0.0
        self.gear = 0
        self.rpm = eng.idle_rpm
        self.throttle = 0.0
        self.brake = 0.0
        self.clutch_locked = False
        self.shift_timer_s = 0.0
        self.shift_count = 0
        self.fuel_cut = False
        self.wheelspin = 0.0
        self.engine_torque_nm = 0.0
        self.engine_power_kw = 0.0

        self.coolant_c = env.initial_coolant_c
        self.head_c = env.initial_coolant_c + params.cooling.head_offset_c
        self.oil_c = env.initial_oil_c
        self.intake_c = env.ambient_c
        self.fan_on = False

        self.oil_kpa = params.oil.base_kpa + params.oil.kpa_per_rpm * eng.idle_rpm
        self.fuel_kpa = params.fuel.regulated_kpa
        self.lambda_ = params.fuel.lambda_cruise
        self.battery_ocv = params.electrical.battery_rest_v
        self.battery_v = params.electrical.regulator_v

        self.gps_satellites = 10
        self._gps_history: deque[tuple[float, float]] = deque([(0.0, 0.0)])
        self._next_sat_change_s = 5.0

    # ------------------------------------------------------------------ helpers
    def total_ratio(self, gear: int) -> float:
        gb = self.p.gearbox
        return gb.primary_ratio * gb.gear_ratios[gear - 1] * gb.final_drive_ratio

    def coupled_rpm(self, speed_mps: float, gear: int) -> float:
        wheel_rpm = speed_mps / (2 * math.pi * self.p.vehicle.wheel_radius_m) * 60.0
        return wheel_rpm * self.total_ratio(gear)

    @property
    def speed_kmh(self) -> float:
        return self.speed_mps * 3.6

    @property
    def shifting(self) -> bool:
        return self.shift_timer_s > 0

    # --------------------------------------------------------------------- step
    def step(self, dt: float, throttle_cmd: float, brake_cmd: float, drive: bool = True) -> None:
        self.time_s += dt
        # Throttle body opens fully in ~0.17 s and closes in ~0.1 s.
        throttle_cmd = _clamp(throttle_cmd, 0.0, 1.0)
        rate = 6.0 if throttle_cmd > self.throttle else 10.0
        self.throttle = _approach(self.throttle, throttle_cmd, rate * dt)
        self.brake = _clamp(brake_cmd, 0.0, 1.0)

        self._update_gear(dt, throttle_cmd, drive)
        self._update_powertrain(dt)
        self._update_thermal(dt)
        self._update_oil_fuel(dt)
        self._update_electrical(dt)
        self._update_gps(dt)

    def _upshift_rpm(self) -> float:
        return self.knobs.upshift_rpm or self.p.gearbox.upshift_rpm

    def _rev_limit(self) -> float:
        return self.knobs.rev_limit_rpm or self.p.engine.rev_limit_rpm

    def _start_shift(self) -> None:
        self.shift_timer_s = self.p.gearbox.shift_time_s
        self.shift_count += 1

    def _update_gear(self, dt: float, throttle_cmd: float, drive: bool) -> None:
        gb = self.p.gearbox
        if self.shift_timer_s > 0:
            self.shift_timer_s = max(0.0, self.shift_timer_s - dt)
            return
        if not drive:
            self.gear = 0
            return
        if self.knobs.forced_gear_drop:
            return  # mid mis-shift: the driver is not changing gear normally
        if self.gear == 0:
            if throttle_cmd > 0.05:
                self.gear = 1
            return
        upshift = self._upshift_rpm()
        if self.gear < len(gb.gear_ratios) and self.clutch_locked and self.rpm >= upshift:
            self.gear += 1
            self._start_shift()
        elif self.gear > 1 and self.rpm <= gb.downshift_rpm:
            if self.coupled_rpm(self.speed_mps, self.gear - 1) < upshift - 800:
                self.gear -= 1
                self._start_shift()

    def _update_powertrain(self, dt: float) -> None:
        p, k = self.p, self.knobs
        eng, veh = p.engine, p.vehicle
        rev_limit = self._rev_limit()

        gear = self.gear
        if gear > 0 and k.forced_gear_drop:
            gear = max(1, gear - k.forced_gear_drop)
        thr = self.throttle**0.6  # butterfly: small openings give a lot of torque
        v = self.speed_mps

        # Engine speed and clutch. A slipping clutch holds the engine near the
        # launch RPM until road speed catches up; once locked, RPM follows the
        # wheels (plus wheelspin) until the car is slow enough to declutch.
        if gear == 0:
            target = eng.idle_rpm + thr * (0.85 * rev_limit - eng.idle_rpm)
            self.rpm = _lag(self.rpm, target, eng.free_rev_time_constant_s, dt)
            self.clutch_locked = False
        else:
            coupled = self.coupled_rpm(v, gear)
            slip_target = eng.idle_rpm + thr * (eng.launch_rpm - eng.idle_rpm)
            if coupled >= slip_target or (self.clutch_locked and coupled >= eng.idle_rpm):
                self.clutch_locked = True
                self.rpm = _lag(self.rpm, coupled * (1.0 + self.wheelspin), 0.03, dt)
            else:
                self.clutch_locked = False
                self.rpm = _lag(self.rpm, max(slip_target, coupled), eng.free_rev_time_constant_s, dt)
        rpm = self.rpm

        # Torque: ignition/fuel cut on the limiter and during shifts, and
        # deceleration fuel cut on a closed throttle.
        cut = rpm >= rev_limit or self.shifting
        self.fuel_cut = cut or (self.throttle < 0.02 and self.clutch_locked and rpm > 3000)
        x = (rpm - eng.peak_torque_rpm) / eng.torque_curve_width_rpm
        torque_wot = eng.peak_torque_nm * max(0.35, 1.0 - x * x)
        torque = 0.0 if cut else thr * torque_wot
        friction = eng.friction_torque_nm + eng.friction_torque_per_krpm * rpm / 1000.0
        self.engine_torque_nm = torque
        self.engine_power_kw = torque * rpm * 2 * math.pi / 60.0 / 1000.0

        f_drive = 0.0
        target_spin = 0.0
        if gear > 0:
            wheel_torque = (torque - friction if self.clutch_locked else torque) * self.total_ratio(gear)
            f_drive = wheel_torque * veh.driveline_efficiency / veh.wheel_radius_m
            f_traction = veh.tyre_mu * veh.mass_kg * G * veh.driven_axle_load_fraction
            if f_drive > f_traction:
                target_spin = min(0.12, 0.08 * (f_drive / f_traction - 1.0))
                f_drive = f_traction
        self.wheelspin = _lag(self.wheelspin, target_spin, 0.15, dt)

        f_brake = self.brake * veh.max_brake_decel_g * G * veh.mass_kg if v > 0 else 0.0
        f_drag = 0.5 * AIR_DENSITY * veh.cda_m2 * v * v
        f_roll = veh.rolling_coeff * veh.mass_kg * G if v > 0.05 else 0.0
        accel = (f_drive - f_brake - f_drag - f_roll) / veh.mass_kg
        self.speed_mps = max(0.0, v + accel * dt)

    def _update_thermal(self, dt: float) -> None:
        p, k = self.p, self.knobs
        c, amb = p.cooling, p.environment.ambient_c
        load = min(1.0, self.engine_power_kw / POWER_REF_KW)

        q_gen = c.heat_to_coolant_fraction * self.engine_power_kw
        q_gen += c.idle_heat_kw * min(1.0, self.rpm / p.engine.idle_rpm)

        if self.coolant_c >= c.fan_on_c:
            self.fan_on = True
        elif self.coolant_c <= c.fan_off_c:
            self.fan_on = False
        opening = _clamp(
            (self.coolant_c - c.thermostat_open_c) / (c.thermostat_full_c - c.thermostat_open_c),
            c.thermostat_bypass_fraction,
            1.0,
        )
        ua = c.radiator_ua_static_kw_per_k + c.radiator_ua_per_mps_kw_per_k * self.speed_mps
        if self.fan_on:
            ua += c.fan_ua_kw_per_k
        ua *= opening * k.cooling_efficiency
        delta = self.coolant_c - amb
        q_out = ua * delta + c.block_loss_kw_per_k * delta
        capacity = c.thermal_capacity_kj_per_k * max(0.2, k.coolant_capacity_factor)
        self.coolant_c = min(c.boil_over_c, self.coolant_c + (q_gen - q_out) / capacity * dt)

        self.head_c = _lag(
            self.head_c, self.coolant_c + c.head_offset_c + c.head_load_rise_c * load, c.head_time_constant_s, dt
        )
        self.oil_c = _lag(
            self.oil_c, self.coolant_c + c.oil_offset_c + c.oil_load_rise_c * load, c.oil_time_constant_s, dt
        )
        soak = c.intake_heat_soak_c * math.exp(-self.speed_mps / 8.0)
        intake_target = amb + soak + 0.05 * max(0.0, self.coolant_c - 80.0)
        self.intake_c = _lag(self.intake_c, intake_target, c.intake_time_constant_s, dt)

    def _update_oil_fuel(self, dt: float) -> None:
        p, k = self.p, self.knobs
        o, f = p.oil, p.fuel
        viscosity = _clamp(1.0 + o.viscosity_coeff_per_c * (o.reference_temp_c - self.oil_c), 0.75, 1.3)
        # Cold oil raises pressure, but the relief valve caps it either way.
        healthy = min(o.relief_kpa, (o.base_kpa + o.kpa_per_rpm * self.rpm) * viscosity)
        target = healthy * k.oil_pump_factor
        if k.oil_slosh_drop and self.brake > 0.4:
            target *= 1.0 - k.oil_slosh_drop
        self.oil_kpa = _lag(self.oil_kpa, target, o.time_constant_s, dt)

        thr = self.throttle**0.6
        fuel_target = f.regulated_kpa - f.flow_drop_kpa * thr * self.rpm / p.engine.rev_limit_rpm
        self.fuel_kpa = _lag(self.fuel_kpa, fuel_target, 0.05, dt)

        if self.fuel_cut and self.rpm > 2500:
            lam = f.lambda_overrun
        elif self.throttle > 0.85:
            lam = f.lambda_wot
        else:  # closed-loop fuelling dithers around the target
            lam = f.lambda_cruise + 0.015 * math.sin(2 * math.pi * 1.3 * self.time_s)
        self.lambda_ = _lag(self.lambda_, lam, f.lambda_time_constant_s, dt)

    def _update_electrical(self, dt: float) -> None:
        e = self.p.electrical
        load_a = e.base_load_a
        if self.fan_on:
            load_a += e.fan_load_a
        if self.shifting:
            load_a += e.shift_load_a  # shift actuator solenoid
        if self.rpm >= e.alternator_cut_in_rpm:
            charge = min(1.0, 0.7 + 0.3 * (self.rpm - e.alternator_cut_in_rpm) / 3000.0)
        else:
            charge = 0.0
        alternator = charge * self.knobs.alternator_output
        if alternator < 0.5:  # the battery is carrying the car
            drain = e.discharge_v_per_s * dt * load_a / e.base_load_a
            self.battery_ocv = max(e.min_battery_v, self.battery_ocv - drain)
        else:
            self.battery_ocv = _lag(self.battery_ocv, e.battery_rest_v, 120.0, dt)
        source_v = alternator * e.regulator_v + (1.0 - alternator) * self.battery_ocv
        target_v = source_v - load_a * e.system_resistance_ohm
        self.battery_v = _lag(self.battery_v, target_v, e.time_constant_s, dt)

    def _update_gps(self, dt: float) -> None:
        delay = self.p.sensor_noise.gps_delay_s
        self._gps_history.append((self.time_s, self.speed_kmh))
        while len(self._gps_history) > 1 and self._gps_history[1][0] <= self.time_s - delay:
            self._gps_history.popleft()
        if self.time_s >= self._next_sat_change_s:
            self.gps_satellites = int(_clamp(self.gps_satellites + self.rng.choice((-1, 0, 1)), 8, 13))
            self._next_sat_change_s = self.time_s + self.rng.uniform(3.0, 8.0)

    # ------------------------------------------------------------------ outputs
    def signals(self) -> dict[str, float | None]:
        """Sensor readings keyed by DBC signal name (physical units, with noise)."""
        n = self.p.sensor_noise
        g = self.rng.gauss
        speed = self.speed_kmh
        gps_speed = self._gps_history[0][1]
        return {
            "EngineSpeed": max(0.0, self.rpm + g(0, n.rpm)),
            "ThrottlePosition": _clamp(self.throttle * 100 + g(0, n.throttle_pct), 0.0, 100.0),
            "Gear": float(self.gear),
            "OilPressure": max(0.0, self.oil_kpa + g(0, n.oil_kpa)),
            "FuelPressure": max(0.0, self.fuel_kpa + g(0, n.fuel_kpa)),
            "Lambda": max(0.0, self.lambda_ + g(0, n.lambda_ratio)),
            "CoolantTemp": self.coolant_c + g(0, n.coolant_c),
            "EngineTemp": self.head_c + g(0, n.engine_c),
            "IntakeAirTemp": self.intake_c + g(0, n.intake_c),
            "BatteryVoltage": max(0.0, self.battery_v + g(0, n.battery_v)),
            "VehicleSpeed": max(0.0, speed + g(0, n.speed_kmh)) if speed > 0 else 0.0,
            "GpsSpeed": max(0.0, gps_speed + g(0, n.gps_speed_kmh)) if gps_speed > 0.5 else 0.0,
            "GpsFix": 2.0,
            "GpsSatellites": float(self.gps_satellites),
        }
