"""Record CAN traffic to a SocketCAN candump log.

    python -m replay.record                              # vcan0 -> recordings/<timestamp>.log
    python -m replay.record --channel can0 --duration 600 --out recordings/endurance-1.log

Recording is receive-only and safe on a real vehicle bus (it opens the bus
through the car node's receive-only wrapper and never transmits). For a real
car, put the interface in listen-only mode first (docs/hardware-transition.md).
`candump -l <iface>` produces the same format if you prefer can-utils.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import can

from car_node.can_rx import ListenOnlyError, ReceiveOnlyBus, check_listen_only
from common.canbus import open_bus
from common.config import REPO_ROOT
from replay.logs import format_line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay.record", description="Record CAN traffic to a candump log.")
    parser.add_argument("--interface", default="socketcan")
    parser.add_argument("--channel", default="vcan0")
    parser.add_argument("--out", help="output file (default recordings/<channel>-<timestamp>.log)")
    parser.add_argument("--duration", type=float, help="stop after this many seconds")
    parser.add_argument("--allow-active-bus", action="store_true",
                        help="record from a real CAN interface that is not in listen-only mode")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else REPO_ROOT / "recordings" / (
        f"{args.channel}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    try:
        mode = check_listen_only(args.interface, args.channel, require=not args.allow_active_bus)
        bus = ReceiveOnlyBus(open_bus(args.interface, args.channel))
    except ListenOnlyError as exc:
        print(f"replay.record: {exc}", file=sys.stderr)
        return 3
    except (OSError, can.CanError) as exc:
        print(f"replay.record: cannot open {args.interface}:{args.channel}: {exc}", file=sys.stderr)
        return 1

    stopping = False

    def _stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"replay.record: {mode} -> {out} (Ctrl+C to stop)", flush=True)
    count = 0
    start = time.monotonic()
    end = start + args.duration if args.duration else None
    try:
        with out.open("w", encoding="ascii") as fh:
            while not stopping and (end is None or time.monotonic() < end):
                msg = bus.recv(timeout=0.2)
                if msg is None or msg.is_error_frame or msg.is_remote_frame:
                    continue
                fh.write(format_line(msg, args.channel))
                count += 1
    finally:
        bus.shutdown()
    elapsed = time.monotonic() - start
    print(f"replay.record: wrote {count} frames in {elapsed:.1f} s ({count / max(elapsed, 1e-9):.0f} frames/s) to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
