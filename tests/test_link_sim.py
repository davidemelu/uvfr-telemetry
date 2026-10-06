"""Simulated radio relay: profiles, runtime control and real UDP forwarding."""

from __future__ import annotations

import random
import time

import pytest

from common.config import ConfigError, load_yaml
from common.transport import UdpTransport
from link_sim.relay import RadioRelay, load_profiles


@pytest.fixture(scope="module")
def profiles():
    return load_profiles(load_yaml("config/telemetry.yaml")["link_sim"]["profiles"])


@pytest.fixture
def relay(profiles):
    pit = UdpTransport(bind=("127.0.0.1", 0))
    r = RadioRelay(("127.0.0.1", 0), pit.local_address, profiles, "perfect", random.Random(1))
    yield r, pit
    r.close()
    pit.close()


def test_shipped_profiles(profiles):
    assert {"perfect", "lora_good", "lora_marginal", "lora_bad", "congested"} <= set(profiles)
    assert profiles["lora_marginal"].loss_pct == 5
    assert profiles["lora_bad"].outage_every_s > 0


def test_unknown_profile_rejected(profiles):
    with pytest.raises(ConfigError):
        RadioRelay(("127.0.0.1", 0), ("127.0.0.1", 9), profiles, "teleport")


def test_forwards_packets(relay):
    r, pit = relay
    car = UdpTransport(remote=r.rx.local_address)
    try:
        for i in range(10):
            car.send(bytes([i]) * 20)
        deadline = time.monotonic() + 2
        got = []
        while len(got) < 10 and time.monotonic() < deadline:
            r.pump_input(0.05)
            data = pit.recv(0.05)
            if data:
                got.append(data)
        assert got == [bytes([i]) * 20 for i in range(10)]
    finally:
        car.close()


def test_control_commands(relay):
    r, _ = relay
    assert r.handle({"cmd": "status"})["profile"] == "perfect"
    assert "lora_bad" in r.handle({"cmd": "profiles"})["profiles"]
    reply = r.handle({"cmd": "profile", "name": "lora_bad"})
    assert reply["impairment"]["loss_pct"] == 10
    reply = r.handle({"cmd": "set", "params": {"loss_pct": "2.5", "latency_ms": "40", "allow_reorder": "true"}})
    assert reply["profile"] == "custom"
    assert reply["impairment"]["loss_pct"] == 2.5 and reply["impairment"]["allow_reorder"] is True
    assert reply["impairment"]["outage_every_s"] == 30  # untouched keys keep the previous profile's value
    assert r.handle({"cmd": "outage", "seconds": 2})["outage_s"] == 2
    assert r.link.in_outage()


@pytest.mark.parametrize(
    "req",
    [{"cmd": "profile", "name": "nope"}, {"cmd": "set", "params": {"loss_pct": 150}},
     {"cmd": "set", "params": {"warp": 1}}, {"cmd": "outage", "seconds": -1}, {"cmd": "explode"}],
)
def test_bad_control_requests_raise(relay, req):
    r, _ = relay
    with pytest.raises((ValueError, ConfigError)):
        r.handle(req)
