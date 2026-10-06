"""DBC decoding of received frames, with rolling-counter gap detection."""

from __future__ import annotations

from dataclasses import dataclass

import cantools
from cantools.database.can import Database, Message

from common.dbc import counter_signal, decode_physical


@dataclass
class MessageStats:
    frames: int = 0
    decode_errors: int = 0
    counter_gaps: int = 0  # times the rolling counter skipped
    missed_frames: int = 0  # estimated frames lost, from counter skips
    last_counter: int | None = None


class FrameDecoder:
    def __init__(self, db: Database, message_names: list[str] | tuple[str, ...]) -> None:
        self._by_id: dict[int, Message] = {}
        self._counter: dict[int, str | None] = {}
        self.stats: dict[str, MessageStats] = {}
        for name in message_names:
            message = db.get_message_by_name(name)
            self._by_id[message.frame_id] = message
            sig = counter_signal(message)
            self._counter[message.frame_id] = sig.name if sig else None
            self.stats[name] = MessageStats()
        self.unknown_frames = 0
        self.ignored_frames = 0  # error / remote / extended frames

    @property
    def can_ids(self) -> list[int]:
        return list(self._by_id)

    def decode(self, msg) -> tuple[Message, dict[str, float | None]] | None:
        """Decode a python-can Message. None if it is not a tracked data frame."""
        if msg.is_error_frame or msg.is_remote_frame or msg.is_extended_id:
            self.ignored_frames += 1
            return None
        message = self._by_id.get(msg.arbitration_id)
        if message is None:
            self.unknown_frames += 1
            return None
        stats = self.stats[message.name]
        try:
            if len(msg.data) != message.length:
                raise ValueError(f"DLC {len(msg.data)} != {message.length}")
            values = decode_physical(message, bytes(msg.data))
        except (cantools.database.DecodeError, ValueError, KeyError):
            stats.decode_errors += 1
            return None
        stats.frames += 1
        counter_name = self._counter[msg.arbitration_id]
        if counter_name is not None and values.get(counter_name) is not None:
            counter = int(values[counter_name])
            if stats.last_counter is not None:
                step = (counter - stats.last_counter) % 16
                if step != 1:
                    stats.counter_gaps += 1
                    stats.missed_frames += (step - 1) % 16
            stats.last_counter = counter
        return message, values

    # ---------------------------------------------------------------- totals
    @property
    def frames(self) -> int:
        return sum(s.frames for s in self.stats.values())

    @property
    def decode_errors(self) -> int:
        return sum(s.decode_errors for s in self.stats.values())

    @property
    def counter_gaps(self) -> int:
        return sum(s.counter_gaps for s in self.stats.values())
