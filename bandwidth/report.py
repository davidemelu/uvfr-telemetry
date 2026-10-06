"""Telemetry bandwidth report: what the configured stream needs, and whether LoRa can carry it.

    python -m bandwidth                    # predicted from config/channels.yaml
    python -m bandwidth --measure 20       # also average the running car node for 20 s
    python -m bandwidth --markdown         # tables as Markdown (docs/bandwidth.md)

Predictions come from the same scheduler and codec the car node uses, so they
are exact for the configured layout. --measure reads the car node's own
per-second statistics (run/car_node_stats.json) for the real transmitted rate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

from bandwidth.lora import DWELL_LIMIT_S, STANDARD_CONFIGS, LoraConfig, verdict
from car_node.scheduler import ChannelScheduler
from common.config import load_yaml, resolve_path
from common.dbc import load_dbc
from common.protocol import OVERHEAD_BYTES, ChannelLayout, Codec

SERIAL_FRAMING = 2  # COBS byte + delimiter on serial radio links


@dataclass(frozen=True)
class StreamModel:
    """One second of the configured stream (the schedule repeats every base_rate ticks)."""

    layout: ChannelLayout
    telemetry_sizes: tuple[int, ...]  # bytes of each telemetry packet in one second
    status_size: int
    status_per_s: float

    @classmethod
    def from_layout(cls, layout: ChannelLayout) -> StreamModel:
        codec, sched = Codec(layout), ChannelScheduler(layout)
        sizes = tuple(codec.telemetry_size(sched.due(t)) for t in range(layout.base_rate_hz) if sched.due(t))
        return cls(layout, sizes, codec.status_size, layout.status_rate_hz)

    @property
    def packets_per_s(self) -> float:
        return len(self.telemetry_sizes) + self.status_per_s

    @property
    def payload_bytes_per_s(self) -> float:
        return sum(c.wire.size * c.rate_hz for c in self.layout.channels)

    @property
    def encoded_bytes_per_s(self) -> float:
        return sum(self.telemetry_sizes) + self.status_size * self.status_per_s

    @property
    def framed_bytes_per_s(self) -> float:
        return self.encoded_bytes_per_s + SERIAL_FRAMING * self.packets_per_s

    def lora_airtime_per_s(self, cfg: LoraConfig, modem_overhead: int = 0) -> float:
        extra = SERIAL_FRAMING + modem_overhead
        t = sum(cfg.airtime_s(s + extra) for s in self.telemetry_sizes)
        return t + self.status_per_s * cfg.airtime_s(self.status_size + extra)

    @property
    def largest_packet(self) -> int:
        return max(max(self.telemetry_sizes), self.status_size)


def uniform_stream_airtime(cfg: LoraConfig, channels: int, rate_hz: int, value_bytes: int = 2,
                           status_size: int = 34, modem_overhead: int = 0) -> tuple[int, float]:
    """(packet bytes, airtime per second) for N channels all at rate_hz in one packet per tick."""
    packet = OVERHEAD_BYTES + (channels + 7) // 8 + channels * value_bytes
    extra = SERIAL_FRAMING + modem_overhead
    per_s = rate_hz * cfg.airtime_s(packet + extra) + cfg.airtime_s(status_size + extra)
    return packet, per_s


def max_channels_at(cfg: LoraConfig, rate_hz: int, budget: float = 0.5, modem_overhead: int = 0) -> int:
    best = 0
    for n in range(1, 65):
        _, per_s = uniform_stream_airtime(cfg, n, rate_hz, modem_overhead=modem_overhead)
        if per_s <= budget:
            best = n
    return best


def measure(stats_path: str, seconds: float) -> dict[str, Any] | None:
    """Average the car node's per-second transmit statistics for `seconds`."""
    path = resolve_path(stats_path)
    samples, seen = [], None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data and data.get("rates") and data.get("uptime_s") != seen:
            seen = data["uptime_s"]
            samples.append(data["rates"])
        time.sleep(0.25)
    if not samples:
        return None

    def avg(get) -> float:
        return sum(get(s) for s in samples) / len(samples)

    return {
        "samples": len(samples),
        "packets_per_s": avg(lambda s: s["packets_per_s"]["total"]),
        "encoded_bytes_per_s": avg(lambda s: s["bytes_per_s"]["total"]),
        "payload_bytes_per_s": avg(lambda s: s["payload_bytes_per_s"]),
        "channel_bytes_per_s": {k: avg(lambda s, k=k: s["channel_value_bytes_per_s"][k])
                                for k in samples[0]["channel_value_bytes_per_s"]},
    }


# ------------------------------------------------------------------ output
class Table:
    def __init__(self, markdown: bool) -> None:
        self.markdown = markdown

    def __call__(self, headers: list[str], rows: list[list[Any]]) -> str:
        cells = [[str(c) for c in r] for r in rows]
        if self.markdown:
            out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
            out += ["| " + " | ".join(r) + " |" for r in cells]
            return "\n".join(out)
        widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
        line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
        out = [line, "  ".join("-" * w for w in widths)]
        out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in cells]
        return "\n".join(out)


