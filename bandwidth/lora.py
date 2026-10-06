"""LoRa time-on-air, from Semtech's formula (SX1276/77/78/79 datasheet, section 4.1.1.7).

The headline LoRa "data rate" ignores the preamble and header that every
packet pays, so small, frequent packets get far less than the headline rate.
Time on air is what limits a telemetry stream:

    T_sym      = 2^SF / BW
    T_preamble = (n_preamble + 4.25) * T_sym
    n_payload  = 8 + max(ceil((8*PL - 4*SF + 28 + 16*CRC - 20*IH) / (4*(SF - 2*DE))) * (CR + 4), 0)
    T_packet   = T_preamble + n_payload * T_sym

PL payload bytes, CRC 1 = payload CRC on, IH 1 = implicit header,
DE 1 = low data rate optimisation (required when T_sym > 16 ms),
CR 1..4 for coding rate 4/5 .. 4/8.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LoraConfig:
    sf: int
    bw_hz: int
    cr: int = 1  # 4/5
    preamble: int = 8
    explicit_header: bool = True
    crc: bool = True

    def __post_init__(self) -> None:
        if not 6 <= self.sf <= 12:
            raise ValueError("spreading factor must be 6..12")
        if not 1 <= self.cr <= 4:
            raise ValueError("coding rate index must be 1..4 (4/5..4/8)")

    @property
    def name(self) -> str:
        return f"SF{self.sf} / {self.bw_hz // 1000} kHz / CR 4/{self.cr + 4}"

    @property
    def symbol_s(self) -> float:
        return (2**self.sf) / self.bw_hz

    @property
    def low_data_rate_opt(self) -> bool:
        return self.symbol_s > 0.016

    @property
    def raw_bitrate(self) -> float:
        """The usual headline figure: SF * BW / 2^SF * 4 / (4 + CR)."""
        return self.sf * self.bw_hz / (2**self.sf) * 4 / (4 + self.cr)

    def airtime_s(self, payload_bytes: int) -> float:
        de = 1 if self.low_data_rate_opt else 0
        ih = 0 if self.explicit_header else 1
        num = 8 * payload_bytes - 4 * self.sf + 28 + 16 * (1 if self.crc else 0) - 20 * ih
        n_payload = 8 + max(math.ceil(num / (4 * (self.sf - 2 * de))) * (self.cr + 4), 0)
        return (self.preamble + 4.25) * self.symbol_s + n_payload * self.symbol_s


STANDARD_CONFIGS = [
    LoraConfig(7, 500_000),
    LoraConfig(8, 500_000),
    LoraConfig(7, 250_000),
    LoraConfig(8, 250_000),
    LoraConfig(9, 250_000),
    LoraConfig(7, 125_000),
    LoraConfig(8, 125_000),
    LoraConfig(9, 125_000),
    LoraConfig(10, 125_000),
    LoraConfig(11, 125_000),
    LoraConfig(12, 125_000),
]

DWELL_LIMIT_S = 0.4  # common 915 MHz per-transmission dwell limit at narrow bandwidths; check your modem's certification


def verdict(utilisation: float) -> str:
    if utilisation <= 0.5:
        return "fits with margin"
    if utilisation <= 0.8:
        return "tight"
    if utilisation <= 1.0:
        return "saturated"
    return "does not fit"
