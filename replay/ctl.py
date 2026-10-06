"""Control a running replay.

    python -m replay.ctl status
    python -m replay.ctl pause | resume | toggle | stop
    python -m replay.ctl speed 5
    python -m replay.ctl loop on|off
"""

from __future__ import annotations

import argparse
import json
import sys

from common.config import ConfigError, load_yaml
from common.control import ControlError, request
from common.transport import parse_hostport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay.ctl", description="Control a running replay.")
    parser.add_argument("command", choices=["status", "pause", "resume", "toggle", "speed", "loop", "stop"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--config", default="config/telemetry.yaml")
    args = parser.parse_args(argv)
    try:
        host, port = parse_hostport((load_yaml(args.config).get("replay") or {}).get("control", "127.0.0.1:47030"))
    except (ConfigError, ValueError) as exc:
        print(f"replay.ctl: {exc}", file=sys.stderr)
        return 2
    payload: dict = {"cmd": args.command}
    if args.command == "speed":
        if args.value is None:
            parser.error("speed needs a value, e.g. speed 2")
        payload["value"] = float(args.value)
    elif args.command == "loop":
        payload["value"] = (args.value or "on").lower() in ("on", "true", "1", "yes")
    try:
        reply = request(host, port, payload)
    except ControlError as exc:
        print(f"replay.ctl: {exc}", file=sys.stderr)
        return 1
    if not reply.pop("ok", False):
        print(f"replay.ctl: {reply.get('error', 'request failed')}", file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