def report(layout: ChannelLayout, measured: dict[str, Any] | None, markdown: bool, modem_overhead: int) -> str:
    m = StreamModel.from_layout(layout)
    table = Table(markdown)
    h = (lambda s: f"## {s}") if markdown else (lambda s: f"\n=== {s} ===")
    out: list[str] = []

    out.append(h("Channels (raw payload)"))
    rows = []
    for c in layout.channels:
        bps = c.wire.size * c.rate_hz
        meas = ""
        if measured:
            meas = f"{measured['channel_bytes_per_s'].get(c.name, 0) * 8:.0f}"
        rows.append([c.name, f"{c.rate_hz:g}", c.wire.name, c.wire.size, f"{bps:g}", f"{bps * 8:g}"] + ([meas] if measured else []))
    rows.append(["**total**" if markdown else "TOTAL", "", "", "", f"{m.payload_bytes_per_s:g}", f"{m.payload_bytes_per_s * 8:g}"]
                + ([f"{measured['payload_bytes_per_s'] * 8:.0f}"] if measured else []))
    out.append(table(["channel", "rate Hz", "wire", "bytes", "payload B/s", "payload bit/s"]
                     + (["measured bit/s"] if measured else []), rows))

    out.append(h("Encoded protocol stream"))
    tel = m.telemetry_sizes
    rows = [
        ["telemetry packets/s", f"{len(tel)}"],
        ["status packets/s", f"{m.status_per_s:g}"],
        ["telemetry packet size (bytes)", f"{min(tel)} to {max(tel)} (mean {sum(tel) / len(tel):.1f})"],
        ["status packet size (bytes)", f"{m.status_size}"],
        ["fixed overhead per packet (bytes)", f"{OVERHEAD_BYTES} header+CRC + {layout.bitmap_bytes} bitmap"],
        ["raw payload", f"{m.payload_bytes_per_s:g} B/s = {m.payload_bytes_per_s * 8:g} bit/s"],
        ["encoded (UDP)", f"{m.encoded_bytes_per_s:g} B/s = {m.encoded_bytes_per_s * 8:g} bit/s"],
        ["encoded + COBS serial framing", f"{m.framed_bytes_per_s:g} B/s = {m.framed_bytes_per_s * 8:g} bit/s"],
        ["protocol overhead", f"{100 * (1 - m.payload_bytes_per_s / m.encoded_bytes_per_s):.0f}% of encoded bytes"],
    ]
    if measured:
        rows += [
            ["measured packets/s", f"{measured['packets_per_s']:.2f} ({measured['samples']} samples)"],
            ["measured encoded", f"{measured['encoded_bytes_per_s']:.1f} B/s = {measured['encoded_bytes_per_s'] * 8:.0f} bit/s"],
            ["measured payload", f"{measured['payload_bytes_per_s']:.1f} B/s = {measured['payload_bytes_per_s'] * 8:.0f} bit/s"],
        ]
    out.append(table(["quantity", "value"], rows))

    out.append(h("LoRa time on air for this stream"))
    rows = []
    for cfg in STANDARD_CONFIGS:
        util = m.lora_airtime_per_s(cfg, modem_overhead)
        biggest = cfg.airtime_s(m.largest_packet + SERIAL_FRAMING + modem_overhead)
        flag = " (over 400 ms dwell)" if biggest > DWELL_LIMIT_S else ""
        rows.append([cfg.name, f"{cfg.raw_bitrate / 1000:.2f}", f"{1000 * biggest:.1f}{flag}",
                     f"{100 * util:.0f}%", verdict(util), max_channels_at(cfg, 10, 0.5, modem_overhead)])
    out.append(table(["LoRa setting", "headline kbit/s", "largest packet ms", "airtime used", "verdict",
                      "max u16 channels at 10 Hz (50% budget)"], rows))

    out.append(h("Uniform streams: N channels, all 10 Hz, 2 bytes each"))
    pick = [LoraConfig(7, 500_000), LoraConfig(7, 250_000), LoraConfig(7, 125_000), LoraConfig(8, 125_000), LoraConfig(9, 125_000)]
    rows = []
    for n in (10, 12, 15):
        size, _ = uniform_stream_airtime(pick[0], n, 10, modem_overhead=modem_overhead)
        cells = [n, size, f"{(10 * size + 34) * 8:g}"]
        for cfg in pick:
            _, per_s = uniform_stream_airtime(cfg, n, 10, modem_overhead=modem_overhead)
            cells.append(f"{100 * per_s:.0f}%")
        rows.append(cells)
    out.append(table(["channels", "packet bytes", "encoded bit/s"] + [c.name.split(" / CR")[0] for c in pick], rows))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bandwidth", description="Telemetry bandwidth and LoRa airtime report.")
    parser.add_argument("--channels", default="config/channels.yaml")
    parser.add_argument("--dbc", default="dbc/simulated_uvfr.dbc")
    parser.add_argument("--measure", type=float, metavar="SECONDS", help="also average the running car node")
    parser.add_argument("--stats-file", default=None, help="car node stats file (default from telemetry.yaml)")
    parser.add_argument("--modem-overhead", type=int, default=0, help="extra bytes the radio modem adds per packet")
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args(argv)

    layout = ChannelLayout.load(args.channels, load_dbc(args.dbc))
    measured = None
    if args.measure:
        stats = args.stats_file or load_yaml("config/telemetry.yaml")["car_node"]["stats_file"]
        print(f"measuring the running car node for {args.measure:g} s ...", file=sys.stderr)
        measured = measure(stats, args.measure)
        if measured is None:
            print(f"no car node statistics found in {stats}; is the car node running?", file=sys.stderr)
    print(report(layout, measured, args.markdown, args.modem_overhead), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
