"""channels.yaml loading and validation against the DBC."""

from __future__ import annotations

import copy

import pytest

from common.config import ConfigError, load_yaml
from common.protocol import ChannelLayout, Missing


@pytest.fixture(scope="module")
def raw_channels():
    return load_yaml("config/channels.yaml")


def test_shipped_layout(dbc):
    layout = ChannelLayout.load("config/channels.yaml", dbc)
    vehicle = [c for c in layout.channels if not c.name.startswith("sim_")]
    assert 10 <= len(vehicle) <= 15, "MVP target is 10 to 15 vehicle channels"
    assert all(c.rate_hz <= 10 for c in layout.channels)
    for name in ("rpm", "coolant_temperature", "oil_pressure", "battery_voltage", "throttle_position", "vehicle_speed"):
        layout.by_name(name)
    assert layout.by_name("coolant_temperature").unit == "degC"
    assert layout.by_name("rpm").stale_after_ms == 250  # max(250, 5 x 10 ms)
    assert layout.by_name("coolant_temperature").stale_after_ms == 500  # 5 x 100 ms


def test_layout_hash_ignores_rates_but_not_encoding(raw_channels, dbc):
    base = ChannelLayout.from_config(raw_channels, dbc).config_hash
    cfg = copy.deepcopy(raw_channels)
    cfg["channels"][0]["rate_hz"] = 5
    assert ChannelLayout.from_config(cfg, dbc).config_hash == base
    cfg["channels"][0]["wire"]["scale"] = 2
    assert ChannelLayout.from_config(cfg, dbc).config_hash != base
    cfg = copy.deepcopy(raw_channels)
    cfg["channels"][0], cfg["channels"][1] = cfg["channels"][1], cfg["channels"][0]
    assert ChannelLayout.from_config(cfg, dbc).config_hash != base


def test_hash_is_the_same_with_or_without_dbc(raw_channels, dbc):
    assert ChannelLayout.from_config(raw_channels).config_hash == ChannelLayout.from_config(raw_channels, dbc).config_hash


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c: c["channels"][0].update(rate_hz=3), "divide"),
        (lambda c: c["channels"][0].update(rate_hz=20), "between"),
        (lambda c: c["channels"][1].update(name="rpm"), "duplicate"),
        (lambda c: c["channels"][0].update(name="Bad-Name"), "lower_snake_case"),
        (lambda c: c["channels"][0].update(signal="SIM_ENGINE_FAST.NoSuchSignal"), "no signal"),
        (lambda c: c["channels"][0].update(signal="NO_SUCH_MESSAGE.EngineSpeed"), "no message"),
        (lambda c: c["channels"][0].update(signal="EngineSpeed"), "MESSAGE.Signal"),
        (lambda c: c["channels"][0]["wire"].update(type="u8"), "maximum"),  # 16000 rpm in a byte
        (lambda c: c["channels"][5]["wire"].update(type="u16"), "minimum"),  # coolant -40 degC unsigned
        (lambda c: c["channels"][0]["wire"].update(type="f32"), "wire type"),
        (lambda c: c["channels"][0]["wire"].update(scale=0), "positive"),
        (lambda c: c["channels"][0].update(typo=1), "unknown key"),
        (lambda c: c["protocol"].update(status_rate_hz=3), "divide"),
        (lambda c: c.update(channels=[]), "non-empty"),
        (lambda c: c.update(channels=c["channels"] * 3), "at most"),
    ],
)
def test_invalid_layouts_are_rejected(raw_channels, dbc, mutate, match):
    cfg = copy.deepcopy(raw_channels)
    mutate(cfg)
    with pytest.raises(ConfigError, match=match):
        ChannelLayout.from_config(cfg, dbc)


def test_sentinels_are_outside_the_valid_range(dbc):
    layout = ChannelLayout.load("config/channels.yaml", dbc)
    for spec in layout.channels:
        w = spec.wire
        assert w.no_data_raw not in range(w.valid_min, w.valid_max + 1)
        assert w.stale_raw not in range(w.valid_min, w.valid_max + 1)
        assert spec.from_raw(w.no_data_raw) is Missing.NO_DATA
        assert spec.from_raw(w.stale_raw) is Missing.STALE
