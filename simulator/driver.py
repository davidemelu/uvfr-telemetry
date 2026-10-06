"""A simple simulated driver lapping a configurable track.

The track is a loop of segments, each with a length and a target speed
(straights have high targets, corners low ones). The driver:

  * idles in the pits (neutral, occasional throttle blips) for pit_idle_s,
  * accelerates towards each segment's target speed,
  * looks ahead and brakes so it reaches the next segment's target speed
    in time, the way a driver brakes for a corner,
  * holds part throttle through corners.

The result is a believable FSAE-style lap: full-throttle bursts, hard braking,
mid-corner part throttle, gear changes, and RPM, speed and lambda that all
move together.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from common.config import ConfigError, build_dataclass

G = 9.81


@dataclass(frozen=True)
class TrackSegment:
    name: str
    length_m: float
    target_kmh: float

    def __post_init__(self) -> None:
        if self.length_m <= 0 or self.target_kmh <= 0:
            raise ValueError(f"segment {self.name!r}: length_m and target_kmh must be positive")


@dataclass(frozen=True)
class DriverParams:
    pit_idle_s: float
    planned_brake_decel_g: float
    throttle_noise: float
    track: tuple[TrackSegment, ...]

    @classmethod
    def from_config(cls, data: dict) -> DriverParams:
        if not isinstance(data, dict) or "track" not in data:
            raise ConfigError("simulation.driver: missing 'track'")
        raw = dict(data)
        segments = []
        for i, seg in enumerate(raw.pop("track") or []):
            segments.append(build_dataclass(TrackSegment, seg, f"simulation.driver.track[{i}]"))
        if not segments:
            raise ConfigError("simulation.driver.track: needs at least one segment")
        raw["track"] = tuple(segments)
        return build_dataclass(cls, raw, "simulation.driver")

    @property
    def lap_length_m(self) -> float:
        return sum(s.length_m for s in self.track)


@dataclass(frozen=True)
class DriverCommand:
    throttle: float
    brake: float
    drive: bool  # False = neutral (sitting in the pits)


class Driver:
    def __init__(self, params: DriverParams, rng: random.Random) -> None:
        self.p = params
        self.rng = rng
        self.segment_index = 0
        self.segment_pos_m = 0.0
        self.lap = 0
        self.distance_m = 0.0
        self.in_pits = True
        self._noise = 0.0

    @property
    def segment(self) -> TrackSegment:
        return self.p.track[self.segment_index]

    def _advance(self, ds: float) -> None:
        self.distance_m += ds
        self.segment_pos_m += ds
        while self.segment_pos_m >= self.segment.length_m:
            self.segment_pos_m -= self.segment.length_m
            self.segment_index += 1
            if self.segment_index == len(self.p.track):
                self.segment_index = 0
                self.lap += 1

    def update(self, t: float, dt: float, speed_mps: float) -> DriverCommand:
        if t < self.p.pit_idle_s:
            self.in_pits = True
            # Warm-up blips every 4 s while waiting in neutral.
            blip = 0.3 if t > 2.0 and (t % 4.0) < 0.35 else 0.0
            return DriverCommand(throttle=blip, brake=0.0, drive=False)
        self.in_pits = False
        self._advance(speed_mps * dt)

        # Slowly wandering throttle error, like a human foot.
        target_noise = self.rng.gauss(0.0, self.p.throttle_noise)
        self._noise += (target_noise - self._noise) * (1 - math.exp(-dt / 0.3))

        track = self.p.track
        seg = track[self.segment_index]
        nxt = track[(self.segment_index + 1) % len(track)]
        v = speed_mps
        v_target = seg.target_kmh / 3.6
        v_next = nxt.target_kmh / 3.6
        remaining = seg.length_m - self.segment_pos_m
        decel = self.p.planned_brake_decel_g * G

        if v > v_next + 0.3:
            braking_distance = (v * v - v_next * v_next) / (2 * decel)
            if remaining <= braking_distance + v * 0.1:
                needed = (v * v - v_next * v_next) / (2 * max(remaining, 0.5))
                return DriverCommand(0.0, min(1.0, max(0.15, needed / (decel * 1.25))), True)
        if v > v_target + 1.0:
            return DriverCommand(0.0, min(0.5, max(0.05, (v - v_target) / 8.0)), True)
        throttle = 0.28 + 0.4 * (v_target - v) + self._noise
        return DriverCommand(min(1.0, max(0.0, throttle)), 0.0, True)
