"""Time-series points produced by the pit receiver, independent of the database.

to_line() renders InfluxDB line protocol:
    measurement,tag=value field=1.5,other=2i,text="x" 1700000000000000000
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

FieldValue = float | int | bool | str


def _escape_key(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _escape_measurement(text: str) -> str:
    return text.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _field_value(value: FieldValue) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    raise TypeError(f"unsupported field type {type(value).__name__}")


@dataclass(frozen=True)
class Point:
    measurement: str
    fields: dict[str, FieldValue]
    tags: dict[str, str] = field(default_factory=dict)
    time_ns: int | None = None

    def to_line(self) -> str | None:
        """Line protocol, or None if no field can be written."""
        rendered = []
        for key in sorted(self.fields):
            value = _field_value(self.fields[key])
            if value is not None:
                rendered.append(f"{_escape_key(key)}={value}")
        if not rendered:
            return None
        head = _escape_measurement(self.measurement)
        for key in sorted(self.tags):
            if self.tags[key] != "":
                head += f",{_escape_key(key)}={_escape_key(str(self.tags[key]))}"
        line = f"{head} {','.join(rendered)}"
        if self.time_ns is not None:
            line += f" {self.time_ns}"
        return line

    def as_dict(self) -> dict[str, Any]:
        return {"measurement": self.measurement, "tags": self.tags, "fields": self.fields, "time_ns": self.time_ns}
