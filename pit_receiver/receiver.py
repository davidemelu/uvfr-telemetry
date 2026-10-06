"""Pit telemetry receiver.

    python -m pit_receiver                  # wiring from config/telemetry.yaml
    python -m pit_receiver --no-influx --jsonl logs/points.jsonl

For every packet that arrives it validates and decodes it, tracks sequence
gaps, packet loss, rate and staleness, maps the car's timestamp onto the pit
clock, and keeps the latest value of every channel. Once per health interval
it evaluates the alarms in config/alerts.yaml and emits link health,
per-channel state and alarm states. Everything leaves as Points through sinks
(InfluxDB, JSON lines), so the receiver itself has no database code.

It runs as its own process and shares nothing with the car node except the
protocol definition and channels.yaml.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import Any

from common.config import ConfigError, check_keys, load_yaml, resolve_path
from common.dbc import load_dbc
from common.env import load_env
from common.protocol import ChannelLayout, Codec, DecodeError, Missing, StatusPacket, TelemetryPacket
from common.transport import transport_from_config
from pit_receiver.alerts import AlertEngine, AlertEvent
from pit_receiver.channels import ChannelTracker, StalenessConfig
from pit_receiver.clock import SourceClock
from pit_receiver.influx_sink import InfluxConfig, InfluxSink
from pit_receiver.link_stats import LinkSnapshot, LinkStats, LinkThresholds
from pit_receiver.points import Point
from pit_receiver.sinks import JsonlSink, Sink
from pit_receiver.status import Status, worst

PIT_KEYS = {
    "transport",
    "dbc",
    "car_id",
    "health_interval_s",
    "rate_window_s",
    "loss_window_packets",
    "clock_window_s",
    "status_print_interval_s",
    "outputs",
}
INFLUX_KEYS = {"enabled", "env_file", "token_variable", "batch_size", "flush_interval_s", "max_buffer_points"}
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
        alerts: AlertEngine | None = None,
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
        self.alerts = alerts
        self.car_id = car_id
        self.sinks = sinks or []
        self.wall = wall
        self.mono = mono
        self.started = mono()
        self.last_status: StatusPacket | None = None
        self._can_age: tuple[float, float] | None = None  # (oldest vehicle CAN age ms, received at)
        self.last_events: list[AlertEvent] = []
        self.vehicle_status = Status.NO_DATA
        self.channel_status: dict[str, Status] = {c.name: Status.NO_DATA for c in layout.channels}
        self._vehicle_channels = [c.name for c in layout.channels if not c.name.startswith(LAB_ONLY_PREFIX)]
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
            if self.alerts is not None:
                self.alerts.observe(packet.values, mono)
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
        worst_age = None
        for name, age in zip(self.layout.messages, s.message_ages_ms):
            if age is None:
                continue
            fields[f"can_age_{name}_ms"] = float(age)
            if name in self._vehicle_messages:
                worst_age = age if worst_age is None else max(worst_age, age)
        if worst_age is not None:
            fields["can_age_max_ms"] = float(worst_age)
            self._can_age = (float(worst_age), mono)
        return self._emit([Point("car_status", fields, self.tags, time_ns)])

    # ---------------------------------------------------------- once a second
    def _combined_status(self, name: str, view_status: Status) -> Status:
        if view_status is not Status.NORMAL or self.alerts is None:
            return view_status
        if self.alerts.channel_frozen(name):
            return Status.STALE  # numbers are arriving but cannot be trusted
        return self.alerts.channel_alarm(name)

    def health(self, wall: float | None = None, mono: float | None = None) -> tuple[LinkSnapshot, list[Point]]:
        wall = self.wall() if wall is None else wall
        mono = self.mono() if mono is None else mono
        time_ns = int(wall * 1e9)
        snap = self.link.snapshot(mono)
        views = self.channels.views(mono)
        can_age = None
        if self._can_age is not None and mono - self._can_age[1] < 3.0:  # STATUS is 1 Hz
            can_age = self._can_age[0]
        events = self.alerts.evaluate(mono, views, snap, can_age) if self.alerts is not None else []
        self.last_events = events

        link_fields = snap.fields()
        link_fields["delay_excess_ms"] = round(self.clock.take_max_excess_delay() * 1000, 1)
        points = [Point("link", link_fields, self.tags, time_ns)]

        for name, view in views.items():
            status = self._combined_status(name, view.status)
            self.channel_status[name] = status
            fields: dict[str, Any] = {"status": status.label, "status_code": int(status)}
            if view.age_s is not None:
                fields["age_ms"] = round(view.age_s * 1000, 1)
            if view.last_value is not None:
                fields["last_value"] = float(view.last_value)
            points.append(Point("channel_state", fields, {**self.tags, "channel": name}, time_ns))
        self.vehicle_status = worst(self.channel_status[n] for n in self._vehicle_channels)

        if self.alerts is not None:
            for ev in events:
                points.append(Point(
                    "alert",
                    {"severity": ev.severity.label, "severity_code": int(ev.severity), "previous": ev.previous.label,
                     "active": ev.severity in (Status.WARNING, Status.CRITICAL), "message": ev.message,
                     **({"value": float(ev.value)} if ev.value is not None else {})},
                    # Link and CAN-age rules have no channel; "-" keeps the tag present for queries.
                    {**self.tags, "rule": ev.rule_id, "channel": ev.channel or "-"},
                    time_ns,
                ))
            active = self.alerts.active()
            points.append(Point(
                "alerts_active",
                {
                    "warning_count": sum(1 for a in active if a.severity is Status.WARNING),
                    "critical_count": sum(1 for a in active if a.severity is Status.CRITICAL),
                    "summary": "; ".join(f"{a.severity.label}: {a.describe()}" for a in active) or "none",
                    "vehicle_status": self.vehicle_status.label,
                    "vehicle_status_code": int(self.vehicle_status),
                    "worst_alarm_code": int(active[0].severity) if active else int(Status.NORMAL),
                },
                self.tags,
                time_ns,
            ))

        sim = views.get("sim_scenario")
        if sim is not None and sim.value is not None:
            mask = int(sim.value)
            points.append(Point("sim", {"scenario": scenario_label(mask), "scenario_mask": mask}, self.tags, time_ns))

        pit_fields: dict[str, Any] = {"uptime_s": round(mono - self.started, 1)}
        for sink in self.sinks:
            pit_fields.update(sink.health())
        points.append(Point("pit", pit_fields, self.tags, time_ns))
        return snap, self._emit(points)

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception as exc:
                print(f"pit_receiver: closing sink {sink.name} failed: {exc}", file=sys.stderr)


def alarm_verb(ev: AlertEvent) -> str:
    alarming = (Status.WARNING, Status.CRITICAL)
    if ev.severity not in alarming:
        return "CLEARED"
    if ev.previous not in alarming:
        return "RAISED"
    return "ESCALATED" if ev.severity.severity > ev.previous.severity else "DOWNGRADED"


def console_line(rx: PitReceiver, snap: LinkSnapshot) -> str:
    views = rx.channels.views(rx.mono())

    def show(name: str, fmt: str) -> str:
        v = views.get(name)
        if v is None:
            return "-"
        return format(v.value, fmt) if v.value is not None else v.status.label

    age = "never" if snap.last_packet_age_s is None else f"{snap.last_packet_age_s * 1000:.0f} ms"
    alarms = rx.alerts.active() if rx.alerts else []
    return (
        f"LINK {snap.status.label:<8} {snap.packet_rate:5.1f} pkt/s {snap.byte_rate:6.1f} B/s "
        f"loss {snap.loss_pct:5.1f}% (recent {snap.loss_pct_window:5.1f}%) last {age} | "
        f"VEHICLE {rx.vehicle_status.label:<8} rpm {show('rpm', '.0f')} coolant {show('coolant_temperature', '.1f')} "
        f"oil {show('oil_pressure', '.0f')} batt {show('battery_voltage', '.2f')} | "
        f"alarms {len(alarms)}" + (f": {alarms[0].describe()}" + (" ..." if len(alarms) > 1 else "") if alarms else "")
    )


def build_sinks(outputs: dict[str, Any], jsonl_override: str | None, no_influx: bool) -> list[Sink]:
    check_keys(outputs, {"jsonl", "influxdb"}, "telemetry.pit_receiver.outputs")
    sinks: list[Sink] = []
    influx = outputs.get("influxdb") or {}
    if influx:
        check_keys(influx, INFLUX_KEYS, "telemetry.pit_receiver.outputs.influxdb")
    if influx.get("enabled") and not no_influx:
        load_env(resolve_path(influx["env_file"]))
        env = os.environ
        token_var = influx["token_variable"]
        cfg = InfluxConfig(
            url=env.get("INFLUX_URL", ""),
            org=env.get("INFLUX_ORG", ""),
            bucket=env.get("INFLUX_BUCKET", ""),
            token=env.get(token_var, ""),
            batch_size=int(influx["batch_size"]),
            flush_interval_s=float(influx["flush_interval_s"]),
            max_buffer_points=int(influx["max_buffer_points"]),
        )
        sinks.append(InfluxSink(cfg))
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
    parser.add_argument("--no-influx", action="store_true", help="do not write to InfluxDB")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = check_keys(load_yaml(args.config).get("pit_receiver"), PIT_KEYS, "telemetry.pit_receiver")
        alerts_cfg = check_keys(load_yaml(args.alerts), {"link", "channel_staleness", "rules"}, "alerts.yaml")
        dbc = load_dbc(cfg["dbc"]) if cfg.get("dbc") else None
        layout = ChannelLayout.load(args.channels, dbc)
        engine = AlertEngine.from_config(alerts_cfg, layout)
        transport_cfg = {"type": "udp", "listen": args.listen} if args.listen else cfg["transport"]
        transport = transport_from_config(transport_cfg, "telemetry.pit_receiver.transport", role="receiver")
        sinks = build_sinks(cfg["outputs"] or {}, args.jsonl, args.no_influx)
        rx = PitReceiver(
            layout,
            engine.link,
            StalenessConfig.from_config(alerts_cfg["channel_staleness"]),
            alerts=engine,
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
        f"{layout.config_hash:#06x} | {len(engine.rules)} alarm rules | outputs: "
        f"{', '.join(s.name for s in sinks) or 'console only'}",
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
                for ev in rx.last_events:
                    if not args.quiet or ev.severity is Status.CRITICAL:
                        print(f"[{now - start:8.1f}s] ALARM {alarm_verb(ev)} {ev.severity.label}: {ev.message}",
                              flush=True)
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
