"""JSON-over-UDP control channel."""

from __future__ import annotations

import threading

import pytest

from common.control import ControlError, ControlServer, request


@pytest.fixture
def server():
    def handler(req):
        if req.get("cmd") == "boom":
            raise ValueError("handler failed")
        return {"ok": True, "echo": req}

    srv = ControlServer("127.0.0.1", 0, handler)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            srv.poll()
            stop.wait(0.005)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield srv
    stop.set()
    thread.join()
    srv.close()


def test_roundtrip(server):
    host, port = server.address
    assert request(host, port, {"cmd": "status", "x": 1}) == {"ok": True, "echo": {"cmd": "status", "x": 1}}


def test_handler_errors_are_reported_not_fatal(server):
    host, port = server.address
    assert request(host, port, {"cmd": "boom"}) == {"ok": False, "error": "handler failed"}
    assert request(host, port, {"cmd": "again"})["ok"] is True


def test_no_server_raises_control_error():
    srv = ControlServer("127.0.0.1", 0, lambda r: r)
    host, port = srv.address
    srv.close()
    with pytest.raises(ControlError):
        request(host, port, {"cmd": "status"}, timeout=0.3)


def test_port_in_use_is_reported():
    srv = ControlServer("127.0.0.1", 0, lambda r: r)
    try:
        with pytest.raises(ControlError, match="cannot bind"):
            ControlServer("127.0.0.1", srv.address[1], lambda r: r)
    finally:
        srv.close()
