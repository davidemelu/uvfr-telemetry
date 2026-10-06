"""Fake Formula SAE ECU: puts SIMULATED vehicle traffic on a virtual CAN bus.

    python simulator/fake_ecu.py --scenario normal
    python simulator/fake_ecu.py --scenario overheating
    python simulator/fake_ecu.py --scenario low_oil_pressure,intermittent_can
    python -m simulator --list-scenarios

Switch scenarios while it runs:

    python -m simulator.ctl set overheating
    python -m simulator.ctl normal

SIMULATION ONLY. This program transmits CAN frames and must only ever be
pointed at a virtual bus (vcan*, udp_multicast or virtual), never at a car.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # allow `python simulator/fake_ecu.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import can  # noqa: E402

from common.canbus import check_virtual_bus, open_bus  # noqa: E402
from common.config import ConfigError, load_yaml  # noqa: E402
from common.control import ControlError, ControlServer  # noqa: E402
from simulator.scenarios import NORMAL, SCENARIOS, parse_scenarios  # noqa: E402
from simulator.simulation import Simulator  # noqa: E402

DEFAULT_CONFIG = "config/simulation.yaml"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fake_ecu",
        description="SIMULATED Formula SAE ECU on a virtual CAN bus.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        help="normal, or one or more fault scenarios (comma-separated or repeated). Default: normal",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"simulation config (default {DEFAULT_CONFIG})")
    parser.add_argument("--interface", help="python-can interface (overrides config)")
    parser.add_argument("--channel", help="CAN channel, e.g. vcan0 (overrides config)")
    parser.add_argument("--seed", type=int, help="random seed for a repeatable run")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--control-port", type=int, help="UDP control port (overrides config)")
    parser.add_argument("--no-control", action="store_true", help="disable runtime scenario control")
    parser.add_argument("--quiet", action="store_true", help="no periodic status line")
    parser.add_argument("--list-scenarios", action="store_true", help="list scenarios and exit")
    return parser.parse_args(argv)


def status_line(sim: Simulator, fps: float) -> str:
    s = sim.status()
    scen = "+".join(s["scenarios"]) or NORMAL
    gear = "N" if s["gear"] == 0 else str(s["gear"])
    where = "pits" if s["in_pits"] else f"lap {s['lap']} {s['segment']}"
    return (
        f"[{s['time_s']:8.1f}s] {where:<24} | {scen:<20} | gear {gear} {s['rpm']:5d} rpm "
        f"thr {s['throttle_pct']:5.1f}% {s['speed_kmh']:5.1f} km/h | "
        f"coolant {s['coolant_c']:5.1f} C oil {s['oil_kpa']:3d} kPa batt {s['battery_v']:5.2f} V | "
        f"{fps:5.0f} frames/s"
    )


class ControlHandler:
    def __init__(self, sim: Simulator) -> None:
        self.sim = sim

    def __call__(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        sim = self.sim
        if cmd == "status":
            return {"ok": True, **sim.status()}
        if cmd == "list":
            return {"ok": True, "scenarios": {n: c.summary for n, c in SCENARIOS.items()}}
        if cmd in ("set", "add", "remove", "normal"):
            requested = parse_scenarios(req.get("scenarios", []))
            current = sim.active_scenarios
            if cmd == "set":
                new = requested
            elif cmd == "add":
                new = current + [n for n in requested if n not in current]
            elif cmd == "remove":
                new = [n for n in current if n not in requested]
            else:
                new = []
            sim.set_scenarios(new)
            print(f"[{sim.time_s:8.1f}s] scenario -> {'+'.join(new) or NORMAL}", flush=True)
            return {"ok": True, "scenarios": sim.active_scenarios, "scenario_mask": sim.scenario_mask}
        raise ValueError(f"unknown command {cmd!r}; use status, list, set, add, remove or normal")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_scenarios:
        print(f"{NORMAL:<22} no faults")
        for name, cls in SCENARIOS.items():
            print(f"{name:<22} {cls.summary}")
        return 0

    try:
        cfg = load_yaml(args.config)
        sim = Simulator.from_config(cfg, seed=args.seed)
        sim.set_scenarios(parse_scenarios(args.scenario or [NORMAL]))
    except (ConfigError, ValueError) as exc:
        print(f"fake_ecu: {exc}", file=sys.stderr)
        return 2

    interface = args.interface or cfg["can"]["interface"]
    channel = args.channel or cfg["can"]["channel"]
    check_virtual_bus(interface, channel)

    try:
        bus = open_bus(interface, channel)
    except (OSError, can.CanError) as exc:
        print(f"fake_ecu: cannot open {interface}:{channel}: {exc}", file=sys.stderr)
        print("hint: run ./scripts/setup-vcan.sh (or `make vcan`) first", file=sys.stderr)
        return 1

    control = None
    if not args.no_control:
        host = cfg["control"]["host"]
        port = args.control_port if args.control_port is not None else cfg["control"]["port"]
        try:
            control = ControlServer(host, port, ControlHandler(sim))
        except ControlError as exc:
            print(f"fake_ecu: {exc} (another fake ECU running?)", file=sys.stderr)
            bus.shutdown()
            return 1

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    scen = "+".join(sim.active_scenarios) or NORMAL
    print(
        f"fake_ecu: SIMULATED ECU on {interface}:{channel} at {sim.physics_hz} Hz, scenario {scen}"
        + (f", control udp://{control.address[0]}:{control.address[1]}" if control else ""),
        flush=True,
    )

    print_every = float(cfg.get("status_print_interval_s", 1.0))
    next_print = time.monotonic() + print_every
    window_frames = 0
    tx_errors = 0
    start = time.monotonic()
    next_tick = start
    try:
        while not stopping:
            for frame in sim.step():
                msg = can.Message(arbitration_id=frame.arbitration_id, data=frame.data, is_extended_id=False)
                try:
                    bus.send(msg, timeout=0.05)
                    window_frames += 1
                except can.CanError:
                    tx_errors += 1
            if control:
                control.poll()

            now = time.monotonic()
            if now >= next_print:
                if not args.quiet:
                    print(status_line(sim, window_frames / print_every), flush=True)
                window_frames = 0
                next_print += print_every
            if args.duration is not None and sim.time_s >= args.duration:
                break

            next_tick += sim.dt
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.25:
                next_tick = time.monotonic()  # fell far behind (host paused); resync
    finally:
        if control:
            control.close()
        bus.shutdown()
    print(f"fake_ecu: stopped after {sim.time_s:.1f} s, transmit errors: {tx_errors}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
