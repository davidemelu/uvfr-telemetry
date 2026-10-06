"""Simulated radio link between the car node and the pit receiver.

    car_node --UDP--> link_sim --(impairment)--UDP--> pit_receiver

    python -m link_sim                       # profile from config/telemetry.yaml
    python -m link_sim --profile lora_bad
    python -m link_sim.ctl profile lora_marginal
    python -m link_sim.ctl set loss_pct=10 latency_ms=250
    python -m link_sim.ctl outage 5

It stands in for the pair of USB LoRa modems. Neither the car node nor the
pit receiver contains any impairment code, so removing this relay and pointing
both ends at real radios changes nothing else.
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from typing import Any

from common.config import ConfigError, check_keys, load_yaml
from common.control import ControlError, ControlServer
from common.transport import ImpairedTransport, Impairment, UdpTransport, parse_hostport

LINK_SIM_KEYS = {"listen", "forward_to", "control", "profile", "seed", "status_print_interval_s", "profiles"}


def load_profiles(raw: Any) -> dict[str, Impairment]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("telemetry.link_sim.profiles must be a non-empty mapping")
    return {name: Impairment.from_dict(params or {}, f"link_sim.profiles.{name}") for name, params in raw.items()}


def _coerce(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


class RadioRelay:
    def __init__(
        self,
        listen: tuple[str, int],
        forward_to: tuple[str, int],
        profiles: dict[str, Impairment],
        profile: str,
        rng: random.Random | None = None,
    ) -> None:
        if profile not in profiles:
            raise ConfigError(f"unknown link profile {profile!r}; available: {sorted(profiles)}")
        self.profiles = profiles
        self.profile = profile
        self.rx = UdpTransport(bind=listen)
        self.link = ImpairedTransport(UdpTransport(remote=forward_to), profiles[profile], rng=rng)
        self.forward_to = forward_to
        self.start = time.monotonic()
        self._mark = (self.start, self.link.link.as_dict())

    def pump_input(self, timeout: float) -> None:
        data = self.rx.recv(timeout)
        while data is not None:
            self.link.send(data)
            data = self.rx.recv(0)

    # ----------------------------------------------------------------- control
    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "profiles":
            return {"ok": True, "profiles": {n: p.as_dict() for n, p in self.profiles.items()}}
        if cmd == "profile":
            name = req.get("name")
            if name not in self.profiles:
                raise ValueError(f"unknown profile {name!r}; available: {sorted(self.profiles)}")
            self.profile = name
            self.link.set_impairment(self.profiles[name])
            self._log(f"profile -> {name}")
            return {"ok": True, "profile": name, "impairment": self.link.impairment.as_dict()}
        if cmd == "set":
            params = {k: _coerce(v) for k, v in (req.get("params") or {}).items()}
            merged = {**self.link.impairment.as_dict(), **params}
            impairment = Impairment.from_dict(merged, "link_sim set")
            self.profile = "custom"
            self.link.set_impairment(impairment)
            self._log(f"impairment -> {params}")
            return {"ok": True, "profile": self.profile, "impairment": impairment.as_dict()}
        if cmd == "outage":
            seconds = float(req.get("seconds", 5))
            if not 0 < seconds <= 3600:
                raise ValueError("outage seconds must be between 0 and 3600")
            self.link.force_outage(seconds)
            self._log(f"forced outage for {seconds:g} s")
            return {"ok": True, "outage_s": seconds}
        raise ValueError(f"unknown command {cmd!r}; use status, profiles, profile, set or outage")

    def _log(self, text: str) -> None:
        print(f"[{time.monotonic() - self.start:8.1f}s] {text}", flush=True)

    def status(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "impairment": self.link.impairment.as_dict(),
            "in_outage": self.link.in_outage(),
            "queue": self.link.queue_length,
            "link": self.link.link.as_dict(),
            "received_from_car": self.rx.stats.as_dict(),
            "forward_to": f"{self.forward_to[0]}:{self.forward_to[1]}",
        }

    def window(self) -> dict[str, float]:
        """Rates since the previous call."""
        now = time.monotonic()
        t0, prev = self._mark
        cur = self.link.link.as_dict()
        self._mark = (now, cur)
        dt = max(1e-9, now - t0)
        d = {k: cur[k] - prev[k] for k in cur}
        return {
            "in_pkt_s": d["offered"] / dt,
            "in_bytes_s": d["bytes_offered"] / dt,
            "out_pkt_s": d["delivered"] / dt,
            "out_bytes_s": d["bytes_delivered"] / dt,
            "dropped": d["dropped_loss"] + d["dropped_outage"] + d["dropped_queue"],
            "dropped_loss": d["dropped_loss"],
            "dropped_outage": d["dropped_outage"],
            "dropped_queue": d["dropped_queue"],
            "corrupted": d["corrupted"],
            # Airtime of the packets accepted for transmission, per second of
            # wall time. Above 100% the radio is being offered more than it
            # can send and its queue is growing.
            "radio_load_pct": 100.0 * d["airtime_s"] / dt,
        }

    def close(self) -> None:
        self.rx.close()
        self.link.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="link_sim", description="Simulated radio link (UDP relay with impairment).")
    parser.add_argument("--config", default="config/telemetry.yaml")
    parser.add_argument("--profile", help="impairment profile name (overrides config)")
    parser.add_argument("--listen", help="host:port to receive car packets on")
    parser.add_argument("--forward", help="host:port of the pit receiver")
    parser.add_argument("--control-port", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = check_keys(load_yaml(args.config).get("link_sim"), LINK_SIM_KEYS, "telemetry.link_sim")
        profiles = load_profiles(cfg["profiles"])
        listen = parse_hostport(args.listen or cfg["listen"])
        forward = parse_hostport(args.forward or cfg["forward_to"])
        control_host, control_port = parse_hostport(cfg["control"])
        seed = args.seed if args.seed is not None else cfg["seed"]
        relay = RadioRelay(listen, forward, profiles, args.profile or cfg["profile"], random.Random(seed))
    except (ConfigError, ValueError, OSError) as exc:
        print(f"link_sim: {exc}", file=sys.stderr)
        return 2

    try:
        port = args.control_port if args.control_port is not None else control_port
        control = ControlServer(control_host, port, relay.handle)
    except ControlError as exc:
        print(f"link_sim: {exc}", file=sys.stderr)
        relay.close()
        return 1

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(
        f"link_sim: SIMULATED radio udp://{listen[0]}:{listen[1]} -> udp://{forward[0]}:{forward[1]}, "
        f"profile {relay.profile}, control udp://{control.address[0]}:{control.address[1]}",
        flush=True,
    )
    every = float(cfg["status_print_interval_s"])
    next_print = time.monotonic() + every
    end = time.monotonic() + args.duration if args.duration else None
    try:
        while not stopping and (end is None or time.monotonic() < end):
            relay.pump_input(timeout=0.05)
            control.poll()
            if time.monotonic() >= next_print:
                next_print += every
                w = relay.window()
                if not args.quiet:
                    flag = " OUTAGE" if relay.link.in_outage() else ""
                    print(
                        f"[{time.monotonic() - relay.start:8.1f}s] {relay.profile:<14}{flag} | in {w['in_pkt_s']:5.1f} pkt/s "
                        f"{w['in_bytes_s']:6.1f} B/s | out {w['out_pkt_s']:5.1f} pkt/s | dropped loss {w['dropped_loss']} "
                        f"outage {w['dropped_outage']} queue {w['dropped_queue']} | corrupted {w['corrupted']} | "
                        f"queue {relay.link.queue_length} | radio load {w['radio_load_pct']:5.1f}%",
                        flush=True,
                    )
    finally:
        control.close()
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
