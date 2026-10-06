"""Current vehicle state: the latest value and receive time of every CAN signal.

The car node transmits from this state on its own schedule, never frame by
frame, so the radio load does not depend on how busy the CAN bus is.
"""

from __future__ import annotations

from common.protocol.channels import ChannelSpec, Missing


class VehicleState:
    def __init__(self) -> None:
        self._signals: dict[str, tuple[float | None, float]] = {}
        self._messages: dict[str, float] = {}

    def update(self, message_name: str, values: dict[str, float | None], t: float) -> None:
        self._messages[message_name] = t
        for signal, value in values.items():
            self._signals[f"{message_name}.{signal}"] = (value, t)

    def message_age_s(self, message_name: str, now: float) -> float | None:
        t = self._messages.get(message_name)
        return None if t is None else max(0.0, now - t)

    def channel_value(self, spec: ChannelSpec, now: float) -> float | Missing:
        """Value to transmit: the number, STALE (CAN timeout) or NO DATA."""
        entry = self._signals.get(spec.key)
        if entry is None:
            return Missing.NO_DATA
        value, t = entry
        if spec.stale_after_ms is not None and (now - t) * 1000.0 > spec.stale_after_ms:
            return Missing.STALE
        if value is None:  # sensor reported a fault
            return Missing.NO_DATA
        return value
