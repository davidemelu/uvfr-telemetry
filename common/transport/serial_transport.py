"""Serial transport for USB radio modems (915 MHz LoRa now, RFD900x later).

Packets are COBS-framed with a 0x00 delimiter (common/protocol/framing.py) so
the receiver can find packet boundaries in a byte stream and resynchronise
after corruption.

PHASE 0 STATUS: implemented and tested against pyserial's loop:// loopback
only. It has NOT been run against real radio hardware yet; see
docs/hardware-transition.md for the bring-up checklist.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from common.protocol.framing import FrameReader, frame
from common.transport.base import Transport


class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = 57600, serial_obj: Any = None) -> None:
        super().__init__()
        if serial_obj is None:
            try:
                import serial  # pyserial
            except ImportError as exc:  # pragma: no cover - environment specific
                raise RuntimeError("SerialTransport needs pyserial: pip install pyserial") from exc
            serial_obj = serial.serial_for_url(port, baudrate=baudrate, timeout=0, write_timeout=0.5)
        self._ser = serial_obj
        self.port = port
        self.baudrate = baudrate
        self._reader = FrameReader()
        self._pending: deque[bytes] = deque()

    def describe(self) -> str:
        return f"serial {self.port} @ {self.baudrate} baud (COBS framed)"

    def send(self, packet: bytes) -> bool:
        data = frame(packet)
        try:
            self._ser.write(data)
        except Exception:  # serial.SerialException / timeouts: count, never crash the node
            self.stats.send_errors += 1
            return False
        self.stats.packets_sent += 1
        self.stats.bytes_sent += len(data)
        return True

    def recv(self, timeout: float | None = None) -> bytes | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._pending:
            waiting = self._ser.in_waiting
            chunk = self._ser.read(waiting or 1)
            if chunk:
                self.stats.bytes_received += len(chunk)
                self._pending.extend(self._reader.feed(chunk))
                self.stats.frame_errors = self._reader.frame_errors + self._reader.overflows
                continue
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.002)
        self.stats.packets_received += 1
        return self._pending.popleft()

    def close(self) -> None:
        self._ser.close()
