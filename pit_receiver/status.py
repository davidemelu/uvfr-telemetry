"""Status vocabulary shared by link health, channel state and alerts.

The numeric code is what Grafana maps to colours and text, so it is stable.
Severity is a separate ordering used to pick the worst of several statuses.
"""

from __future__ import annotations

from enum import IntEnum


class Status(IntEnum):
    NORMAL = 0
    WARNING = 1
    CRITICAL = 2
    STALE = 3
    NO_DATA = 4

    @property
    def label(self) -> str:
        return "NO DATA" if self is Status.NO_DATA else self.name

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


# CRITICAL first: a known-bad engine matters more than an unknown one.
_SEVERITY = {
    Status.NORMAL: 0,
    Status.NO_DATA: 1,
    Status.STALE: 2,
    Status.WARNING: 3,
    Status.CRITICAL: 4,
}


def worst(statuses) -> Status:
    statuses = list(statuses)
    return max(statuses, key=lambda s: s.severity) if statuses else Status.NO_DATA
