"""DBC helpers shared by the simulator, car node and replay tools.

Fault convention used throughout the project: a raw value with every bit set
(0xFFFF for a 16-bit signal) means "sensor fault / not available". It decodes
to a physical value outside the signal's [min, max] range, so a decoder can
detect it without special cases. In Python the fault is represented as None.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import cantools
from cantools.database.can import Database, Message, Signal

from common.config import ConfigError, resolve_path

COUNTER_SUFFIX = "_Counter"


def load_dbc(path: str | Path) -> Database:
    p = resolve_path(path)
    if not p.is_file():
        raise ConfigError(f"DBC file not found: {p}")
    return cantools.database.load_file(str(p), database_format="dbc", strict=True)


def is_counter(signal: Signal) -> bool:
    return signal.name.endswith(COUNTER_SUFFIX)


def counter_signal(message: Message) -> Signal | None:
    for sig in message.signals:
        if is_counter(sig):
            return sig
    return None


def fault_raw(signal: Signal) -> int:
    if signal.is_signed:
        return -(1 << (signal.length - 1))
    return (1 << signal.length) - 1


def fault_physical(signal: Signal) -> float:
    return fault_raw(signal) * signal.scale + signal.offset


def in_range(signal: Signal, value: float) -> bool:
    """True when value lies within the DBC [min, max] (half an LSB of slack)."""
    slack = abs(signal.scale) / 2
    if signal.minimum is not None and value < signal.minimum - slack:
        return False
    if signal.maximum is not None and value > signal.maximum + slack:
        return False
    return True


def clamp(signal: Signal, value: float) -> float:
    if signal.minimum is not None and value < signal.minimum:
        return float(signal.minimum)
    if signal.maximum is not None and value > signal.maximum:
        return float(signal.maximum)
    return value


def encode_physical(message: Message, values: Mapping[str, float | None]) -> bytes:
    """Encode physical values; None encodes the fault pattern, others are clamped."""
    data: dict[str, float] = {}
    for sig in message.signals:
        try:
            value = values[sig.name]
        except KeyError:
            raise KeyError(f"no value for signal {sig.name} of {message.name}") from None
        data[sig.name] = fault_physical(sig) if value is None else clamp(sig, value)
    return bytes(message.encode(data, scaling=True, padding=False, strict=False))


def decode_physical(message: Message, data: bytes) -> dict[str, float | None]:
    """Decode a frame; signals outside their DBC range (faults) become None."""
    raw = message.decode(data, decode_choices=False, scaling=True)
    out: dict[str, float | None] = {}
    for sig in message.signals:
        value = float(raw[sig.name])
        out[sig.name] = value if in_range(sig, value) else None
    return out
