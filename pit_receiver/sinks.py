"""Where pit receiver points go. Sinks must never crash the receiver."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from pit_receiver.points import Point


class Sink(ABC):
    name = "sink"

    @abstractmethod
    def write(self, points: list[Point]) -> None: ...

    def flush(self) -> None:  # noqa: B027 - optional override
        pass

    def close(self) -> None:
        self.flush()

    def health(self) -> dict[str, float | int | str]:
        return {}


class MemorySink(Sink):
    name = "memory"

    def __init__(self) -> None:
        self.points: list[Point] = []

    def write(self, points: list[Point]) -> None:
        self.points.extend(points)

    def by_measurement(self, measurement: str) -> list[Point]:
        return [p for p in self.points if p.measurement == measurement]


class JsonlSink(Sink):
    """Every point as one JSON object per line; handy for debugging and offline analysis."""

    name = "jsonl"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.errors = 0

    def write(self, points: list[Point]) -> None:
        try:
            for p in points:
                self._fh.write(json.dumps(p.as_dict()) + "\n")
        except OSError:
            self.errors += 1

    def flush(self) -> None:
        try:
            self._fh.flush()
        except OSError:
            self.errors += 1

    def close(self) -> None:
        self.flush()
        self._fh.close()
