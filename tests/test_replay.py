"""Recording and replay: log format, timing at every speed, loop, pause, safety."""

from __future__ import annotations

import subprocess
import sys
import time

import can
import pytest

from common.config import REPO_ROOT
from common.protocol import ChannelLayout, Codec, TelemetryPacket
from common.transport import UdpTransport
from replay.logs import read_log, summarize, write_log
from replay.player import Player, make_handler, retarget


class Clock:
    """Fake time: sleep() advances it."""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def frames(n=11, step=0.1, start=1_700_000_000.0):
    return [can.Message(timestamp=start + i * step, arbitration_id=0x100 + (i % 3), data=bytes([i] * 8),
                        is_extended_id=False, channel="can0") for i in range(n)]


def play(msgs, **kw):
    clock = Clock()
    sent: list[tuple[float, can.Message]] = []
    player = Player(lambda: iter(msgs), lambda m: sent.append((clock.t, m)), clock=clock, sleep=clock.sleep, **kw)
    return player, sent, clock


# ------------------------------------------------------------------ logs
def test_candump_log_roundtrip(tmp_path, make_sim):
    sim = make_sim(seed=3)
    msgs = [can.Message(timestamp=1.7e9 + i * 0.01, arbitration_id=f.arbitration_id, data=f.data, is_extended_id=False)
            for i, f in enumerate(sim.run_for(2.0))]
    path = tmp_path / "session.log"
    assert write_log(path, msgs) == len(msgs)
    first = path.read_text().splitlines()[0]
    assert first.startswith("(1700000000.000000) vcan0 ") and "#" in first  # candump -l format
    back = list(read_log(path))
    assert [(m.arbitration_id, bytes(m.data)) for m in back] == [(m.arbitration_id, bytes(m.data)) for m in msgs]
    assert back[-1].timestamp == pytest.approx(msgs[-1].timestamp, abs=1e-6)
    info = summarize(path)
    assert info["frames"] == len(msgs) and info["duration_s"] == pytest.approx(len(msgs) * 0.01 - 0.01, abs=0.001)


def test_retarget_drops_the_recorded_channel():
    msg = retarget(frames(1)[0])
    assert msg.channel is None and msg.arbitration_id == 0x100


# ---------------------------------------------------------------- timing
@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0, 5.0])
def test_playback_speed(speed):
    player, sent, clock = play(frames(), speed=speed)
    player.run()
    assert player.finished and player.frames_sent == 11
    offsets = [t - sent[0][0] for t, _ in sent]
    expected = [i * 0.1 / speed for i in range(11)]
    assert offsets == pytest.approx(expected, abs=0.051 / speed + 1e-6)


def test_pause_and_resume():
    player, sent, clock = play(frames())
    calls = {"n": 0}

    def on_idle():
        calls["n"] += 1
        if player.frames_sent == 5 and not player.paused and calls.get("paused_once") is None:
            player.pause()
            calls["paused_once"] = clock.t
        elif player.paused and clock.t - calls["paused_once"] >= 2.0:
            player.resume()

    player.run(on_idle=on_idle)
    gap = sent[5][0] - sent[4][0]
    assert gap == pytest.approx(2.1, abs=0.06)  # 2 s paused + the normal 0.1 s spacing
    assert sent[10][0] - sent[5][0] == pytest.approx(0.5, abs=0.06)


def test_speed_change_mid_playback_is_seamless():
    player, sent, clock = play(frames(21))

    def on_idle():
        if player.frames_sent == 10 and player.speed == 1.0:
            player.set_speed(5.0)

    player.run(on_idle=on_idle)
    assert sent[9][0] - sent[0][0] == pytest.approx(0.9, abs=0.06)
    assert sent[20][0] - sent[9][0] == pytest.approx(1.1 / 5, abs=0.06)


def test_loop():
    player, sent, clock = play(frames(5), loop=True)
    player.run(should_stop=lambda: player.frames_sent >= 12)
    assert player.loops_completed == 2
    assert [m.data[0] for _, m in sent[:12]] == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1]


def test_falling_far_behind_resyncs_instead_of_bursting():
    player, sent, clock = play(frames())

    def send_slowly(msg):
        sent.append((clock.t, msg))
        if len(sent) == 3:
            clock.t += 5.0  # the host stalled

    player._send = send_slowly
    player.run()
    assert player.resyncs == 1
    assert sent[-1][0] - sent[3][0] == pytest.approx(0.7, abs=0.06)  # resumed normal pacing


def test_control_handler():
    player, _, _ = play(frames())
    stopped = []
    handle = make_handler(player, lambda: stopped.append(True))
    assert handle({"cmd": "pause"})["state"] == "paused"
    assert handle({"cmd": "resume"})["state"] == "playing"
    assert handle({"cmd": "speed", "value": 5})["speed"] == 5
    assert handle({"cmd": "loop", "value": True})["loop"] is True
    handle({"cmd": "stop"})
    assert stopped
    with pytest.raises(ValueError):
        handle({"cmd": "speed", "value": 0})
    with pytest.raises(ValueError):
        handle({"cmd": "rewind"})


def test_faster_slower_steps():
    player, _, _ = play(frames())
    player.faster()
    assert player.speed == 2.0
    player.slower()
    player.slower()
    assert player.speed == 0.5


def test_refuses_to_replay_onto_a_real_bus(tmp_path):
    from replay.player import main

    path = tmp_path / "x.log"
    write_log(path, frames(3))
    with pytest.raises(SystemExit, match="refusing"):
        main([str(path), "--channel", "can0", "--no-control"])


# ------------------------------------------------------------------ vcan
@pytest.mark.vcan
def test_replay_feeds_the_car_node_like_the_fake_ecu(can_channel, tmp_path, make_sim, dbc):
    """Recorded traffic -> replay at 2x -> vcan -> car node -> telemetry. Same pipeline, no fake ECU."""
    sim = make_sim(seed=5)
    msgs = []
    for i in range(round(16 * sim.physics_hz)):  # 16 s of driving, generated faster than real time
        for f in sim.step():
            msgs.append(can.Message(timestamp=1.7e9 + i / sim.physics_hz, arbitration_id=f.arbitration_id,
                                    data=f.data, is_extended_id=False))
    log = tmp_path / "drive.log"
    write_log(log, msgs, channel="can0")  # recorded on a "real" interface name

    rx = UdpTransport(bind=("127.0.0.1", 0))
    port = rx.local_address[1]
    node = subprocess.Popen([sys.executable, "-m", "car_node", "--channel", can_channel, "--remote",
                             f"127.0.0.1:{port}", "--duration", "10", "--quiet"], cwd=REPO_ROOT)
    time.sleep(0.5)
    player = subprocess.Popen([sys.executable, "-m", "replay", str(log), "--channel", can_channel, "--speed", "2",
                               "--no-control", "--quiet"], cwd=REPO_ROOT, stdin=subprocess.DEVNULL)
    codec = Codec(ChannelLayout.load("config/channels.yaml", dbc))
    rpm = []
    try:
        start = time.monotonic()
        while time.monotonic() - start < 9.0:
            data = rx.recv(0.5)
            if data:
                p = codec.decode(data)
                if isinstance(p, TelemetryPacket) and isinstance(p.values.get("rpm"), float):
                    rpm.append(p.values["rpm"])
        assert player.wait(timeout=15) == 0
    finally:
        for p in (player, node):
            p.kill()
        rx.close()
    assert len(rpm) > 60
    assert max(rpm) > 6000  # the replayed car pulled away from the pits (8 s of log at 2x)
