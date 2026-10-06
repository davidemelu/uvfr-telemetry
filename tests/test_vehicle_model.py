"""The vehicle model behaves like a car: correlated, bounded and plausible."""

from __future__ import annotations

import statistics

import pytest


def record(sim, seconds: float, every: int = 1) -> list[dict]:
    rows = []
    for i in range(round(seconds * sim.physics_hz)):
        sim.step()
        if i % every == 0:
            m = sim.model
            rows.append(
                {
                    "t": sim.time_s,
                    "rpm": m.rpm,
                    "speed": m.speed_kmh,
                    "throttle": m.throttle,
                    "gear": m.gear,
                    "shifting": m.shifting,
                    "coolant": m.coolant_c,
                    "oil": m.oil_kpa,
                    "battery": m.battery_v,
                }
            )
    return rows


def test_sits_in_pits_then_pulls_away(make_sim):
    sim = make_sim()
    pits = record(sim, 7.9)
    assert all(r["gear"] == 0 and r["speed"] == 0 for r in pits)
    assert all(1700 < r["rpm"] < 8000 for r in pits)  # idle plus warm-up blips
    assert statistics.median(r["rpm"] for r in pits) == pytest.approx(1800, abs=100)
    on_track = record(sim, 5.0)
    assert on_track[-1]["speed"] > 30
    assert on_track[-1]["gear"] >= 1


def test_throttle_drives_rpm_and_speed(make_sim):
    model = make_sim().model
    dt = 0.01
    samples = []
    for _ in range(300):  # 3 s at full throttle from a standstill
        model.step(dt, throttle_cmd=1.0, brake_cmd=0.0, drive=True)
        samples.append((model.speed_kmh, model.rpm, model.shifting))
    assert samples[-1][0] > 40
    assert max(rpm for _, rpm, _ in samples) > 6000
    # Speed only ever dips during the 60 ms ignition cut of an upshift.
    for (v0, _, s0), (v1, _, s1) in zip(samples, samples[1:]):
        if not (s0 or s1):
            assert v1 >= v0 - 1e-9

    before = model.speed_kmh
    for _ in range(200):  # lift off for 2 s
        model.step(dt, throttle_cmd=0.0, brake_cmd=0.0, drive=True)
    assert model.speed_kmh < before


def test_brakes_slow_the_car_faster_than_coasting(make_sim):
    coast, brake = make_sim().model, make_sim().model
    for m in (coast, brake):
        for _ in range(300):
            m.step(0.01, 1.0, 0.0, True)
    for _ in range(100):
        coast.step(0.01, 0.0, 0.0, True)
        brake.step(0.01, 0.0, 1.0, True)
    assert brake.speed_kmh < coast.speed_kmh - 10


def test_rpm_follows_road_speed_through_the_gearbox(make_sim):
    sim = make_sim()
    sim.run_for(30)
    rows = record(sim, 60)
    checked = 0
    for r in rows:
        if r["gear"] >= 1 and r["speed"] > 35 and not r["shifting"]:
            expected = sim.model.coupled_rpm(r["speed"] / 3.6, r["gear"])
            assert r["rpm"] == pytest.approx(expected, rel=0.2)
            checked += 1
    assert checked > 1000


def test_oil_pressure_correlates_with_rpm(make_sim):
    sim = make_sim()
    sim.run_for(60)
    rows = record(sim, 120, every=10)
    corr = statistics.correlation([r["rpm"] for r in rows], [r["oil"] for r in rows])
    assert corr > 0.8


def test_normal_session_stays_in_healthy_ranges(make_sim):
    sim = make_sim()
    rows = record(sim, 300, every=10)
    warm = [r for r in rows if r["t"] > 90]
    assert 80 < min(r["coolant"] for r in warm)
    assert max(r["coolant"] for r in warm) < 96
    assert all(12.8 < r["battery"] < 14.6 for r in rows)
    assert max(r["rpm"] for r in rows) <= 13500 + 50
    assert min(r["oil"] for r in rows if r["rpm"] > 3000) > 170  # alarm threshold is 150
    assert max(r["speed"] for r in rows) < 130
    assert len({r["gear"] for r in rows if r["gear"] > 0}) >= 3
    assert sim.driver.lap >= 3


def test_coolant_responds_to_engine_load(make_sim):
    idling = make_sim(overrides={"driver": {"pit_idle_s": 10_000}})
    racing = make_sim()
    idling.run_for(90)
    racing.run_for(90)
    assert racing.model.coolant_c > idling.model.coolant_c + 5


def test_gps_speed_roughly_matches_wheel_speed(make_sim):
    sim = make_sim()
    sim.run_for(60)
    diffs = []
    for _ in range(3000):
        sim.step()
        sig = sim.model.signals()
        diffs.append(abs(sig["GpsSpeed"] - sig["VehicleSpeed"]))
    assert statistics.mean(diffs) < 3.0  # km/h; GPS lags 150 ms and is noisier


def test_seed_makes_runs_repeatable(make_sim):
    a = make_sim(seed=7).run_for(20)
    b = make_sim(seed=7).run_for(20)
    c = make_sim(seed=8).run_for(20)
    assert a == b
    assert a != c
