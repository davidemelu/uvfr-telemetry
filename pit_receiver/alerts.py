"""Alert engine: every threshold comes from config/alerts.yaml.

Rule types
  threshold  value above / below warning and critical levels, with hysteresis
             (an alarm clears only once the value is back past the level by
             `hysteresis`) and an optional hold time (stays raised for at
             least hold_s, so a 0.3 s over-rev spike is still seen)
  rate       rate of change (units per second, least-squares slope over
             window_s) above / below levels, e.g. coolant rising rapidly
  stale      channel is STALE (CAN timeout on the car or not received)
  no_data    channel was live and now reports NO DATA (sensor fault)
  frozen     value has not changed at all for window_s while it should be
             moving (the `when` condition, e.g. engine running)
  link       telemetry link: lost, stale, packet loss, or layout mismatch
  can_age    oldest vehicle CAN message age reported by the car node (ms),
             i.e. a CAN timeout on the car, independent of the radio

`when: {channel, above, below}` gates a rule on another channel, e.g. oil
pressure is only judged above 3000 rpm.

Threshold rules look at the extreme value since the previous evaluation
(highest for `above`, lowest for `below`), so short spikes between evaluations
are not missed. A raised alarm is not cleared just because its channel goes
STALE: losing data is not evidence that the problem went away.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from common.config import ConfigError, check_keys
from common.protocol import ChannelLayout, Missing
from pit_receiver.channels import ChannelView
from pit_receiver.link_stats import LinkSnapshot, LinkThresholds
from pit_receiver.status import Status

RULE_TYPES = {"threshold", "rate", "stale", "no_data", "frozen", "link", "can_age"}
CHANNELLESS = {"link", "can_age"}
LINK_CONDITIONS = {"lost", "stale", "loss", "config_mismatch"}
LAB_ONLY_PREFIX = "sim_"


@dataclass(frozen=True)
class Levels:
    warning: float | None = None
    critical: float | None = None


@dataclass(frozen=True)
class When:
    channel: str
    above: float | None = None
    below: float | None = None

    def holds(self, value: float | None) -> bool:
        if value is None:
            return False
        if self.above is not None and value <= self.above:
            return False
        if self.below is not None and value >= self.below:
            return False
        return True


@dataclass(frozen=True)
class Rule:
    id: str
    type: str
    message: str
    channels: tuple[str, ...] = ()
    above: Levels | None = None
    below: Levels | None = None
    hysteresis: float = 0.0
    hold_s: float = 0.0
    window_s: float = 10.0
    tolerance: float = 0.0
    when: When | None = None
    severity: Status = Status.WARNING
    condition: str = ""


RULE_KEYS = {"id", "type", "message", "channel", "channels", "above", "below", "hysteresis", "hold_s",
             "window_s", "tolerance", "when", "severity", "condition"}


def _levels(raw: Any, where: str) -> Levels | None:
    if raw is None:
        return None
    check_keys(raw, {"warning", "critical"}, where, required=set())
    lv = Levels(raw.get("warning"), raw.get("critical"))
    if lv.warning is None and lv.critical is None:
        raise ConfigError(f"{where}: needs warning and/or critical")
    return lv


def parse_rules(raw_rules: Any, layout: ChannelLayout) -> list[Rule]:
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError("alerts.rules must be a non-empty list")
    names = {c.name for c in layout.channels}
    vehicle = tuple(c.name for c in layout.channels if not c.name.startswith(LAB_ONLY_PREFIX))
    rules: list[Rule] = []
    ids: set[str] = set()
    for i, raw in enumerate(raw_rules):
        where = f"alerts.rules[{i}]"
        check_keys(raw, RULE_KEYS, where, required={"id", "type", "message"})
        rid, rtype = raw["id"], raw["type"]
        where = f"alerts.rules[{i}] ({rid})"
        if rid in ids:
            raise ConfigError(f"{where}: duplicate rule id")
        ids.add(rid)
        if rtype not in RULE_TYPES:
            raise ConfigError(f"{where}: type must be one of {sorted(RULE_TYPES)}")

        if "channel" in raw and "channels" in raw:
            raise ConfigError(f"{where}: use channel or channels, not both")
        chans = raw.get("channels", [raw["channel"]] if "channel" in raw else [])
        if chans == "all":
            chans = list(vehicle)
        chans = tuple(chans)
        for c in chans:
            if c not in names:
                raise ConfigError(f"{where}: unknown channel {c!r}")
        if rtype in CHANNELLESS and chans:
            raise ConfigError(f"{where}: {rtype} rules do not take channels")
        if rtype not in CHANNELLESS and not chans:
            raise ConfigError(f"{where}: needs channel or channels")
        if rtype in ("threshold", "rate") and len(chans) != 1:
            raise ConfigError(f"{where}: {rtype} rules take exactly one channel")

        above = _levels(raw.get("above"), f"{where}.above")
        below = _levels(raw.get("below"), f"{where}.below")
        if rtype in ("threshold", "rate") and not (above or below):
            raise ConfigError(f"{where}: needs above and/or below levels")
        if rtype == "can_age" and (above is None or below is not None):
            raise ConfigError(f"{where}: can_age rules need above levels only")
        for lv, name in ((above, "above"), (below, "below")):
            if lv and lv.warning is not None and lv.critical is not None:
                ordered = lv.critical > lv.warning if name == "above" else lv.critical < lv.warning
                if not ordered:
                    raise ConfigError(f"{where}: {name}.critical must be beyond {name}.warning")

        when = None
        if raw.get("when") is not None:
            w = check_keys(raw["when"], {"channel", "above", "below"}, f"{where}.when", required={"channel"})
            if w["channel"] not in names:
                raise ConfigError(f"{where}.when: unknown channel {w['channel']!r}")
            when = When(w["channel"], w.get("above"), w.get("below"))

        severity = Status.WARNING
        if "severity" in raw:
            try:
                severity = {"warning": Status.WARNING, "critical": Status.CRITICAL}[raw["severity"]]
            except KeyError:
                raise ConfigError(f"{where}: severity must be warning or critical") from None
        condition = raw.get("condition", "")
        if rtype == "link" and condition not in LINK_CONDITIONS:
            raise ConfigError(f"{where}: link condition must be one of {sorted(LINK_CONDITIONS)}")

        rules.append(Rule(
            id=rid, type=rtype, message=str(raw["message"]), channels=chans, above=above, below=below,
            hysteresis=float(raw.get("hysteresis", 0.0)), hold_s=float(raw.get("hold_s", 0.0)),
            window_s=float(raw.get("window_s", 10.0)), tolerance=float(raw.get("tolerance", 0.0)),
            when=when, severity=severity, condition=condition,
        ))
    return rules


@dataclass
class AlertState:
    rule: Rule
    channel: str  # "" for link rules
    severity: Status = Status.NORMAL
    since: float | None = None
    hold_until: float = 0.0
    value: float | None = None
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.rule.id}:{self.channel}" if self.channel else self.rule.id

    @property
    def active(self) -> bool:
        return self.severity in (Status.WARNING, Status.CRITICAL)

    def describe(self) -> str:
        text = self.rule.message
        if self.channel and self.rule.type in ("stale", "no_data", "frozen"):
            text = f"{text}: {self.channel}"
        return f"{text} ({self.detail})" if self.detail else text


@dataclass(frozen=True)
class AlertEvent:
    state_key: str
    rule_id: str
    channel: str
    previous: Status
    severity: Status
    message: str
    value: float | None


@dataclass
class _History:
    samples: deque = field(default_factory=deque)  # (t, value)
    peak: float | None = None  # highest since last evaluation
    trough: float | None = None  # lowest since last evaluation


def _slope(samples: list[tuple[float, float]]) -> float | None:
    n = len(samples)
    if n < 5:
        return None
    mt = sum(t for t, _ in samples) / n
    mv = sum(v for _, v in samples) / n
    den = sum((t - mt) ** 2 for t, _ in samples)
    if den <= 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in samples) / den


class AlertEngine:
    def __init__(self, rules: list[Rule], layout: ChannelLayout, link: LinkThresholds, units: dict[str, str] | None = None):
        self.rules = rules
        self.link = link
        self.units = units or {c.name: c.unit for c in layout.channels}
        self.vehicle_channels = [c.name for c in layout.channels if not c.name.startswith(LAB_ONLY_PREFIX)]
        self.states: dict[str, AlertState] = {}
        for rule in rules:
            for ch in rule.channels or ("",):
                st = AlertState(rule, ch)
                self.states[st.key] = st
        horizon = max([r.window_s for r in rules if r.type in ("rate", "frozen")] + [10.0])
        self._horizon = horizon + 1.0
        self._history = {c.name: _History() for c in layout.channels}
        self._seen_live: set[str] = set()

    # --------------------------------------------------------------- input
    @classmethod
    def from_config(cls, alerts_cfg: dict[str, Any], layout: ChannelLayout) -> AlertEngine:
        link = LinkThresholds.from_config(alerts_cfg.get("link"))
        return cls(parse_rules(alerts_cfg.get("rules"), layout), layout, link)

    def observe(self, values: dict[str, float | Missing], t: float) -> None:
        for name, value in values.items():
            if isinstance(value, Missing):
                continue
            h = self._history[name]
            h.samples.append((t, value))
            while h.samples and h.samples[0][0] < t - self._horizon:
                h.samples.popleft()
            h.peak = value if h.peak is None else max(h.peak, value)
            h.trough = value if h.trough is None else min(h.trough, value)
            self._seen_live.add(name)

    # ------------------------------------------------------------ evaluate
    def evaluate(
        self, now: float, views: dict[str, ChannelView], link: LinkSnapshot, can_age_ms: float | None = None
    ) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for st in self.states.values():
            if st.rule.type == "can_age":
                if can_age_ms is None:
                    continue  # no recent STATUS from the car: cannot judge
                sev = self._grade(st, can_age_ms, st.rule)
                target, value, detail = sev, can_age_ms, f"oldest CAN message {can_age_ms:.0f} ms"
            else:
                target, value, detail = self._target(st, now, views, link)
            if target is None:  # cannot judge right now: keep the current state
                continue
            # hold_s only delays clearing; escalation and de-escalation are immediate.
            if target is not Status.NORMAL:
                st.hold_until = now + st.rule.hold_s
            elif st.active and now < st.hold_until:
                target = st.severity  # held
            if target is not st.severity:
                previous = st.severity
                st.severity = target
                st.since = now if st.active else None
                st.value, st.detail = value, detail
                events.append(AlertEvent(st.key, st.rule.id, st.channel, previous, target, st.describe(), value))
            elif st.active and target is not Status.NORMAL:
                st.value, st.detail = value, detail
        for h in self._history.values():
            h.peak = h.trough = None
        return events

    def _target(self, st: AlertState, now: float, views: dict[str, ChannelView], link: LinkSnapshot):
        rule = st.rule
        if rule.type == "link":
            return self._link_target(rule, link)

        view = views.get(st.channel)
        if view is None:
            return None, None, ""
        if rule.type == "stale":
            if view.status is Status.STALE and st.channel in self._seen_live:
                return rule.severity, view.last_value, view.reason
            return Status.NORMAL, None, ""
        if rule.type == "no_data":
            if view.status is Status.NO_DATA and st.channel in self._seen_live:
                return rule.severity, None, view.reason
            return Status.NORMAL, None, ""

        if rule.when is not None:
            gate = views.get(rule.when.channel)
            if gate is None or gate.value is None:
                return None, None, ""  # gating channel not live: cannot judge
            if not rule.when.holds(gate.value):
                return Status.NORMAL, None, ""

        if view.value is None:  # not live: hold whatever state we are in
            return None, None, ""
        h = self._history[st.channel]
        unit = self.units.get(st.channel, "")

        if rule.type == "frozen":
            recent = [v for t, v in h.samples if t >= now - rule.window_s]
            span_ok = h.samples and h.samples[0][0] <= now - rule.window_s * 0.9
            if span_ok and len(recent) >= 5 and max(recent) - min(recent) <= rule.tolerance:
                return rule.severity, view.value, f"unchanged at {view.value:g} {unit} for {rule.window_s:g} s"
            return Status.NORMAL, None, ""

        if rule.type == "rate":
            samples = [(t, v) for t, v in h.samples if t >= now - rule.window_s]
            if not samples or samples[0][0] > now - rule.window_s * 0.6:
                return None, None, ""  # not enough history yet
            slope = _slope(samples)
            if slope is None:
                return None, None, ""
            sev = self._grade(st, slope, rule)
            return sev, slope, f"{slope:+.2f} {unit}/s"

        # threshold
        if rule.above is not None:
            value = h.peak if h.peak is not None else view.value
            sev = self._grade(st, value, rule)
            if sev is not Status.NORMAL or rule.below is None:
                return sev, value, f"{value:g} {unit}".strip()
        value = h.trough if h.trough is not None else view.value
        return self._grade(st, value, rule), value, f"{value:g} {unit}".strip()

    @staticmethod
    def _grade(st: AlertState, value: float, rule: Rule) -> Status:
        hyst = rule.hysteresis
        current = st.severity
        if rule.above is not None:
            lv = rule.above
            if lv.critical is not None and (value >= lv.critical or (current is Status.CRITICAL and value > lv.critical - hyst)):
                return Status.CRITICAL
            if lv.warning is not None and (value >= lv.warning or (st.active and value > lv.warning - hyst)):
                return Status.WARNING
        if rule.below is not None:
            lv = rule.below
            if lv.critical is not None and (value <= lv.critical or (current is Status.CRITICAL and value < lv.critical + hyst)):
                return Status.CRITICAL
            if lv.warning is not None and (value <= lv.warning or (st.active and value < lv.warning + hyst)):
                return Status.WARNING
        return Status.NORMAL

    def _link_target(self, rule: Rule, link: LinkSnapshot):
        t = self.link
        age = link.last_packet_age_s
        if rule.condition == "lost":
            if age is not None and age >= t.lost_after_s:
                return Status.CRITICAL, age, f"no packets for {age:.1f} s"
            return Status.NORMAL, None, ""
        if rule.condition == "stale":
            if age is not None and t.stale_after_s <= age < t.lost_after_s:
                return rule.severity, age, f"no packets for {age * 1000:.0f} ms"
            return Status.NORMAL, None, ""
        if rule.condition == "loss":
            loss = link.loss_pct_window
            if age is None or age >= t.lost_after_s:
                return None, None, ""  # the lost rule covers this
            if not link.loss_window_ready:
                return Status.NORMAL, loss, ""
            if loss >= t.critical_loss_pct:
                return Status.CRITICAL, loss, f"{loss:.1f}% recent packet loss"
            if loss >= t.degraded_loss_pct:
                return Status.WARNING, loss, f"{loss:.1f}% recent packet loss"
            return Status.NORMAL, loss, ""
        # config_mismatch
        if link.config_mismatch:
            return rule.severity, None, "car and pit channels.yaml differ"
        return Status.NORMAL, None, ""

    # ------------------------------------------------------------- queries
    def active(self) -> list[AlertState]:
        return sorted((s for s in self.states.values() if s.active), key=lambda s: (-s.severity.severity, s.key))

    def channel_alarm(self, channel: str) -> Status:
        """Worst value alarm (threshold / rate) on a channel."""
        worst = Status.NORMAL
        for st in self.states.values():
            if st.channel == channel and st.active and st.rule.type in ("threshold", "rate"):
                if st.severity.severity > worst.severity:
                    worst = st.severity
        return worst

    def channel_frozen(self, channel: str) -> bool:
        return any(s.channel == channel and s.active and s.rule.type == "frozen" for s in self.states.values())
