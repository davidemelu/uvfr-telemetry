"""Replay a CAN log onto a virtual bus with its original timing.

    python -m replay recordings/session.log                 # real time, once
    python -m replay recordings/session.log --speed 2 --loop
    python -m replay recordings/session.log --speed 0.5 --paused

While it runs:
    keyboard (in a terminal): space pause/resume, + faster, - slower, l loop on/off, q quit
    python -m replay.ctl pause | resume | speed 5 | loop on | status | stop

The rest of the pipeline cannot tell replayed frames from the fake ECU (or,
later, the car): the car node just sees frames on vcan0. That is how real
UVFR test-day data will be fed through the system.

SAFETY: like the fake ECU, the player only transmits on a virtual bus
(vcan*, udp_multicast, virtual). It refuses can0: replaying recorded traffic
onto a real vehicle bus could command real hardware.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import can

from common.canbus import check_virtual_bus, open_bus
from common.config import ConfigError, load_yaml
from common.control import ControlError, ControlServer
from common.transport import parse_hostport
from replay.logs import read_log, summarize

SPEED_STEPS = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0]
MAX_SPEED = 50.0
LAG_RESYNC_S = 1.0  # if playback falls this far behind, jump instead of bursting


def retarget(msg: can.Message) -> can.Message:
    """Copy without the recorded channel, so it is sent on *our* bus, not the one in the log."""
    return can.Message(
        timestamp=msg.timestamp,
        arbitration_id=msg.arbitration_id,
        is_extended_id=msg.is_extended_id,
        is_fd=msg.is_fd,
        bitrate_switch=msg.bitrate_switch,
        dlc=msg.dlc,
        data=bytes(msg.data),
    )


class Player:
    def __init__(
        self,
        messages: Callable[[], Iterator[can.Message]],
        send: Callable[[can.Message], None],
        *,
        speed: float = 1.0,
        loop: bool = False,
        paused: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        loop_gap_s: float = 0.1,
    ) -> None:
        self._messages = messages
        self._send = send
        self.clock = clock
        self.sleep = sleep
        self.loop = loop
        self.loop_gap_s = loop_gap_s
        self.paused = paused
        self.finished = False
        self.frames_sent = 0
        self.loops_completed = 0
        self.resyncs = 0
        self.position_s = 0.0  # log time of the last frame sent, from the start of the log
        self._paused_at: float | None = clock() if paused else None
        self._wall_anchor = clock()
        self._log_anchor = 0.0
        self._first_ts: float | None = None
        self.speed = 1.0
        self.set_speed(speed)

    # ------------------------------------------------------------ control
    def set_speed(self, speed: float) -> None:
        if not 0 < speed <= MAX_SPEED:
            raise ValueError(f"speed must be between 0 and {MAX_SPEED:g}")
        if not self.paused:
            # re-anchor at the current log position so the change is seamless
            now = self.clock()
            self._log_anchor += (now - self._wall_anchor) * self.speed
            self._wall_anchor = now
        self.speed = float(speed)

    def pause(self) -> None:
        if not self.paused:
            self.paused = True
            self._paused_at = self.clock()

    def resume(self) -> None:
        if self.paused:
            self._wall_anchor += self.clock() - (self._paused_at or self.clock())
            self.paused = False
            self._paused_at = None

    def toggle(self) -> None:
        self.resume() if self.paused else self.pause()

    def faster(self) -> None:
        self.set_speed(next((s for s in SPEED_STEPS if s > self.speed), SPEED_STEPS[-1]))

    def slower(self) -> None:
        self.set_speed(next((s for s in reversed(SPEED_STEPS) if s < self.speed), SPEED_STEPS[0]))

    def status(self) -> dict[str, Any]:
        return {"state": "finished" if self.finished else ("paused" if self.paused else "playing"),
                "speed": self.speed, "loop": self.loop, "position_s": round(self.position_s, 2),
                "loops_completed": self.loops_completed, "frames_sent": self.frames_sent, "resyncs": self.resyncs}

    # --------------------------------------------------------------- play
    def _due(self, ts: float) -> float:
        return self._wall_anchor + (ts - self._log_anchor) / self.speed

    def run(self, should_stop: Callable[[], bool] = lambda: False, on_idle: Callable[[], None] | None = None) -> None:
        it = self._messages()
        msg = next(it, None)
        if msg is None:
            self.finished = True
            return
        self._first_ts = msg.timestamp
        self._log_anchor = msg.timestamp
        self._wall_anchor = self.clock()
        if self.paused:
            self._paused_at = self._wall_anchor
        while not should_stop():
            if on_idle:
                on_idle()
            if self.paused:
                self.sleep(0.05)
                continue
            now = self.clock()
            due = self._due(msg.timestamp)
            if due > now:
                self.sleep(min(due - now, 0.05))  # short naps keep pause/speed responsive
                continue
            if now - due > LAG_RESYNC_S:
                self._wall_anchor, self._log_anchor = now, msg.timestamp
                self.resyncs += 1
            self._send(msg)
            self.frames_sent += 1
            self.position_s = msg.timestamp - self._first_ts
            nxt = next(it, None)
            if nxt is None:
                if not self.loop:
                    self.finished = True
                    return
                self.loops_completed += 1
                it = self._messages()
                nxt = next(it, None)
                if nxt is None:
                    self.finished = True
                    return
                self._first_ts = nxt.timestamp
                self._log_anchor = nxt.timestamp
                self._wall_anchor = self.clock() + self.loop_gap_s
            elif nxt.timestamp < msg.timestamp:
                # out-of-order timestamps in the log: play immediately
                self._log_anchor -= msg.timestamp - nxt.timestamp
            msg = nxt


class Keyboard:
    """Single-key commands when stdin is a terminal (Linux/macOS)."""

    def __init__(self) -> None:
        self.enabled = sys.stdin.isatty()
        self._saved = None
        if self.enabled:
            import termios
            import tty

            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def key(self) -> str | None:
        if not self.enabled:
            return None
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        return os.read(sys.stdin.fileno(), 1).decode(errors="ignore") if ready else None

    def close(self) -> None:
        if self._saved is not None:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)


def make_handler(player: Player, stop: Callable[[], None]):
    def handle(req: dict[str, Any]) -> dict[str, Any]:
        cmd = req.get("cmd")
        if cmd == "pause":
            player.pause()
        elif cmd == "resume":
            player.resume()
        elif cmd == "toggle":
            player.toggle()
        elif cmd == "speed":
            player.set_speed(float(req.get("value", 1.0)))
        elif cmd == "loop":
            player.loop = bool(req.get("value", True))
        elif cmd == "stop":
            stop()
        elif cmd != "status":
            raise ValueError(f"unknown command {cmd!r}; use status, pause, resume, toggle, speed, loop or stop")
        return {"ok": True, **player.status()}

    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay", description="Replay a CAN log onto a virtual bus.")
    parser.add_argument("log", help="candump .log (or .asc, .blf, .trc, .csv)")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed, e.g. 0.5, 1, 2, 5 (default 1)")
    parser.add_argument("--loop", action="store_true", help="start again at the end")
    parser.add_argument("--paused", action="store_true", help="start paused")
    parser.add_argument("--config", default="config/telemetry.yaml")
    parser.add_argument("--interface", help="python-can interface (default from config)")
    parser.add_argument("--channel", help="bus to play onto (default from config)")
    parser.add_argument("--no-control", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        cfg = load_yaml(args.config).get("replay") or {}
        interface = args.interface or cfg.get("interface", "socketcan")
        channel = args.channel or cfg.get("channel", "vcan0")
        control_addr = parse_hostport(cfg.get("control", "127.0.0.1:47030"))
        info = summarize(args.log)
    except (ConfigError, ValueError, OSError) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2
    if not info["frames"]:
        print(f"replay: {args.log} contains no CAN frames", file=sys.stderr)
        return 2
    check_virtual_bus(interface, channel)

    try:
        bus = open_bus(interface, channel)
    except (OSError, can.CanError) as exc:
        print(f"replay: cannot open {interface}:{channel}: {exc}", file=sys.stderr)
        return 1

    errors = 0

    def send(msg: can.Message) -> None:
        nonlocal errors
        try:
            bus.send(retarget(msg), timeout=0.05)
        except can.CanError:
            errors += 1

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        player = Player(lambda: read_log(args.log), send, speed=args.speed, loop=args.loop, paused=args.paused)
    except ValueError as exc:
        bus.shutdown()
        print(f"replay: {exc}", file=sys.stderr)
        return 2

    control = None
    if not args.no_control:
        try:
            control = ControlServer(*control_addr, make_handler(player, _stop))
        except ControlError as exc:
            print(f"replay: {exc} (another replay running?)", file=sys.stderr)
            bus.shutdown()
            return 1
    keyboard = Keyboard() if not args.quiet else None
    print(f"replay: {args.log}: {info['frames']} frames, {info['duration_s']} s, {info['frames_per_s']} frames/s "
          f"-> {interface}:{channel} at {player.speed:g}x{' (loop)' if player.loop else ''}"
          + (f", control udp://{control.address[0]}:{control.address[1]}" if control else "")
          + (" | keys: space pause, +/- speed, l loop, q quit" if keyboard and keyboard.enabled else ""), flush=True)

    next_print = time.monotonic() + 1.0
    last_sent = 0

    def on_idle() -> None:
        nonlocal next_print, last_sent
        if control:
            control.poll()
        if keyboard:
            k = keyboard.key()
            if k == " ":
                player.toggle()
            elif k in ("+", "="):
                player.faster()
            elif k == "-":
                player.slower()
            elif k == "l":
                player.loop = not player.loop
            elif k in ("q", "\x03"):
                _stop()
        now = time.monotonic()
        if now >= next_print:
            next_print = now + 1.0
            if not args.quiet:
                s = player.status()
                print(f"replay: {s['state']:<8} {s['speed']:g}x | log {s['position_s']:8.1f} / {info['duration_s']} s "
                      f"(loops {s['loops_completed']}) | {s['frames_sent'] - last_sent:5d} frames/s | sent {s['frames_sent']}",
                      flush=True)
            last_sent = player.frames_sent

    try:
        player.run(lambda: stopping, on_idle)
    finally:
        if keyboard:
            keyboard.close()
        if control:
            control.close()
        bus.shutdown()
    print(f"replay: done; sent {player.frames_sent} frames, {player.loops_completed} loops, "
          f"{errors} transmit errors, {player.resyncs} resyncs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
