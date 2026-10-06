"""Framing for byte-stream links (USB serial LoRa modems, RFD900x).

UDP keeps packet boundaries; a serial port does not. On serial links each
packet is COBS-encoded (Consistent Overhead Byte Stuffing, which removes every
0x00 byte) and followed by a single 0x00 delimiter. Overhead is 2 bytes for
packets under 254 bytes. After line noise or a lost byte, the receiver
resynchronises at the next 0x00; the CRC rejects the damaged packet.
"""

from __future__ import annotations

DELIMITER = b"\x00"


def cobs_encode(data: bytes) -> bytes:
    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError("zero byte inside a COBS frame")
        start, end = i + 1, i + code
        if end > n:
            raise ValueError("truncated COBS block")
        block = data[start:end]
        if 0 in block:
            raise ValueError("zero byte inside a COBS frame")
        out += block
        i = end
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)


def frame(packet: bytes) -> bytes:
    return cobs_encode(packet) + DELIMITER


class FrameReader:
    """Accumulates serial bytes and yields complete, COBS-decoded packets."""

    def __init__(self, max_frame: int = 1024) -> None:
        self._buf = bytearray()
        self._discarding = False
        self.max_frame = max_frame
        self.frame_errors = 0
        self.overflows = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        packets: list[bytes] = []
        for byte in chunk:
            if byte == 0:
                if self._buf and not self._discarding:
                    try:
                        packets.append(cobs_decode(bytes(self._buf)))
                    except ValueError:
                        self.frame_errors += 1
                self._buf.clear()
                self._discarding = False
            elif not self._discarding:
                self._buf.append(byte)
                if len(self._buf) > self.max_frame:
                    # No delimiter for too long: drop everything up to the next
                    # 0x00 so the junk is not glued onto the next good frame.
                    self.overflows += 1
                    self._buf.clear()
                    self._discarding = True
        return packets
