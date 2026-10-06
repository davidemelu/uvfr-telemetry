"""Reading and writing CAN logs.

The default format is the SocketCAN candump log (`candump -l`), one frame per
line, readable by can-utils (canplayer, log2asc) and by python-can:

    (1791311234.567890) vcan0 100#28237302B718101B

python-can's LogReader also reads Vector .asc / .blf, PCAN .trc and CSV, so
recordings from other tools can be replayed too.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

import can


def read_log(path: str | Path) -> Iterator[can.Message]:
    """Data frames from a log, in file order (error and remote frames skipped)."""
    for msg in can.LogReader(str(path)):
        if msg.is_error_frame or msg.is_remote_frame:
            continue
        yield msg


def format_line(msg: can.Message, channel: str) -> str:
    """One candump -l line: (timestamp) channel ID#DATA"""
    ident = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
    return f"({msg.timestamp:.6f}) {channel} {ident}#{bytes(msg.data).hex().upper()}\n"


def write_log(path: str | Path, messages: Iterable[can.Message], channel: str = "vcan0") -> int:
    """Write messages as a candump log. Returns the number of frames written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="ascii") as fh:
        for msg in messages:
            fh.write(format_line(msg, msg.channel or channel))
            count += 1
    return count


def summarize(path: str | Path) -> dict[str, object]:
    first = last = None
    count = 0
    ids: dict[int, int] = {}
    for msg in read_log(path):
        first = msg.timestamp if first is None else first
        last = msg.timestamp
        count += 1
        ids[msg.arbitration_id] = ids.get(msg.arbitration_id, 0) + 1
    duration = (last - first) if count > 1 else 0.0
    return {"frames": count, "duration_s": round(duration, 3),
            "frames_per_s": round(count / duration, 1) if duration else 0.0,
            "ids": {f"0x{k:X}": v for k, v in sorted(ids.items())}}
