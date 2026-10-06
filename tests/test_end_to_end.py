"""End to end: fake ECU -> vcan -> car node -> simulated radio -> pit receiver -> InfluxDB.

Five independent processes, exactly as in the demo, except that they use the
test bus (vcan1), private ports and a temporary InfluxDB bucket. The car
starts warm with the overheating fault active, so within about 40 seconds the
coolant alarm must reach CRITICAL and every stage must have left its mark in
the database.
"""

from __future__ import annotations

import copy
import os
import socket
import subprocess
import sys
import time

import pytest
import yaml

from common.config import REPO_ROOT, load_yaml

RUN_S = 40


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
@pytest.mark.vcan
@pytest.mark.influx
def test_fake_ecu_to_influxdb(can_channel, influx, tmp_path):
    sim = copy.deepcopy(load_yaml("config/simulation.yaml"))
    sim["environment"]["initial_coolant_c"] = 100.0  # warm car: alarms within the test window
    sim["driver"]["pit_idle_s"] = 2.0
    sim_path = tmp_path / "simulation.yaml"
    sim_path.write_text(yaml.safe_dump(sim), encoding="utf-8")

    radio, pit = free_udp_port(), free_udp_port()
    env = {**os.environ, "INFLUX_BUCKET": influx["bucket"], "PIT_INFLUX_TOKEN": influx["token"],
           "INFLUX_URL": influx["url"], "INFLUX_ORG": influx["org"]}
    py = sys.executable
    commands = [
        [py, "-m", "pit_receiver", "--listen", f"127.0.0.1:{pit}", "--duration", str(RUN_S + 2), "--quiet"],
        [py, "-m", "link_sim", "--profile", "lora_good", "--listen", f"127.0.0.1:{radio}", "--forward",
         f"127.0.0.1:{pit}", "--control-port", "0", "--seed", "1", "--duration", str(RUN_S + 1), "--quiet"],
        [py, "-m", "car_node", "--channel", can_channel, "--remote", f"127.0.0.1:{radio}",
         "--duration", str(RUN_S), "--quiet"],
        [py, "-m", "simulator", "--config", str(sim_path), "--channel", can_channel, "--scenario", "overheating",
         "--no-control", "--quiet", "--seed", "1", "--duration", str(RUN_S)],
    ]
    procs = []
    try:
        for cmd in commands:
            procs.append(subprocess.Popen(cmd, cwd=REPO_ROOT, env=env, stdin=subprocess.DEVNULL))
            time.sleep(0.3)
        for p in procs:
            assert p.wait(timeout=RUN_S + 30) == 0, p.args
    finally:
        for p in procs:
            p.kill()

    bucket, q = influx["bucket"], influx["query"]

    def values(measurement: str, field: str, extra: str = "") -> list[str]:
        rows = q(f'from(bucket: "{bucket}") |> range(start: -5m) '
                 f'|> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}"{extra})')
        return [r["_value"] for r in rows if r.get("_value") not in (None, "")]

    rpm = [float(v) for v in values("vehicle", "rpm")]
    coolant = [float(v) for v in values("vehicle", "coolant_temperature")]
    assert len(rpm) > 300, "about 10 rpm samples per second should reach the database"
    assert max(rpm) > 6000, "the car drove away from the pits"
    assert len(coolant) > 100
    assert max(coolant) > 110, "overheating must push coolant past the critical threshold"

    severities = values("alert", "severity", ' and r.rule == "coolant_high"')
    assert "WARNING" in severities and "CRITICAL" in severities
    coolant_states = values("channel_state", "status", ' and r.channel == "coolant_temperature"')
    assert "CRITICAL" in coolant_states
    assert "CRITICAL" in values("alerts_active", "vehicle_status")

    loss = [float(v) for v in values("link", "loss_pct")]
    assert loss and loss[-1] < 6.0, "lora_good drops about 1% of packets"
    assert "true" in values("car_status", "config_match")
    assert values("sim", "scenario")[-1] == "overheating"

    encoded = [float(v) for v in values("bandwidth", "encoded_bits_per_s")]
    steady = sorted(encoded)[len(encoded) // 2]
    assert 1800 < steady < 2400, "the measured stream should be about 2.2 kbit/s"
