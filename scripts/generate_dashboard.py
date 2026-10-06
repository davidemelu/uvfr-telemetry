#!/usr/bin/env python3
"""Generate the Grafana pit dashboard from configuration.

    python scripts/generate_dashboard.py           # (re)write the dashboard JSON
    python scripts/generate_dashboard.py --check   # exit 1 if the committed JSON is stale

Inputs:
  config/dashboard.yaml  layout, display ranges and labels
  config/alerts.yaml     every threshold (gauge colours, graph lines, stat colours)
  config/channels.yaml   channel names

Never edit the JSON by hand: change the config and regenerate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import ConfigError, check_keys, load_yaml, resolve_path  # noqa: E402
from common.protocol import ChannelLayout  # noqa: E402
from pit_receiver.alerts import Rule, parse_rules  # noqa: E402
from pit_receiver.link_stats import LinkThresholds  # noqa: E402
from pit_receiver.status import Status  # noqa: E402

OUTPUT = "infrastructure/grafana/dashboards/uvfr-pit-telemetry.json"
DS = {"type": "influxdb", "uid": "uvfr-influx"}
STATUS_COLORS = {
    Status.NORMAL: "green",
    Status.WARNING: "orange",
    Status.CRITICAL: "red",
    Status.STALE: "purple",
    Status.NO_DATA: "#7f7f7f",
}
GRAPH_KEYS = {"title", "time", "channels", "unit", "right", "thresholds", "agg", "width"}
GAUGE_KEYS = {"channel", "unit", "min", "max", "decimals"}


# ------------------------------------------------------------------- flux
def flux_last(measurement: str, field: str, window: str = "10s") -> str:
    return (
        'from(bucket: "${bucket}")\n'
        f"  |> range(start: -{window})\n"
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r.car == "${{car}}" and r._field == "{field}")\n'
        "  |> last()\n"
        '  |> keep(columns: ["_time", "_value"])'
    )


def flux_series(measurement: str, field: str, agg: str) -> str:
    return (
        'from(bucket: "${bucket}")\n'
        "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
        f'  |> filter(fn: (r) => r._measurement == "{measurement}" and r.car == "${{car}}" and r._field == "{field}")\n'
        f"  |> aggregateWindow(every: v.windowPeriod, fn: {agg}, createEmpty: true)\n"
        '  |> keep(columns: ["_time", "_value"])'
    )


def flux_channel_states(window: str | None) -> str:
    rng = f"range(start: -{window})" if window else "range(start: v.timeRangeStart, stop: v.timeRangeStop)"
    tail = "  |> last()\n" if window else "  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)\n"
    return (
        'from(bucket: "${bucket}")\n'
        f"  |> {rng}\n"
        '  |> filter(fn: (r) => r._measurement == "channel_state" and r.car == "${car}" and r._field == "status_code"'
        " and r.channel !~ /^sim_/)\n"
        f"{tail}"
        '  |> keep(columns: ["_time", "_value", "channel"])'
    )


FLUX_ALARM_HISTORY = (
    'from(bucket: "${bucket}")\n'
    "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
    '  |> filter(fn: (r) => r._measurement == "alert" and r.car == "${car}")\n'
    # rowKey is _time only: rule and channel are group-key tags, kept per table,
    # so rows without a channel tag (older data) cannot break the pivot.
    '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
    "  |> group()\n"
    '  |> keep(columns: ["_time", "severity", "previous", "rule", "channel", "message"])\n'
    '  |> sort(columns: ["_time"], desc: true)\n'
    "  |> limit(n: 200)"
)

FLUX_ALARM_ANNOTATIONS = (
    'from(bucket: "${bucket}")\n'
    "  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n"
    '  |> filter(fn: (r) => r._measurement == "alert" and r.car == "${car}" and r._field == "message")\n'
    '  |> keep(columns: ["_time", "_value", "rule"])'
)


# --------------------------------------------------------------- styling
def status_mappings() -> list[dict[str, Any]]:
    options = {
        str(int(s)): {"text": s.label, "color": STATUS_COLORS[s], "index": i} for i, s in enumerate(Status)
    }
    return [{"type": "value", "options": options}]


def steps(*pairs: tuple[float | None, str]) -> dict[str, Any]:
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for v, c in pairs]}


def channel_thresholds(rules: list[Rule], channel: str) -> dict[str, Any]:
    """Grafana steps for a channel from its threshold rules.

    Several rules on one channel (oil pressure at speed and at idle) are merged
    conservatively: the highest 'below' levels and the lowest 'above' levels.
    """
    above_w = above_c = below_w = below_c = None
    for r in rules:
        if r.type != "threshold" or r.channels != (channel,):
            continue
        if r.above:
            above_w = r.above.warning if above_w is None else min(above_w, r.above.warning or above_w)
            above_c = r.above.critical if above_c is None else min(above_c, r.above.critical or above_c)
        if r.below:
            below_w = r.below.warning if below_w is None else max(below_w, r.below.warning or below_w)
            below_c = r.below.critical if below_c is None else max(below_c, r.below.critical or below_c)
    if all(v is None for v in (above_w, above_c, below_w, below_c)):
        return steps((None, "blue"))
    pairs: list[tuple[float | None, str]] = [(None, "red" if below_c is not None else ("orange" if below_w is not None else "green"))]
    if below_c is not None:
        pairs.append((below_c, "orange" if below_w is not None else "green"))
    if below_w is not None:
        pairs.append((below_w, "green"))
    if above_w is not None:
        pairs.append((above_w, "orange"))
    if above_c is not None:
        pairs.append((above_c, "red"))
    return steps(*pairs)


def can_age_thresholds(rules: list[Rule]) -> dict[str, Any]:
    for r in rules:
        if r.type == "can_age" and r.above:
            pairs = [(None, "green")]
            if r.above.warning is not None:
                pairs.append((r.above.warning, "orange"))
            if r.above.critical is not None:
                pairs.append((r.above.critical, "red"))
            return steps(*pairs)
    return steps((None, "blue"))


# ---------------------------------------------------------------- panels
class Builder:
    def __init__(self) -> None:
        self.panels: list[dict[str, Any]] = []
        self._id = 0
        self.y = 0

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def add(self, panel: dict[str, Any]) -> dict[str, Any]:
        panel["id"] = self.next_id()
        self.panels.append(panel)
        return panel

    def row(self, title: str) -> None:
        self.add({"type": "row", "title": title, "collapsed": False, "panels": [],
                  "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1


def target(query: str, ref: str = "A") -> dict[str, Any]:
    return {"refId": ref, "datasource": DS, "query": query}


def stat(title: str, targets: list[dict], pos: dict, *, unit: str = "none", decimals: int | None = None,
         mappings: list | None = None, thresholds: dict | None = None, color_mode: str = "background",
         strings: bool = False, description: str = "", overrides: list | None = None,
         no_value: str = "NO DATA") -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "unit": unit,
        "mappings": mappings or [],
        "thresholds": thresholds or steps((None, "text")),
        "color": {"mode": "thresholds"},
        "noValue": no_value,
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description, "datasource": DS, "gridPos": pos,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if strings else "", "values": False},
            "colorMode": color_mode, "graphMode": "none", "textMode": "auto", "justifyMode": "center",
            "orientation": "auto", "wideLayout": True, "showPercentChange": False,
        },
    }


def by_ref(ref: str, props: dict[str, Any]) -> dict[str, Any]:
    return {"matcher": {"id": "byFrameRefID", "options": ref},
            "properties": [{"id": k, "value": v} for k, v in props.items()]}


def timeseries(title: str, series: list[dict[str, Any]], pos: dict, *, unit: str, time_from: str | None,
               thresholds: dict | None = None, description: str = "") -> dict[str, Any]:
    targets, overrides = [], []
    for i, s in enumerate(series):
        ref = chr(ord("A") + i)
        targets.append(target(flux_series(s["measurement"], s["field"], s.get("agg", "mean")), ref))
        props: dict[str, Any] = {"displayName": s["label"]}
        if s.get("right_unit"):
            props["custom.axisPlacement"] = "right"
            props["unit"] = s["right_unit"]
        overrides.append(by_ref(ref, props))
    custom = {
        "drawStyle": "line", "lineWidth": 1, "fillOpacity": 6, "showPoints": "never",
        "spanNulls": 1500, "lineInterpolation": "linear", "axisPlacement": "auto",
        "thresholdsStyle": {"mode": "dashed" if thresholds else "off"},
    }
    panel = {
        "type": "timeseries", "title": title, "description": description, "datasource": DS, "gridPos": pos,
        "targets": targets,
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"},
                         "thresholds": thresholds or steps((None, "green"))},
            "overrides": overrides,
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "none"}},
    }
    if time_from:
        panel["timeFrom"] = time_from
        panel["hideTimeOverride"] = False
    return panel


# ------------------------------------------------------------- dashboard
def build(dash_cfg: dict[str, Any], alerts_cfg: dict[str, Any], layout: ChannelLayout) -> dict[str, Any]:
    check_keys(dash_cfg, {"title", "uid", "refresh", "car", "bucket", "labels", "gauges", "graphs"}, "dashboard.yaml")
    rules = parse_rules(alerts_cfg.get("rules"), layout)
    link = LinkThresholds.from_config(alerts_cfg.get("link"))
    labels: dict[str, str] = dash_cfg["labels"]
    names = {c.name for c in layout.channels}

    def label(ch: str) -> str:
        if ch not in names:
            raise ConfigError(f"dashboard.yaml: unknown channel {ch!r}")
        return labels.get(ch, ch)

    b = Builder()

    # ---- status strip
    w = 4
    b.add(stat("Vehicle status", [target(flux_last("alerts_active", "vehicle_status_code"))],
               {"h": 4, "w": w, "x": 0, "y": b.y}, mappings=status_mappings(),
               description="Worst status over every vehicle channel, including active alarms."))
    b.add(stat("Telemetry link", [target(flux_last("link", "status_code"))],
               {"h": 4, "w": w, "x": w, "y": b.y}, mappings=status_mappings(),
               description="Radio link health measured at the pit: loss, staleness, lost."))
    b.add(stat("Active alarms",
               [target(flux_last("alerts_active", "critical_count"), "A"),
                target(flux_last("alerts_active", "warning_count"), "B")],
               {"h": 4, "w": w, "x": 2 * w, "y": b.y},
               overrides=[by_ref("A", {"displayName": "Critical", "thresholds": steps((None, "green"), (1, "red"))}),
                          by_ref("B", {"displayName": "Warning", "thresholds": steps((None, "green"), (1, "orange"))})]))
    b.add(stat("Simulated scenario", [target(flux_last("sim", "scenario"))],
               {"h": 4, "w": w, "x": 3 * w, "y": b.y}, strings=True, color_mode="none",
               description="Fault scenario the fake ECU is running (lab only; NO DATA on a real car)."))
    b.add(stat("Session uptime", [target(flux_last("link", "session_uptime_s"))],
               {"h": 4, "w": w, "x": 4 * w, "y": b.y}, unit="dtdurations", color_mode="none"))
    b.add(stat("Latest packet age", [target(flux_last("link", "last_packet_age_ms"))],
               {"h": 4, "w": w, "x": 5 * w, "y": b.y}, unit="ms", decimals=0,
               thresholds=steps((None, "green"), (link.stale_after_s * 1000, "orange"), (link.lost_after_s * 1000, "red"))))
    b.y += 4
    b.add(stat("Active alarm list", [target(flux_last("alerts_active", "summary"))],
               {"h": 3, "w": 24, "x": 0, "y": b.y}, strings=True, color_mode="none", no_value="pit receiver not reporting"))
    b.y += 3

    # ---- gauges
    b.row("Primary vehicle health")
    gauge_w = 24 // max(1, len(dash_cfg["gauges"]))
    for i, g in enumerate(dash_cfg["gauges"]):
        check_keys(g, GAUGE_KEYS, f"dashboard.gauges[{i}]", required={"channel", "unit", "min", "max"})
        b.add({
            "type": "gauge", "title": label(g["channel"]), "datasource": DS,
            "gridPos": {"h": 7, "w": gauge_w, "x": i * gauge_w, "y": b.y},
            "targets": [target(flux_last("vehicle", g["channel"]))],
            "fieldConfig": {"defaults": {
                "unit": g["unit"], "min": g["min"], "max": g["max"], "decimals": g.get("decimals"),
                "thresholds": channel_thresholds(rules, g["channel"]), "color": {"mode": "thresholds"},
                "noValue": "NO DATA", "displayName": label(g["channel"]),
            }, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showThresholdMarkers": True, "showThresholdLabels": False, "orientation": "auto",
                        "sizing": "auto", "minVizWidth": 75, "minVizHeight": 75},
        })
    b.y += 7
    tiles = stat("Channel status", [target(flux_channel_states("15s"))], {"h": 4, "w": 24, "x": 0, "y": b.y},
                 mappings=status_mappings(),
                 description="Per channel: NORMAL, WARNING, CRITICAL, STALE or NO DATA (liveness plus alarms).")
    tiles["fieldConfig"]["defaults"]["displayName"] = "${__field.labels.channel}"
    b.add(tiles)
    b.y += 4

    # ---- vehicle trends
    b.row("Vehicle trends (fixed windows: 1, 5 and 15 minutes)")
    x = 0
    row_h = 8
    for i, g in enumerate(dash_cfg["graphs"]):
        check_keys(g, GRAPH_KEYS, f"dashboard.graphs[{i}]", required={"title", "time", "channels", "unit", "width"})
        width = int(g["width"])
        if x + width > 24:
            x = 0
            b.y += row_h
        right = g.get("right") or {}
        series = [{"measurement": "vehicle", "field": ch, "label": label(ch), "agg": g.get("agg", "mean"),
                   "right_unit": right.get(ch)} for ch in g["channels"]]
        thr = channel_thresholds(rules, g["thresholds"]) if g.get("thresholds") else None
        title = f"{g['title']} (last {g['time'].replace('m', ' min')})"
        b.add(timeseries(title, series, {"h": row_h, "w": width, "x": x, "y": b.y},
                         unit=g["unit"], time_from=g["time"], thresholds=thr))
        x += width
    b.y += row_h

    # ---- telemetry health
    b.row("Telemetry link health")
    link_stats = [
        ("Packet rate", "link", "packet_rate", "suffix: pkt/s", 1, None),
        ("Packet loss (recent)", "link", "loss_pct_window", "percent", 1,
         steps((None, "green"), (link.degraded_loss_pct, "orange"), (link.critical_loss_pct, "red"))),
        ("Packet loss (session)", "link", "loss_pct", "percent", 2,
         steps((None, "green"), (link.degraded_loss_pct, "orange"), (link.critical_loss_pct, "red"))),
        ("Packets received", "link", "packets_received", "none", 0, None),
        ("Missing packets", "link", "packets_missing", "none", 0, None),
        ("CAN message age (worst)", "car_status", "can_age_max_ms", "ms", 0, can_age_thresholds(rules)),
        ("Received bandwidth", "link", "bits_per_s", "bps", 0, None),
        ("Link availability", "link", "availability_pct", "percent", 1, None),
    ]
    for i, (title, meas, field, unit, dec, thr) in enumerate(link_stats):
        b.add(stat(title, [target(flux_last(meas, field))], {"h": 4, "w": 3, "x": 3 * i, "y": b.y},
                   unit=unit, decimals=dec, thresholds=thr, color_mode="background" if thr else "none"))
    b.y += 4
    link_graphs = [
        ("Packet rate and recent loss (last 5 min)", "suffix: pkt/s",
         [{"measurement": "link", "field": "packet_rate", "label": "Packet rate", "agg": "mean"},
          {"measurement": "link", "field": "loss_pct_window", "label": "Recent loss %", "agg": "max", "right_unit": "percent"}]),
        ("Staleness: packet age and CAN age (last 5 min)", "ms",
         [{"measurement": "link", "field": "last_packet_age_ms", "label": "Latest packet age", "agg": "max"},
          {"measurement": "car_status", "field": "can_age_max_ms", "label": "Worst CAN message age", "agg": "max"},
          {"measurement": "link", "field": "delay_excess_ms", "label": "Radio jitter (excess delay)", "agg": "max"}]),
        ("Received bandwidth (last 5 min)", "bps",
         [{"measurement": "link", "field": "bits_per_s", "label": "Bits per second at the pit", "agg": "mean"}]),
        ("CAN bus on the car (last 5 min)", "none",
         [{"measurement": "car_status", "field": "can_fps", "label": "CAN frames/s", "agg": "mean"},
          {"measurement": "car_status", "field": "can_counter_gaps", "label": "Counter gaps (total)", "agg": "max", "right_unit": "none"}]),
    ]
    for i, (title, unit, series) in enumerate(link_graphs):
        b.add(timeseries(title, series, {"h": 8, "w": 12, "x": 12 * (i % 2), "y": b.y + 8 * (i // 2)},
                         unit=unit, time_from="5m"))
    b.y += 16

    # ---- alarms
    b.row("Alarms")
    timeline = {
        "type": "state-timeline", "title": "Channel status (last 15 min)", "datasource": DS,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": b.y}, "timeFrom": "15m",
        "targets": [target(flux_channel_states(None))],
        "fieldConfig": {"defaults": {"mappings": status_mappings(), "color": {"mode": "thresholds"},
                                     "thresholds": steps((None, "green")), "displayName": "${__field.labels.channel}",
                                     "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
        "options": {"showValue": "never", "rowHeight": 0.85, "mergeValues": True, "alignValue": "left",
                    "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
    }
    b.add(timeline)
    b.y += 10
    severity_colors = {s.label: {"text": s.label, "color": STATUS_COLORS[s]} for s in Status}
    b.add({
        "type": "table", "title": "Alarm history (dashboard time range)", "datasource": DS,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": b.y},
        "targets": [target(FLUX_ALARM_HISTORY)],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                        "overrides": [{"matcher": {"id": "byName", "options": "severity"},
                                       "properties": [{"id": "mappings", "value": [{"type": "value", "options": severity_colors}]},
                                                      {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}]},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    })
    b.y += 10

    links = [
        {"title": f"Last {m} min", "type": "link", "url": f"/d/{dash_cfg['uid']}?from=now-{m}m&to=now&refresh={dash_cfg['refresh']}",
         "keepTime": False, "targetBlank": False, "icon": "dashboard", "includeVars": False, "asDropdown": False,
         "tags": [], "tooltip": f"Show the last {m} minutes"}
        for m in (1, 5, 15)
    ]
    return {
        "uid": dash_cfg["uid"],
        "title": dash_cfg["title"],
        "description": "GENERATED by scripts/generate_dashboard.py from config/dashboard.yaml, config/alerts.yaml "
                       "and config/channels.yaml. Do not edit by hand. All CAN data shown is SIMULATED in Phase 0.",
        "tags": ["uvfr", "telemetry", "simulated"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": dash_cfg["refresh"],
        "schemaVersion": 39,
        "version": 1,
        "time": {"from": "now-15m", "to": "now"},
        "timepicker": {"refresh_intervals": ["1s", "2s", "5s", "10s", "30s", "1m"]},
        "templating": {"list": [
            {"type": "constant", "name": "bucket", "query": dash_cfg["bucket"], "hide": 2},
            {"type": "constant", "name": "car", "query": dash_cfg["car"], "hide": 2},
        ]},
        "annotations": {"list": [
            {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True, "hide": True,
             "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"},
            {"datasource": DS, "enable": True, "iconColor": "red", "name": "Alarm changes",
             "target": {"refId": "Anno", "query": FLUX_ALARM_ANNOTATIONS}},
        ]},
        "links": links,
        "panels": b.panels,
    }


def render(dash_path: str, alerts_path: str, channels_path: str) -> str:
    dashboard = build(load_yaml(dash_path), load_yaml(alerts_path), ChannelLayout.load(channels_path))
    return json.dumps(dashboard, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dashboard", default="config/dashboard.yaml")
    parser.add_argument("--alerts", default="config/alerts.yaml")
    parser.add_argument("--channels", default="config/channels.yaml")
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the output file is out of date")
    args = parser.parse_args(argv)
    try:
        text = render(args.dashboard, args.alerts, args.channels)
    except ConfigError as exc:
        print(f"generate_dashboard: {exc}", file=sys.stderr)
        return 2
    out = resolve_path(args.output)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{args.output} is out of date; run make dashboard", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {args.output} ({len(json.loads(text)['panels'])} panels)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
