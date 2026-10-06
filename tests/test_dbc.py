"""The simulated DBC: labelling, encoding, decoding, scaling and fault values."""

from __future__ import annotations

import pytest

from common.dbc import (
    clamp,
    counter_signal,
    decode_physical,
    encode_physical,
    fault_physical,
    fault_raw,
    in_range,
    is_counter,
)


def _message_with(dbc, signal_name):
    return next(m for m in dbc.messages if any(s.name == signal_name for s in m.signals))


def test_every_id_is_labelled_simulated(dbc):
    for message in dbc.messages:
        assert message.name.startswith("SIM_"), message.name
        assert message.comment and ("SIMULATED" in message.comment or "SIMULATION" in message.comment), message.name


def test_database_comment_disclaims_real_ids():
    text = open("dbc/simulated_uvfr.dbc", encoding="utf-8").read()
    assert "None of it is the real UVFR vehicle CAN database" in text


def test_every_message_has_cycle_time_and_counter(dbc):
    for message in dbc.messages:
        assert message.cycle_time and message.cycle_time > 0, message.name
        assert counter_signal(message) is not None, message.name
        assert message.length == 8


def test_signals_have_units_and_ranges(dbc):
    for message in dbc.messages:
        for sig in message.signals:
            assert sig.minimum is not None and sig.maximum is not None, sig.name
            assert sig.minimum < sig.maximum, sig.name
            if not is_counter(sig) and message.name != "SIM_STATUS" and sig.name not in {"Gear", "GpsFix", "GpsSatellites"}:
                assert sig.unit, f"{sig.name} has no unit"


def test_no_overlapping_ids(dbc):
    ids = [m.frame_id for m in dbc.messages]
    assert len(ids) == len(set(ids))
    assert all(not m.is_extended_frame for m in dbc.messages)


@pytest.mark.parametrize(
    ("signal", "value", "raw"),
    [
        ("CoolantTemp", 88.3, 1283),  # (88.3 + 40) / 0.1
        ("CoolantTemp", -40.0, 0),
        ("OilPressure", 412.5, 4125),
        ("BatteryVoltage", 13.86, 1386),
        ("ThrottlePosition", 100.0, 1000),
        ("Lambda", 0.87, 870),
        ("VehicleSpeed", 63.27, 6327),
    ],
)
def test_signal_scaling(dbc, signal, value, raw):
    message = _message_with(dbc, signal)
    sig = message.get_signal_by_name(signal)
    values = {s.name: float(s.minimum) for s in message.signals}
    values[signal] = value
    data = encode_physical(message, values)
    decoded_raw = message.decode(data, scaling=False)
    assert decoded_raw[signal] == raw
    decoded = decode_physical(message, data)
    assert decoded[signal] == pytest.approx(value, abs=sig.scale / 2)


def test_roundtrip_whole_message(dbc):
    message = dbc.get_message_by_name("SIM_ENGINE_FAST")
    values = {"EngineSpeed": 9876, "ThrottlePosition": 54.3, "Gear": 3, "EngineFast_Counter": 11}
    decoded = decode_physical(message, encode_physical(message, values))
    assert decoded == pytest.approx({k: float(v) for k, v in values.items()})


def test_fault_value_decodes_as_none(dbc):
    message = dbc.get_message_by_name("SIM_ENGINE_PRESSURES")
    values = {"OilPressure": None, "FuelPressure": 300.0, "Lambda": 1.0, "EnginePressures_Counter": 1}
    data = encode_physical(message, values)
    assert message.decode(data, scaling=False)["OilPressure"] == 0xFFFF
    decoded = decode_physical(message, data)
    assert decoded["OilPressure"] is None
    assert decoded["FuelPressure"] == pytest.approx(300.0)


def test_fault_helpers(dbc):
    sig = dbc.get_message_by_name("SIM_ENGINE_TEMPS").get_signal_by_name("CoolantTemp")
    assert fault_raw(sig) == 0xFFFF
    assert fault_physical(sig) == pytest.approx(0xFFFF * 0.1 - 40)
    assert not in_range(sig, fault_physical(sig))
    assert in_range(sig, 150.0)
    assert clamp(sig, 999.0) == 150.0
    assert clamp(sig, -100.0) == -40.0


def test_out_of_range_values_are_clamped_not_rejected(dbc):
    message = dbc.get_message_by_name("SIM_ENGINE_FAST")
    data = encode_physical(message, {"EngineSpeed": 25000, "ThrottlePosition": -5, "Gear": 2, "EngineFast_Counter": 0})
    decoded = decode_physical(message, data)
    assert decoded["EngineSpeed"] == 16000
    assert decoded["ThrottlePosition"] == 0


def test_missing_signal_value_is_an_error(dbc):
    message = dbc.get_message_by_name("SIM_ELECTRICAL")
    with pytest.raises(KeyError, match="BatteryVoltage"):
        encode_physical(message, {"Electrical_Counter": 0})
