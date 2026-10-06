#!/usr/bin/env python3
"""Scripted Phase 0 success check: overheat the simulated car and follow it end to end.

    make demo-overheating            # needs the demo running (make demo)
    python scripts/demo_overheating.py --keep    # leave the car overheating afterwards

What it proves, step by step:
  1. the fake ECU switches to the overheating scenario
  2. coolant temperature rises in the telemetry
  3. the pit receiver receives it (link healthy, packets arriving)
  4. InfluxDB records it
  5. Grafana's own query API returns the rising values (what the dashboard plots)
  6. the coolant alarm becomes WARNING, then CRITICAL

Everything is read back through Grafana, the same path the dashboard uses.
Watch the dashboard while it runs.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import load_yaml  # noqa: E402
from common.control import ControlError, request  # noqa: E402
from common.env import load_env  # noqa: E402


class Grafana:
    def __init__(self, url: str, user: str, password: str, bucket: str, car: str) -> None:
        self.url = url.rstrip("/")
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.bucket, self.car = bucket, car

    def last(self, measurement: str, field: str, window: str = "15s", extra: str = ""):
        flux = (f'from(bucket: "{self.bucket}") |> range(start: -{window}) '
                f'|> filter(fn: (r) => r._measurement == "{measurement}" and r.car == "{self.car}" '
                f'and r._field == "{field}"{extra}) |> last()')
        frames = self._query(flux, window)
        values = [f["data"]["values"][-1][-1] for f in frames if f.get("data", {}).get("values")]
        return values[-1] if values else None

    def count(self, measurement: str, field: str, window: str) -> int:
        flux = (f'from(bucket: "{self.bucket}") |> range(start: -{window}) '
                f'|> filter(fn: (r) => r._measurement == "{measurement}" and r.car == "{self.car}" '
                f'and r._field == "{field}") |> count()')
        frames = self._query(flux, window)
        return int(sum(f["data"]["values"][-1][-1] for f in frames if f.get("data", {}).get("values")))

    def _query(self, flux: str, window: str) -> list:
        body = {"queries": [{"refId": "A", "datasource": {"type": "influxdb", "uid": "uvfr-influx"}, "query": flux}],
                "from": f"now-{window}", "to": "now"}
        req = urllib.request.Request(f"{self.url}/api/ds/query", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Basic {self.auth}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())["results"]["A"]
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result.get("frames", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Overheat the simulated car and verify the whole chain.")
    parser.add_argument("--timeout", type=float, default=150, help="give up after this many seconds")
    parser.add_argument("--keep", action="store_true", help="leave the overheating scenario running")
    parser.add_argument("--grafana", default="http://127.0.0.1:3000")
    args = parser.parse_args(argv)

    env = load_env()
    dash = load_yaml("config/dashboard.yaml")
    sim_ctl = load_yaml("config/simulation.yaml")["control"]
    alerts = {r["id"]: r for r in load_yaml("config/alerts.yaml")["rules"]}
    warn, crit = alerts["coolant_high"]["above"]["warning"], alerts["coolant_high"]["above"]["critical"]
    g = Grafana(args.grafana, env.get("GRAFANA_ADMIN_USER", "admin"), env.get("GRAFANA_ADMIN_PASSWORD", ""),
                dash["bucket"], dash["car"])

    def ecu(cmd: str, scenarios: list[str] | None = None) -> dict:
        return request(sim_ctl["host"], sim_ctl["port"], {"cmd": cmd, "scenarios": scenarios or []})

    try:
        ecu("status")
    except ControlError:
        print("The demo is not running (fake ECU not reachable). Start it with: make demo", file=sys.stderr)
        return 1
    try:
        start_coolant = g.last("vehicle", "coolant_temperature")
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"Cannot query Grafana at {args.grafana}: {exc}", file=sys.stderr)
        return 1

    print(f"Coolant alarm thresholds from config/alerts.yaml: WARNING >= {warn} degC, CRITICAL >= {crit} degC")
    if start_coolant is not None and start_coolant >= warn - 5:
        print(f"Coolant is already {start_coolant:.1f} degC; running normal for a while so the demo starts cool ...")
        ecu("normal")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            time.sleep(3)
            start_coolant = g.last("vehicle", "coolant_temperature")
            if start_coolant is not None and start_coolant < warn - 10:
                break

    print(f"\nStep 1  fake ECU -> overheating (fan failure + coolant loss). Baseline coolant {start_coolant} degC\n")
    ecu("set", ["overheating"])
    t0 = time.monotonic()
    seen: dict[str, float] = {}
    first = last = None
    print(f"{'t (s)':>6}  {'coolant':>8}  {'link':<8}  {'pkt/s':>6}  {'coolant status':<14}  active alarms")
    while time.monotonic() - t0 < args.timeout:
        time.sleep(3)
        t = time.monotonic() - t0
        coolant = g.last("vehicle", "coolant_temperature")
        link = g.last("link", "status")
        rate = g.last("link", "packet_rate")
        status = g.last("channel_state", "status", extra=' and r.channel == "coolant_temperature"')
        summary = g.last("alerts_active", "summary") or ""
        scenario = g.last("sim", "scenario")
        if coolant is not None:
            first = coolant if first is None else first
            last = coolant
        if scenario and "overheating" in scenario:
            seen.setdefault("scenario", t)
        if first is not None and last is not None and last > first + 2:
            seen.setdefault("rising", t)
        if "rising rapidly" in summary:
            seen.setdefault("rate", t)
        if "WARNING: Coolant temperature high" in summary or "CRITICAL: Coolant temperature high" in summary:
            seen.setdefault("warning", t)
        if "CRITICAL: Coolant temperature high" in summary and status == "CRITICAL":
            seen.setdefault("critical", t)
        alarms = summary if len(summary) <= 70 else summary[:67] + "..."
        coolant_txt = "-" if coolant is None else f"{coolant:.1f} C"
        rate_txt = "-" if rate is None else f"{rate:.1f}"
        print(f"{t:6.0f}  {coolant_txt:>8}  {str(link):<8}  {rate_txt:>6}  {str(status):<14}  {alarms}")
        if "critical" in seen and t - seen["critical"] >= 3:
            break

    points = g.count("vehicle", "coolant_temperature", "2m")
    received = g.last("link", "packets_received")
    raised = g.last("alerts_active", "critical_count")
    if not args.keep:
        ecu("normal")

    checks = [
        ("fake ECU reports the overheating scenario (via CAN SIM_STATUS)", "scenario" in seen),
        (f"coolant rose in the telemetry ({first} -> {last} degC)", "rising" in seen),
        (f"pit receiver is receiving (link {link}, {received} packets this session)", link in ("NORMAL", "WARNING")),
        (f"InfluxDB recorded it ({points} coolant samples in the last 2 min)", points > 100),
        ("Grafana's query API returns the rising coolant (what the graph shows)", "rising" in seen),
        ("early warning: coolant rising rapidly" + (f" after {seen['rate']:.0f} s" if "rate" in seen else ""),
         "rate" in seen),
        (f"coolant alarm WARNING (>= {warn} degC)" + (f" after {seen['warning']:.0f} s" if "warning" in seen else ""),
         "warning" in seen),
        (f"coolant alarm CRITICAL (>= {crit} degC)" + (f" after {seen['critical']:.0f} s" if "critical" in seen else "")
         + f", {raised} critical alarm(s) active", "critical" in seen),
    ]
    print()
    for text, ok in checks:
        print(f"  [{' OK ' if ok else 'FAIL'}] {text}")
    print(f"\nScenario {'left at overheating (--keep)' if args.keep else 'restored to normal; the coolant will recover over a minute or two'}.")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
