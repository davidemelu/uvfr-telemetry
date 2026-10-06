"""Car telemetry node: CAN in, compact telemetry packets out.

    python -m car_node                          # wiring from config/telemetry.yaml
    python -m car_node --remote 127.0.0.1:47002 # send straight to a pit receiver

Pipeline:

    SocketCAN (receive-only) -> DBC decoder -> vehicle state
        -> channel scheduler -> packet encoder -> transport

This node knows nothing about InfluxDB, Grafana or the pit receiver. Its only
job is to receive CAN, decode it, select telemetry, package it and transmit
it. It is the component that will later run unchanged on the Raspberry Pi 3;
only the CAN channel (vcan0 -> can0) and transport (UDP -> serial) change.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import can
from cantools.database.can import Database

from car_node.can_rx import FrameDecoder, ListenOnlyError, VehicleState, open_receive_only
from car_node.scheduler import ChannelScheduler
from car_node.stats import TxStats
from common.config import ConfigError, check_keys, load_yaml, resolve_path
from common.dbc import load_dbc
from common.protocol import CarStatus, ChannelLayout, Codec, Missing
from common.transport import Transport, transport_from_config

CAR_NODE_KEYS = {"dbc", "can", "transport", "status_print_interval_s", "stats_file"}
CAN_KEYS = {"interface", "channel", "require_listen_only"}


class CarNode:
    """Everything except the CAN bus and the main loop; driven by on_frame() and run_tick()."""

    def __init__(
        self,
        layout: ChannelLayout,
        db: Database,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        session: int | None = None,
    ) -> None:
        self.layout = layout
        self.codec = Codec(layout)
        self.decoder = FrameDecoder(db, layout.messages)
        self.state = VehicleState()
        self.scheduler = ChannelScheduler(layout)
        self.transport = transport
        self.clock = clock
        self.session = session if session is not None else random.SystemRandom().randint(1, 255)
        self.start = clock()
        self.seq = 0
        self.tick = 0
        self.tx = TxStats(layout)
        self.can_fps = 0
        self.cpu_pct: int | None = None
        self._status_mark = (self.start, 0, time.process_time())

    # ------------------------------------------------------------------ input
    def on_frame(self, msg: can.Message, now: float | None = None) -> None:
        result = self.decoder.decode(msg)
        if result is not None:
            message, values = result
            self.state.update(message.name, values, self.clock() if now is None else now)

    # ----------------------------------------------------------------- output
    def _timestamp_ms(self, now: float) -> int:
        return int((now - self.start) * 1000)

    def _next_seq(self) -> int:
        seq = self.seq
        self.seq = (self.seq + 1) & 0xFFFF
        return seq

    def run_tick(self, now: float) -> list[bytes]:
        """Build and send the packets due on this scheduler tick."""
        sent: list[bytes] = []
        due = self.scheduler.due(self.tick)
        if due:
            values = {i: self.state.channel_value(self.layout.channels[i], now) for i in due}
            packet = self.codec.encode_telemetry(self.session, self._next_seq(), self._timestamp_ms(now), values)
            self.transport.send(packet)
            self.tx.record_telemetry(len(packet), values)
            sent.append(packet)
        if self.scheduler.status_due(self.tick):
            packet = self.codec.encode_status(
                self.session, self._next_seq(), self._timestamp_ms(now), self._status(now)
            )
            self.transport.send(packet)
            self.tx.record_status(len(packet))
            sent.append(packet)
        self.tick += 1
        return sent

    def _status(self, now: float) -> CarStatus:
        t0, frames0, cpu0 = self._status_mark
        frames, cpu = self.decoder.frames, time.process_time()
        dt = now - t0
        if dt > 0:
            self.can_fps = round((frames - frames0) / dt)
            self.cpu_pct = round(100.0 * (cpu - cpu0) / dt)
        self._status_mark = (now, frames, cpu)
        ages = []
        for name in self.layout.messages:
            age = self.state.message_age_s(name, now)
            ages.append(None if age is None else age * 1000.0)
        return CarStatus(
            config_hash=self.layout.config_hash,
            tx_packets=self.tx.packets_sent + 1,  # including this STATUS packet
            can_frames=frames,
            can_fps=self.can_fps,
            can_counter_gaps=self.decoder.counter_gaps,
            can_errors=self.decoder.decode_errors,
            cpu_pct=self.cpu_pct,
            message_ages_ms=tuple(ages),
        )

    def unhealthy_channels(self, now: float) -> dict[str, str]:
        out = {}
        for spec in self.layout.channels:
            value = self.state.channel_value(spec, now)
            if isinstance(value, Missing):
                out[spec.name] = value.value
        return out

    # ------------------------------------------------------------------- loop
    def run(self, bus, should_stop: Callable[[], bool], on_tick: Callable[[float], None] | None = None) -> None:
        tick_s = self.scheduler.tick_s
        next_tick = self.clock()
        while not should_stop():
            msg = bus.recv(timeout=max(0.0, next_tick - self.clock()))
            now = self.clock()
            if msg is not None:
                self.on_frame(msg, now)
            if now >= next_tick:
                self.run_tick(now)
                if on_tick:
                    on_tick(now)
                next_tick += tick_s
                if now - next_tick > 1.0:  # stalled (VM paused?): resync, do not burst
                    next_tick = now + tick_s


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="car_node", description="Car telemetry node (CAN in, telemetry out).")
    parser.add_argument("--config", default="config/telemetry.yaml")
    parser.add_argument("--channels", default="config/channels.yaml")
    parser.add_argument("--interface", help="python-can interface (overrides config)")
    parser.add_argument("--channel", help="CAN channel, e.g. vcan0 or can0 (overrides config)")
    parser.add_argument("--remote", help="send UDP to host:port instead of the configured transport")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--quiet", action="store_true", help="no periodic status line")
    args = parser.parse_args(argv)

    try:
        cfg = check_keys(load_yaml(args.config).get("car_node"), CAR_NODE_KEYS, "telemetry.car_node")
        can_cfg = check_keys(cfg["can"], CAN_KEYS, "telemetry.car_node.can")
        db = load_dbc(cfg["dbc"])
        layout = ChannelLayout.load(args.channels, db)
        transport_cfg = {"type": "udp", "remote": args.remote} if args.remote else cfg["transport"]
        transport = transport_from_config(transport_cfg, "telemetry.car_node.transport", role="sender")
    except (ConfigError, ValueError, OSError) as exc:
        print(f"car_node: {exc}", file=sys.stderr)
        return 2

    interface = args.interface or can_cfg["interface"]
    channel = args.channel or can_cfg["channel"]
    node = CarNode(layout, db, transport)
    try:
        bus, bus_mode = open_receive_only(interface, channel, node.decoder.can_ids, bool(can_cfg["require_listen_only"]))
    except ListenOnlyError as exc:
        print(f"car_node: REFUSING TO START: {exc}", file=sys.stderr)
        return 3
    except (OSError, can.CanError) as exc:
        print(f"car_node: cannot open {interface}:{channel}: {exc}", file=sys.stderr)
        return 1

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    full = node.codec.telemetry_size(list(range(len(layout.channels))))
    print(
        f"car_node: session {node.session:#04x} | CAN {bus_mode} (receive-only) | "
        f"{len(layout.channels)} channels, layout hash {layout.config_hash:#06x}, "
        f"telemetry packet {node.codec.telemetry_size([])}..{full} bytes, status {node.codec.status_size} bytes | "
        f"{transport.describe()}",
        flush=True,
    )

    stats_path = resolve_path(cfg["stats_file"]) if cfg.get("stats_file") else None
    print_every = float(cfg["status_print_interval_s"])
    next_print = node.clock() + print_every
    end = node.clock() + args.duration if args.duration else None

    def on_tick(now: float) -> None:
        nonlocal next_print
        if now < next_print:
            return
        next_print += print_every
        w = node.tx.window(now)
        if not w:
            return
        bad = node.unhealthy_channels(now)
        if not args.quiet:
            print(
                f"[{now - node.start:8.1f}s] CAN {node.can_fps:4d} fps (gaps {node.decoder.counter_gaps}, "
                f"errors {node.decoder.decode_errors}) | TX {w['packets_per_s']['total']:4.1f} pkt/s "
                f"{w['bytes_per_s']['total']:6.1f} B/s {w['bits_per_s']:7.0f} bit/s "
                f"(values {w['payload_bytes_per_s']:5.1f} B/s) | "
                f"{'not live: ' + ', '.join(f'{k}={v}' for k, v in bad.items()) if bad else 'all channels live'}",
                flush=True,
            )
        if stats_path:
            _write_json_atomic(
                stats_path,
                {
                    "session": node.session,
                    "uptime_s": round(now - node.start, 1),
                    "can_fps": node.can_fps,
                    "can_counter_gaps": node.decoder.counter_gaps,
                    "can_decode_errors": node.decoder.decode_errors,
                    "cpu_pct": node.cpu_pct,
                    "transport": node.transport.stats.as_dict(),
                    "not_live": bad,
                    "rates": w,
                },
            )

    try:
        node.run(bus, lambda: stopping or (end is not None and node.clock() >= end), on_tick)
    finally:
        bus.shutdown()
        transport.close()
    print(f"car_node: stopped; sent {node.tx.packets_sent} packets, send errors {transport.stats.send_errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
