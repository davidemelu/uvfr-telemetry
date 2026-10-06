"""Control a running simulated radio link.

    python -m link_sim.ctl status
    python -m link_sim.ctl profiles
    python -m link_sim.ctl profile lora_bad
    python -m link_sim.ctl set loss_pct=5 latency_ms=200 jitter_ms=50
    python -m link_sim.ctl outage 5
"""

from __future__ import annotations

import argparse
import json
import sys

from common.config import ConfigError, load_yaml
from common.control import ControlError, request
from common.transport import parse_hostport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="link_sim.ctl", description="Control the simulated radio link.")
    parser.add_argument("command", choices=["status", "profiles", "profile", "set", "outage"])
    parser.add_argument("args", nargs="*", help="profile name, key=value pairs, or outage seconds")
    parser.add_argument("--config", default="config/telemetry.yaml")
    args = parser.parse_args(argv)

    try:
        host, port = parse_hostport(load_yaml(args.config)["link_sim"]["control"])
    except (ConfigError, KeyError, ValueError) as exc:
        print(f"link_sim.ctl: {exc}", file=sys.stderr)
        return 2

    payload: dict = {"cmd": args.command}
    if args.command == "profile":
        if len(args.args) != 1:
            parser.error("profile needs exactly one name")
        payload["name"] = args.args[0]
    elif args.command == "set":
        if not args.args or not all("=" in a for a in args.args):
            parser.error("set needs key=value pairs, e.g. loss_pct=5")
        payload["params"] = dict(a.split("=", 1) for a in args.args)
    elif args.command == "outage":
        payload["seconds"] = float(args.args[0]) if args.args else 5.0

    try:
        reply = request(host, port, payload)
    except ControlError as exc:
        print(f"link_sim.ctl: {exc}", file=sys.stderr)
        return 1
    if not reply.pop("ok", False):
        print(f"link_sim.ctl: {reply.get('error', 'request failed')}", file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
