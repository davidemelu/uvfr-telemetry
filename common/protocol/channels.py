"""Telemetry channel layout, shared by the car node (encoder) and pit (decoder).

Loaded from config/channels.yaml. A channel's index (its position in the
file) is its on-air identifier. The layout hash lets each end detect that the
other is using a different file.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from cantools.database.can import Database

from common.config import ConfigError, check_keys, load_yaml


class Missing(str, Enum):
    """A channel value that is not a number."""

    NO_DATA = "NO DATA"  # never received, or the sensor reports a fault
    STALE = "STALE"  # the car node has not seen a fresh CAN value in time


@dataclass(frozen=True)
class WireType:
    name: str
    fmt: str  # struct format character
    size: int
    signed: bool

    @property
    def raw_min(self) -> int:
        return -(1 << (8 * self.size - 1)) if self.signed else 0

    @property
    def raw_max(self) -> int:
        return (1 << (8 * self.size - 1)) - 1 if self.signed else (1 << (8 * self.size)) - 1

    @property
    def no_data_raw(self) -> int:
        return self.raw_min if self.signed else self.raw_max

    @property
    def stale_raw(self) -> int:
        return self.raw_min + 1 if self.signed else self.raw_max - 1

    @property
    def valid_min(self) -> int:
        return self.raw_min + 2 if self.signed else self.raw_min

    @property
    def valid_max(self) -> int:
        return self.raw_max if self.signed else self.raw_max - 2


WIRE_TYPES = {
    "u8": WireType("u8", "B", 1, False),
    "i8": WireType("i8", "b", 1, True),
    "u16": WireType("u16", "H", 2, False),
    "i16": WireType("i16", "h", 2, True),
}

MAX_CHANNELS = 32


@dataclass(frozen=True)
class ChannelSpec:
    index: int
    name: str
    message: str
    signal: str
    rate_hz: float
    wire: WireType
    scale: float
    offset: float
    stale_after_ms: float | None = None
    unit: str = ""

    @property
    def key(self) -> str:
        return f"{self.message}.{self.signal}"

    @property
    def physical_min(self) -> float:
        return self.wire.valid_min * self.scale + self.offset

    @property
    def physical_max(self) -> float:
        return self.wire.valid_max * self.scale + self.offset

    def to_raw(self, value: float | Missing) -> tuple[int, bool]:
        """(raw, saturated). Values outside the representable range saturate."""
        if value is Missing.NO_DATA:
            return self.wire.no_data_raw, False
        if value is Missing.STALE:
            return self.wire.stale_raw, False
        raw = round((value - self.offset) / self.scale)
        if raw < self.wire.valid_min:
            return self.wire.valid_min, True
        if raw > self.wire.valid_max:
            return self.wire.valid_max, True
        return raw, False

    def from_raw(self, raw: int) -> float | Missing:
        if raw == self.wire.no_data_raw:
            return Missing.NO_DATA
        if raw == self.wire.stale_raw:
            return Missing.STALE
        return raw * self.scale + self.offset


@dataclass(frozen=True)
class ChannelLayout:
    base_rate_hz: int
    status_rate_hz: float
    channels: tuple[ChannelSpec, ...]
    _by_name: dict[str, ChannelSpec] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        self._by_name.update({c.name: c for c in self.channels})

    @property
    def bitmap_bytes(self) -> int:
        return (len(self.channels) + 7) // 8

    @property
    def messages(self) -> tuple[str, ...]:
        """CAN messages the channels come from, in first-use order (STATUS age order)."""
        seen: list[str] = []
        for c in self.channels:
            if c.message not in seen:
                seen.append(c.message)
        return tuple(seen)

    def by_name(self, name: str) -> ChannelSpec:
        return self._by_name[name]

    @property
    def config_hash(self) -> int:
        """CRC-16 over everything the decoder depends on (not rates or units)."""
        parts = [f"v1|{len(self.channels)}"]
        parts += [f"{c.index},{c.name},{c.key},{c.wire.name},{c.scale!r},{c.offset!r}" for c in self.channels]
        return binascii.crc_hqx(";".join(parts).encode("utf-8"), 0xFFFF)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str = "config/channels.yaml", dbc: Database | None = None) -> ChannelLayout:
        return cls.from_config(load_yaml(path), dbc)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], dbc: Database | None = None) -> ChannelLayout:
        check_keys(cfg, {"protocol", "channels"}, "channels.yaml")
        proto = check_keys(cfg["protocol"], {"base_rate_hz", "status_rate_hz"}, "channels.protocol")
        base = proto["base_rate_hz"]
        if not isinstance(base, int) or not 1 <= base <= 100:
            raise ConfigError("channels.protocol.base_rate_hz must be an integer between 1 and 100")
        status_rate = float(proto["status_rate_hz"])
        _check_rate(status_rate, base, "channels.protocol.status_rate_hz")

        raw_channels = cfg["channels"]
        if not isinstance(raw_channels, list) or not raw_channels:
            raise ConfigError("channels.channels must be a non-empty list")
        if len(raw_channels) > MAX_CHANNELS:
            raise ConfigError(f"at most {MAX_CHANNELS} channels are supported, got {len(raw_channels)}")

        specs: list[ChannelSpec] = []
        names: set[str] = set()
        for i, raw in enumerate(raw_channels):
            where = f"channels[{i}]"
            check_keys(raw, {"name", "signal", "rate_hz", "wire", "stale_after_ms"}, where,
                       required={"name", "signal", "rate_hz", "wire"})
            name = raw["name"]
            if not isinstance(name, str) or not name.replace("_", "").isalnum() or not name.islower():
                raise ConfigError(f"{where}: name {name!r} must be lower_snake_case")
            if name in names:
                raise ConfigError(f"{where}: duplicate channel name {name!r}")
            names.add(name)
            signal = raw["signal"]
            if not isinstance(signal, str) or signal.count(".") != 1:
                raise ConfigError(f"{where} ({name}): signal must be MESSAGE.Signal, got {signal!r}")
            message, sig = signal.split(".")
            rate = float(raw["rate_hz"])
            _check_rate(rate, base, f"{where} ({name}).rate_hz")
            wire = check_keys(raw["wire"], {"type", "scale", "offset"}, f"{where} ({name}).wire",
                              required={"type", "scale"})
            if wire["type"] not in WIRE_TYPES:
                raise ConfigError(f"{where} ({name}): wire type must be one of {sorted(WIRE_TYPES)}")
            scale = float(wire["scale"])
            if scale <= 0:
                raise ConfigError(f"{where} ({name}): wire scale must be positive")
            stale = raw.get("stale_after_ms")
            if stale is not None and float(stale) <= 0:
                raise ConfigError(f"{where} ({name}): stale_after_ms must be positive")
            specs.append(
                ChannelSpec(
                    index=i,
                    name=name,
                    message=message,
                    signal=sig,
                    rate_hz=rate,
                    wire=WIRE_TYPES[wire["type"]],
                    scale=scale,
                    offset=float(wire.get("offset", 0.0)),
                    stale_after_ms=float(stale) if stale is not None else None,
                )
            )
        layout = cls(base_rate_hz=base, status_rate_hz=status_rate, channels=tuple(specs))
        return layout.resolve(dbc) if dbc is not None else layout

    def resolve(self, dbc: Database) -> ChannelLayout:
        """Check channels against the DBC; fill in units and default staleness."""
        resolved = []
        for c in self.channels:
            try:
                message = dbc.get_message_by_name(c.message)
            except KeyError:
                raise ConfigError(f"channel {c.name}: DBC has no message {c.message!r}") from None
            try:
                sig = message.get_signal_by_name(c.signal)
            except KeyError:
                raise ConfigError(f"channel {c.name}: {c.message} has no signal {c.signal!r}") from None
            if sig.minimum is not None and sig.minimum < c.physical_min - c.scale / 2:
                raise ConfigError(
                    f"channel {c.name}: DBC minimum {sig.minimum} is below what {c.wire.name} with scale "
                    f"{c.scale} and offset {c.offset} can carry ({c.physical_min:g})"
                )
            if sig.maximum is not None and sig.maximum > c.physical_max + c.scale / 2:
                raise ConfigError(
                    f"channel {c.name}: DBC maximum {sig.maximum} is above what {c.wire.name} with scale "
                    f"{c.scale} and offset {c.offset} can carry ({c.physical_max:g})"
                )
            stale = c.stale_after_ms
            if stale is None:
                stale = max(250.0, 5.0 * (message.cycle_time or 100))
            resolved.append(
                ChannelSpec(**{**c.__dict__, "stale_after_ms": stale, "unit": sig.unit or ""})
            )
        return ChannelLayout(self.base_rate_hz, self.status_rate_hz, tuple(resolved))


def _check_rate(rate: float, base: int, where: str) -> None:
    if rate <= 0 or rate > base:
        raise ConfigError(f"{where}: {rate:g} Hz must be between 0 and base_rate_hz ({base})")
    ticks = base / rate
    if abs(ticks - round(ticks)) > 1e-9:
        raise ConfigError(f"{where}: {rate:g} Hz must divide base_rate_hz ({base} Hz) evenly")
