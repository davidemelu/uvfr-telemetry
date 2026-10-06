"""Fake ECU program: safety guard, runtime control and real traffic on vcan."""

from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter

import pytest

from common.canbus import open_bus
from common.config import REPO_ROOT
from common.dbc import decode_physical
from simulator.fake_ecu import ControlHandler, check_virtual_bus, main


@pytest.mark.parametrize(("interface", "channel"), [("socketcan", "can0"), ("socketcan", "slcan0"), ("pcan", "PCAN_USBBUS1")])
def test_refuses_to_transmit_on_a_real_bus(interface, channel):
    with pytest.raises(SystemExit, match="refusing"):
        check_virtual_bus(interface, channel)


@pytest.mark.parametrize(("interface", "channel"), [("socketcan", "vcan0"), ("socketcan", "vcan7"), ("udp_multicast", "vcan0"), ("virtual", "x")])
def test_allows_virtual_buses(interface, channel):
    check_virtual_bus(interface, channel)


def test_control_handler(make_sim):
    sim = make_sim()
    handle = ControlHandler(sim)
    assert handle({"cmd": "set", "scenarios": ["overheating"]})["scenarios"] == ["overheating"]
    assert handle({"cmd": "add", "scenarios": ["intermittent_can"]})["scenarios"] == ["overheating", "intermittent_can"]
    assert handle({"cmd": "remove", "scenarios": ["overheating"]})["scenarios"] == ["intermittent_can"]
    assert handle({"cmd": "normal"})["scenarios"] == []
    assert handle({"cmd": "status"})["ok"] is True
    assert "overheating" in handle({"cmd": "list"})["scenarios"]
    with pytest.raises(ValueError):
        handle({"cmd": "explode"})
    with pytest.raises(ValueError):
        handle({"cmd": "set", "scenarios": ["not_a_scenario"]})


def test_list_scenarios(capsys):
    assert main(["--list-scenarios"]) == 0
    out = capsys.readouterr().out
    assert "overheating" in out and "normal" in out


def test_bad_scenario_exits_with_error(capsys):
    assert main(["--scenario", "warp_drive", "--no-control"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


@pytest.mark.vcan
def test_traffic_on_vcan_matches_dbc_rates(can_channel, dbc):
    bus = open_bus("socketcan", can_channel)
    proc = subprocess.Popen(
        [sys.executable, "-m", "simulator", "--channel", can_channel, "--duration", "4",
         "--no-control", "--quiet", "--seed", "1"],
        cwd=REPO_ROOT,
    )
    try:
        counts: Counter[int] = Counter()
        first = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = bus.recv(timeout=0.5)
            if msg is None:
                if first is not None:
                    break
                continue
            now = time.monotonic()
            first = first or now
            if 0.5 <= now - first < 2.5:  # a clean 2 s window
                counts[msg.arbitration_id] += 1
                if msg.arbitration_id == 0x102:
                    values = decode_physical(dbc.get_message_by_frame_id(0x102), msg.data)
                    assert 60 < values["CoolantTemp"] < 100
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        bus.shutdown()

    for message in dbc.messages:
        expected = 2000 / message.cycle_time
        assert counts[message.frame_id] == pytest.approx(expected, rel=0.15, abs=1), message.name
