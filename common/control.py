"""Tiny JSON-over-UDP control channel for the lab processes.

Used to switch fake ECU scenarios, change link impairment and pause replay
while they are running. It binds to localhost by default. It is a lab
convenience and is not part of the telemetry path.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ControlError(RuntimeError):
    """The control request could not be delivered or answered."""


class ControlServer:
    def __init__(self, host: str, port: int, handler: Handler) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.bind((host, port))
        except OSError as exc:
            self._sock.close()
            raise ControlError(f"cannot bind control port {host}:{port}: {exc}") from None
        self._sock.setblocking(False)
        self._handler = handler
        self.address: tuple[str, int] = self._sock.getsockname()

    def poll(self) -> int:
        """Answer every pending request without blocking. Returns how many."""
        handled = 0
        while True:
            try:
                data, addr = self._sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return handled
            try:
                request = json.loads(data.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                response = self._handler(request)
            except Exception as exc:  # report to the caller and keep serving
                response = {"ok": False, "error": str(exc)}
            try:
                self._sock.sendto(json.dumps(response).encode("utf-8"), addr)
            except OSError:
                pass
            handled += 1

    def close(self) -> None:
        self._sock.close()


def request(host: str, port: int, payload: dict[str, Any], timeout: float = 1.0) -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(json.dumps(payload).encode("utf-8"), (host, port))
            data, _ = sock.recvfrom(65535)
        except (TimeoutError, socket.timeout):
            raise ControlError(f"no reply from {host}:{port}; is the process running?") from None
        except OSError as exc:
            raise ControlError(f"control request to {host}:{port} failed: {exc}") from None
    reply = json.loads(data.decode("utf-8"))
    if not isinstance(reply, dict):
        raise ControlError("malformed reply")
    return reply
