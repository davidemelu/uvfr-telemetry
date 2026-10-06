"""UDP transport: the Phase 0 stand-in for the radio link (one packet per datagram)."""

from __future__ import annotations

import select
import socket

from common.transport.base import Transport

MAX_DATAGRAM = 2048


def parse_hostport(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {value!r}")
    return host, int(port)


class UdpTransport(Transport):
    def __init__(self, remote: tuple[str, int] | None = None, bind: tuple[str, int] | None = None) -> None:
        super().__init__()
        self.remote = remote
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if bind is not None:
            try:
                self._sock.bind(bind)
            except OSError:
                self._sock.close()
                raise
        self._sock.setblocking(False)

    @property
    def local_address(self) -> tuple[str, int]:
        return self._sock.getsockname()

    def describe(self) -> str:
        parts = []
        if self.remote:
            parts.append(f"to udp://{self.remote[0]}:{self.remote[1]}")
        try:
            host, port = self.local_address
            if port:
                parts.append(f"on udp://{host}:{port}")
        except OSError:
            pass
        return "udp " + " ".join(parts)

    def send(self, packet: bytes) -> bool:
        if self.remote is None:
            raise RuntimeError("UdpTransport has no remote address to send to")
        try:
            self._sock.sendto(packet, self.remote)
        except OSError:
            self.stats.send_errors += 1
            return False
        self.stats.packets_sent += 1
        self.stats.bytes_sent += len(packet)
        return True

    def recv(self, timeout: float | None = None) -> bytes | None:
        ready, _, _ = select.select([self._sock], [], [], timeout)
        if not ready:
            return None
        try:
            data, _ = self._sock.recvfrom(MAX_DATAGRAM)
        except (BlockingIOError, ConnectionRefusedError):
            return None
        self.stats.packets_received += 1
        self.stats.bytes_received += len(data)
        return data

    def close(self) -> None:
        self._sock.close()
