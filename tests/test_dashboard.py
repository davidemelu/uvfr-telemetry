"""Generated Grafana dashboard: up to date, and thresholds come from alerts.yaml."""

from __future__ import annotations

import copy
import json

import pytest

from common.config import ConfigError, load_yaml
from common.protocol import ChannelLayout
from scripts.generate_dashboard import OUTPUT, build, render


@pytest.fixture(scope="module")
def dashboard():
    return json.loads(render("config/dashboard.yaml", "config/alerts.yaml", "config/channels.yaml"))


def panel(dashboard, title):
    return next(p for p in dashboard["panels"] if p["title"] == title)


def step_values(p):
    return [(s["value"], s["color"]) for s in p["fieldConfig"]["defaults"]["thresholds"]["steps"]]


def test_committed_dashboard_is_up_to_date():
    expected = render("config/dashboard.yaml", "config/alerts.yaml", "config/channels.yaml")
    with open(OUTPUT, encoding="utf-8") as fh:
        assert fh.read() == expected, "run `make dashboard` and commit the result"


def test_gauge_colours_match_alarm_thresholds(dashboard):
    assert step_values(panel(dashboard, "Coolant")) == [(None, "green"), (105, "orange"), (110, "red")]
    assert step_values(panel(dashboard, "Battery")) == [(None, "red"), (11.5, "orange"), (12.0, "green")]
    assert step_values(panel(dashboard, "RPM")) == [(None, "green"), (13500, "orange"), (14000, "red")]
    assert step_values(panel(dashboard, "Oil pressure")) == [(None, "red"), (100, "orange"), (150, "green")]
    assert step_values(panel(dashboard, "Throttle")) == [(None, "blue")]  # no alarm on throttle


def test_changing_alerts_yaml_changes_the_dashboard(dashboard):
    alerts = load_yaml("config/alerts.yaml")
    alerts = copy.deepcopy(alerts)
    for rule in alerts["rules"]:
        if rule["id"] == "coolant_high":
            rule["above"] = {"warning": 100, "critical": 104}
    rebuilt = build(load_yaml("config/dashboard.yaml"), alerts, ChannelLayout.load("config/channels.yaml"))
    assert step_values(panel(rebuilt, "Coolant")) == [(None, "green"), (100, "orange"), (104, "red")]


def test_link_panels_use_link_thresholds(dashboard):
    link = load_yaml("config/alerts.yaml")["link"]
    assert step_values(panel(dashboard, "Latest packet age")) == [
        (None, "green"), (link["stale_after_s"] * 1000, "orange"), (link["lost_after_s"] * 1000, "red")]
    assert step_values(panel(dashboard, "Packet loss (recent)")) == [
        (None, "green"), (link["degraded_loss_pct"], "orange"), (link["critical_loss_pct"], "red")]


def test_required_panels_exist(dashboard):
    titles = {p["title"] for p in dashboard["panels"]}
    for t in ("RPM", "Coolant", "Oil pressure", "Battery", "Throttle", "Wheel speed", "Vehicle status",
              "Telemetry link", "Packet rate", "Packet loss (recent)", "Packets received", "Missing packets",
              "Latest packet age", "CAN message age (worst)", "Simulated scenario", "Session uptime",
              "Channel status", "Alarm history (dashboard time range)", "Channel status (last 15 min)"):
        assert t in titles, t
    windows = {p.get("timeFrom") for p in dashboard["panels"] if p["type"] == "timeseries"}
    assert {"1m", "5m", "15m"} <= windows


def test_status_mapping_covers_all_five_states(dashboard):
    mapping = panel(dashboard, "Vehicle status")["fieldConfig"]["defaults"]["mappings"][0]["options"]
    assert [mapping[str(i)]["text"] for i in range(5)] == ["NORMAL", "WARNING", "CRITICAL", "STALE", "NO DATA"]


def test_layout_is_valid(dashboard):
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    for p in dashboard["panels"]:
        pos = p["gridPos"]
        assert 0 <= pos["x"] and pos["x"] + pos["w"] <= 24, p["title"]
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == "uvfr-influx"
            assert "${bucket}" in t["query"] and "${car}" in t["query"]


def test_unknown_channel_in_layout_is_rejected():
    cfg = load_yaml("config/dashboard.yaml")
    cfg["gauges"][0]["channel"] = "flux_capacitor"
    with pytest.raises(ConfigError, match="flux_capacitor"):
        build(cfg, load_yaml("config/alerts.yaml"), ChannelLayout.load("config/channels.yaml"))
