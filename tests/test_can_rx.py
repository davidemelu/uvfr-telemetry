"""Car node CAN input: decoding, counter gaps, staleness and listen-only safety."""

from __future__ import annotations

import json

import can
import pytest

from car_node.can_rx import (
    FrameDecoder,
    ListenOnlyError,
    ReceiveOnlyBus,
    VehicleState,
    check_listen_only,
    parse_ip_link_json,
)
from common.dbc import encode_physical
from common.protocol import ChannelLayout, Missing


@pytest.fixture(scope="module")
def layout(dbc):
    return ChannelLayout.load("config/channels.yaml", dbc)


def frame(dbc, name, counter=0, **values):
    message = dbc.get_message_by_name(name)
    data = {s.name: float(s.minimum) for s in message.signals}
    data.update(values)
    counter_name = next(s.name for s in message.signals if s.name.endswith("_Counter"))
    data[counter_name] = counter
    return can.Message(arbitration_id=message.frame_id, data=encode_physical(message, data), is_extended_id=False)


def test_decodes_tracked_frames(dbc, layout):
    dec = FrameDecoder(dbc, layout.messages)
    message, values = dec.decode(frame(dbc, "SIM_ENGINE_TEMPS", CoolantTemp=88.3))
    assert message.name == "SIM_ENGINE_TEMPS"
    assert values["CoolantTemp"] == pytest.approx(88.3)
    assert dec.frames == 1


def test_ignores_untracked_error_and_remote_frames(dbc):
    dec = FrameDecoder(dbc, ["SIM_ENGINE_FAST"])
    assert dec.decode(frame(dbc, "SIM_ENGINE_TEMPS")) is None
    assert dec.unknown_frames == 1
    assert dec.decode(can.Message(arbitration_id=0x100, is_error_frame=True)) is None
    assert dec.decode(can.Message(arbitration_id=0x100, is_remote_frame=True)) is None
    assert dec.decode(can.Message(arbitration_id=0x100, is_extended_id=True, data=bytes(8))) is None
    assert dec.ignored_frames == 3


def test_wrong_length_frames_count_as_decode_errors(dbc):
    dec = FrameDecoder(dbc, ["SIM_ENGINE_FAST"])
    assert dec.decode(can.Message(arbitration_id=0x100, data=b"\x01\x02\x03", is_extended_id=False)) is None
    assert dec.decode_errors == 1


def test_rolling_counter_gaps_are_detected(dbc):
    dec = FrameDecoder(dbc, ["SIM_ENGINE_FAST"])
    for c in [1, 2, 3, 6, 7, 7, 8, 0]:  # 3 frames lost after 3, a duplicate 7, 8 -> 0 skips 9..15
        dec.decode(frame(dbc, "SIM_ENGINE_FAST", counter=c))
    stats = dec.stats["SIM_ENGINE_FAST"]
    assert stats.counter_gaps == 3
    assert stats.missed_frames == 2 + 15 + 7


def test_counter_wraps_without_a_gap(dbc):
    dec = FrameDecoder(dbc, ["SIM_ENGINE_FAST"])
    for c in [14, 15, 0, 1]:
        dec.decode(frame(dbc, "SIM_ENGINE_FAST", counter=c))
    assert dec.counter_gaps == 0


def test_state_values_staleness_and_faults(dbc, layout):
    dec = FrameDecoder(dbc, layout.messages)
    state = VehicleState()
    rpm, coolant, oil = (layout.by_name(n) for n in ("rpm", "coolant_temperature", "oil_pressure"))
    assert state.channel_value(rpm, now=0.0) is Missing.NO_DATA  # never received

    message, values = dec.decode(frame(dbc, "SIM_ENGINE_FAST", EngineSpeed=9000))
    state.update(message.name, values, t=10.0)
    assert state.channel_value(rpm, now=10.1) == 9000
    assert state.channel_value(rpm, now=10.0 + rpm.stale_after_ms / 1000 + 0.01) is Missing.STALE  # CAN timeout
    assert state.message_age_s("SIM_ENGINE_FAST", 10.5) == pytest.approx(0.5)
    assert state.message_age_s("SIM_ENGINE_TEMPS", 10.5) is None

    message, values = dec.decode(frame(dbc, "SIM_ENGINE_PRESSURES", OilPressure=None))  # sensor fault
    state.update(message.name, values, t=10.0)
    assert state.channel_value(oil, now=10.05) is Missing.NO_DATA

    message, values = dec.decode(frame(dbc, "SIM_ENGINE_TEMPS", CoolantTemp=91.0))
    state.update(message.name, values, t=10.0)
    assert state.channel_value(coolant, now=10.4) == pytest.approx(91.0)
    assert state.channel_value(coolant, now=10.6) is Missing.STALE  # 500 ms timeout for a 10 Hz message


# --------------------------------------------------------------- safety rails
def test_receive_only_bus_has_no_send():
    assert not hasattr(ReceiveOnlyBus, "send")
    bus = can.Bus(interface="virtual", channel="test-ro")
    ro = ReceiveOnlyBus(bus)
    assert not hasattr(ro, "send")
    ro.shutdown()


def _ip_json(kind, ctrlmode=None):
    info = {"ifname": "x", "linkinfo": {"info_kind": kind, "info_data": {}}}
    if ctrlmode is not None:
        info["linkinfo"]["info_data"]["ctrlmode"] = ctrlmode
    return json.dumps([info])


def test_parse_ip_link_output():
    assert parse_ip_link_json(_ip_json("vcan")).kind == "vcan"
    mode = parse_ip_link_json(_ip_json("can", ["LISTEN-ONLY"]))
    assert mode.kind == "can" and mode.listen_only
    mode = parse_ip_link_json(_ip_json("can", []))
    assert mode.kind == "can" and not mode.listen_only
    assert parse_ip_link_json("not json").kind == "unknown"


def test_refuses_real_can_that_is_not_listen_only(monkeypatch):
    from car_node.can_rx import source

    monkeypatch.setattr(source, "interface_mode", lambda ch: parse_ip_link_json(_ip_json("can", [])))
    with pytest.raises(ListenOnlyError, match="listen-only"):
        check_listen_only("socketcan", "can0", require=True)
    assert "NOT listen-only" in check_listen_only("socketcan", "can0", require=False)

    monkeypatch.setattr(source, "interface_mode", lambda ch: parse_ip_link_json(_ip_json("can", ["LISTEN-ONLY"])))
    assert "LISTEN-ONLY" in check_listen_only("socketcan", "can0", require=True)

    monkeypatch.setattr(source, "interface_mode", lambda ch: parse_ip_link_json(_ip_json("vcan")))
    assert "vcan" in check_listen_only("socketcan", "vcan0", require=True)


def test_car_node_package_never_calls_send():
    """Belt and braces: no .send( on a CAN bus anywhere in the car node code."""
    import pathlib

    for path in pathlib.Path("car_node").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "bus.send(" in line or "_bus.send(" in line:
                pytest.fail(f"{path}: CAN transmit call found: {line.strip()}")
