"""Pit telemetry receiver.

    python -m pit_receiver                  # wiring from config/telemetry.yaml
    python -m pit_receiver --listen 127.0.0.1:47002 --jsonl logs/points.jsonl

For every packet that arrives it validates and decodes it, tracks sequence
gaps, packet loss, rate and staleness, maps the car's timestamp onto the pit
clock, and keeps the latest value of every channel. Once per health interval
it emits link health and per-channel state. Everything leaves as Points
through sinks (InfluxDB, JSON lines, ...), so the receiver has no database
code of its own.

It runs as its own process and shares nothing with the car node except the
protocol definition and channels.yaml.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from typing import Any

from common.config import ConfigError, check_keys, load_yaml, resolve_path
from common.protocol import ChannelLayout, Codec, DecodeError, Missing, StatusPacket, TelemetryPacket
from common.transport import transport_from_config
from pit_receiver.channels import ChannelTracker, StalenessConfig
from pit_receiver.clock import SourceClock
from pit_receiver.link_stats import LinkSnapshot, LinkStats, LinkThresholds
from pit_receiver.points import Point
from pit_receiver.sinks import JsonlSink, Sink
from pit_receiver.status import Status

PIT_KEYS = {
    "transport",
    "car_id",
    "health_interval_s",
    "rate_window_s",
    "loss_window_packets",
    "clock_window_s",
    "status_print_interval_s",
    "outputs",
}
LAB_ONLY_PREFIX = "sim_"  # channels.yaml convention: lab-only channels


def scenario_label(mask: int) -> str:
    """Names of the simulated scenarios in a SimScenarioMask (lab only)."""
    if mask == 0:
        return "normal"
    try:
        from simulator.scenarios import names_from_mask
    except ImportError:  # pit laptop without the simulator package
        return f"mask {mask:#04x}"
    return "+".join(names_from_mask(mask)) or f"mask {mask:#04x}"


class PitReceiver:
    def __init__(
        self,
        layout: ChannelLayout,
        link_thresholds: LinkThresholds,
        staleness: StalenessConfig,
        *,
        car_id: str = "uvfr",
        sinks: list[Sink] | None = None,
        rate_window_s: float = 5.0,
        loss_window_packets: int = 100,
        clock_window_s: float = 30.0,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.layout = layout
        self.codec = Codec(layout)
        self.link = LinkStats(link_thresholds, rate_window_s, loss_window_packets)
        self.clock = SourceClock(clock_window_s)
        self.channels = ChannelTracker(layout, staleness)
        self.car_id = car_id
        self.sinks = sinks or []
        self.wall = wall
        self.mono = mono
        self.last_status: StatusPacket | None = None
        self._vehicle_messages = [
            m for m in layout.messages
            if any(c.message == m and not c.name.startswith(LAB_ONLY_PREFIX) for c in layout.channels)
        ]

    @property
    def tags(self) -> dict[str, str]:
        return {"car": self.car_id}

    def _emit(self, points: list[Point]) -> list[Point]:
        for sink in self.sinks:
            try:
                sink.write(points)
            except Exception as exc:  # a broken sink must not stop telemetry
                print(f"pit_receiver: sink {sink.name} failed: {exc}", file=sys.stderr)
        return points

    # ------------------------------------------------------------ per packet
    def on_datagram(self, data: bytes, wall: float | None = None, mono: float | None = None) -> list[Point]:
        wall = self.wall() if wall is None else wall
        mono = self.mono() if mono is None else mono
        try:
            packet = self.codec.decode(data)
        except DecodeError as exc:
            self.link.record_rejected(exc.reason, mono, len(data))
            return []
        kind = self.link.record_packet(packet, mono, len(data))
        if kind in ("duplicate", "too_old"):
            return []
        if kind == "new_session":
            self.clock.reset()
        measured_at = self.clock.observe(packet.header.timestamp_ms, wall)
        time_ns = int(measured_at * 1e9)

        if isinstance(packet, TelemetryPacket):
            self.channels.update(packet.values, measured_at, mono)
            fields = {k: float(v) for k, v in packet.values.items() if not isinstance(v, Missing)}
            if not fields:
                return []
            return self._emit([Point("vehicle", fields, self.tags, time_ns)])

        self.last_status = packet
        s = packet.status
        fields: dict[str, Any] = {
            "config_match": packet.config_match,
            "tx_packets": s.tx_packets,
            "can_frames": s.can_frames,
            "can_fps": s.can_fps,
            "can_counter_gaps": s.can_counter_gaps,
            "can_errors": s.can_errors,
        }
        if s.cpu_pct is not None:
            fields["cpu_pct"] = s.cpu_pct
        worst = None
        for name, age in zip(self.layout.messages, s.message_ages_ms):
            if age is None:
                continue
            fields[f"can_age_{name}_ms"] = float(age)
            if name in self._vehicle_messages:
                worst = age if worst is None else max(worst, age)
        if worst is not None:
            fields["can_age_max_ms"] = float(worst)
        return self._emit([Point("car_status", fields, self.tags, time_ns)])

    # ---------------------------------------------------------- once a second
    def health(self, wall: float | None = None, mono: float | None = None) -> tuple[LinkSnapshot, list[Point]]:
        wall = self.wall() if wall is None else wall
        mono = self.mono() if mono is None else mono
        time_ns = int(wall * 1e9)
        snap = self.link.snapshot(mono)
        link_fields = snap.fields()
        link_fields["delay_excess_ms"] = round(self.clock.take_max_excess_delay() * 1000, 1)
        points = [Point("link", link_fields, self.tags, time_ns)]

        for name, view in self.channels.views(mono).items():
            fields: dict[str, Any] = {"status": view.status.label, "status_code": int(view.status)}
            if view.age_s is not None:
                fields["age_ms"] = round(view.age_s * 1000, 1)
            if view.last_value is not None:
                fields["last_value"] = float(view.last_value)
            points.append(Point("channel_state", fields, {**self.tags, "channel": name}, time_ns))

        sim = self.channels.view("sim_scenario", mono) if "sim_scenario" in self.channels.readings else None
        if sim is not None and sim.value is not None:
            mask = int(sim.value)
            points.append(Point("sim", {"scenario": scenario_label(mask), "scenario_mask": mask}, self.tags, time_ns))
        return snap, self._emit(points)

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception as exc:
                print(f"pit_receiver: closing sink {sink.name} failed: {exc}", file=sys.stderr)


def console_line(rx: PitReceiver, snap: LinkSnapshot) -> str:
    views = rx.channels.views(rx.mono())

    def show(name: str, fmt: str) -> str:
        v = views.get(name)
        if v is None:
            return "-"
        return format(v.value, fmt) if v.value is not None else v.status.label

    not_live = [n for n, v in views.items() if v.status is not Status.NORMAL]
    age = "never" if snap.last_packet_age_s is None else f"{snap.last_packet_age_s * 1000:.0f} ms"
    session = f"{snap.session:#04x}" if snap.session is not None else "-"
    return (
        f"LINK {snap.status.label:<8} | {snap.packet_rate:5.1f} pkt/s {snap.byte_rate:6.1f} B/s | "
        f"loss {snap.loss_pct:5.1f}% (recent {snap.loss_pct_window:5.1f}%) missing {snap.packets_missing} "
        f"rejected {sum(snap.rejected.values())} | last packet {age} | session {session} up {snap.session_uptime_s:.0f} s | "
        f"rpm {show('rpm', '.0f')} coolant {show('coolant_temperature', '.1f')} oil {show('oil_pressure', '.0f')} "
        f"batt {show('battery_voltage', '.2f')} | "
        f"{'not live: ' + ', '.join(not_live) if not_live else 'all channels live'}"
    )


def build_sinks(outputs: dict[str, Any], jsonl_override: str | None) -> list[Sink]:
    check_keys(outputs, {"jsonl"}, "telemetry.pit_receiver.outputs")
    sinks: list[Sink] = []
    jsonl = jsonl_override or outputs.get("jsonl")
    if jsonl:
        sinks.append(JsonlSink(resolve_path(jsonl)))
    return sinks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pit_receiver", description="Pit telemetry receiver.")
    parser.add_argument("--config", default="config/telemetry.yaml")
    parser.add_argument("--channels", default="config/channels.yaml")
    parser.add_argument("--alerts", default="config/alerts.yaml")
    parser.add_argument("--listen", help="UDP host:port to receive on (overrides config)")
    parser.add_argument("--jsonl", help="also write every point to this JSON-lines file")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = check_keys(load_yaml(args.config).get("pit_receiver"), PIT_KEYS, "telemetry.pit_receiver")
        alerts = load_yaml(args.alerts)
        layout = ChannelLayout.load(args.channels)
        transport_cfg = {"type": "udp", "listen": args.listen} if args.listen else cfg["transport"]
        transport = transport_from_config(transport_cfg, "telemetry.pit_receiver.transport", role="receiver")
        sinks = build_sinks(cfg["outputs"] or {}, args.jsonl)
        rx = PitReceiver(
            layout,
            LinkThresholds.from_config(alerts.get("link")),
            StalenessConfig.from_config(alerts.get("channel_staleness")),
            car_id=str(cfg["car_id"]),
            sinks=sinks,
            rate_window_s=float(cfg["rate_window_s"]),
            loss_window_packets=int(cfg["loss_window_packets"]),
            clock_window_s=float(cfg["clock_window_s"]),
        )
    except (ConfigError, ValueError, OSError) as exc:
        print(f"pit_receiver: {exc}", file=sys.stderr)
        return 2

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    print(
        f"pit_receiver: {transport.describe()} | {len(layout.channels)} channels, layout hash "
        f"{layout.config_hash:#06x} | outputs: {', '.join(s.name for s in sinks) or 'console only'}",
        flush=True,
    )

    health_every = float(cfg["health_interval_s"])
    print_every = float(cfg["status_print_interval_s"])
    start = rx.mono()
    next_health = start + health_every
    next_print = start + print_every
    end = start + args.duration if args.duration else None
    try:
        while not stopping and (end is None or rx.mono() < end):
            data = transport.recv(timeout=max(0.0, min(0.1, next_health - rx.mono())))
            if data is not None:
                rx.on_datagram(data)
            now = rx.mono()
            if now >= next_health:
                next_health += health_every
                if now - next_health > 5:
                    next_health = now + health_every
                snap, _ = rx.health()
                for sink in rx.sinks:
                    sink.flush()
                if now >= next_print:
                    next_print += print_every
                    if not args.quiet:
                        print(f"[{now - start:8.1f}s] {console_line(rx, snap)}", flush=True)
    finally:
        rx.close()
        transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
