"""Control a running fake ECU.

    python -m simulator.ctl status
    python -m simulator.ctl list
    python -m simulator.ctl set overheating [intermittent_can ...]
    python -m simulator.ctl add low_oil_pressure
    python -m simulator.ctl remove low_oil_pressure
    python -m simulator.ctl normal
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import ConfigError, load_yaml  # noqa: E402
from common.control import ControlError, request  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simulator.ctl", description="Control a running fake ECU.")
    parser.add_argument("command", choices=["status", "list", "set", "add", "remove", "normal"])
    parser.add_argument("scenarios", nargs="*", help="scenario names (comma-separated or separate words)")
    parser.add_argument("--config", default="config/simulation.yaml")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    try:
        control = load_yaml(args.config)["control"]
    except (ConfigError, KeyError) as exc:
        print(f"simulator.ctl: {exc}", file=sys.stderr)
        return 2
    host = args.host or control["host"]
    port = args.port or control["port"]

    if args.command in ("set", "add", "remove") and not args.scenarios:
        parser.error(f"'{args.command}' needs at least one scenario name")
    payload = {"cmd": args.command, "scenarios": args.scenarios}
    try:
        reply = request(host, port, payload)
    except ControlError as exc:
        print(f"simulator.ctl: {exc}", file=sys.stderr)
        return 1
    if not reply.get("ok"):
        print(f"simulator.ctl: {reply.get('error', 'request failed')}", file=sys.stderr)
        return 1
    reply.pop("ok", None)
    if args.command == "list":
        for name, summary in reply["scenarios"].items():
            print(f"{name:<22} {summary}")
    else:
        print(json.dumps(reply, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
