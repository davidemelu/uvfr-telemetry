"""Transport abstraction: moves whole telemetry packets between car and pit.

The car node and pit receiver only ever see this interface, so swapping the
Phase 0 UDP link for a USB LoRa serial modem is a configuration change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass


@dataclass
class TransportStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    send_errors: int = 0
    packets_received: int = 0
    bytes_received: int = 0
    frame_errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class Transport(ABC):
    def __init__(self) -> None:
        self.stats = TransportStats()

    @abstractmethod
    def send(self, packet: bytes) -> bool:
        """Queue one packet for transmission. Never raises for link problems;
        returns False and counts a send error instead, because a dead link must
        not stop the car node."""

    @abstractmethod
    def recv(self, timeout: float | None = None) -> bytes | None:
        """Next whole packet, or None if none arrived within timeout seconds."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable endpoint description for logs."""

    def close(self) -> None:  # noqa: B027 - optional override
        pass

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
