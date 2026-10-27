"""The live level rows of the Devices submenu."""

from __future__ import annotations

import math
import socket
import time
from array import array

import pytest

from echolot.audio import bridge
from echolot.ui.meter import FLOOR_DB, Meter

from test_bridge import connect, wait_for  # noqa: E402


@pytest.fixture
def host_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bridge.configure("on", port)
    yield port
    bridge.configure("off", bridge.DEFAULT_PORT)


def tone_bytes(rate=48000, seconds=0.1, amplitude=8000) -> bytes:
    samples = array("h")
    for n in range(int(rate * seconds)):
        v = int(amplitude * math.sin(2 * math.pi * 440 * n / rate))
        samples.extend([v, v])
    return samples.tobytes()


def test_a_track_with_sound_shows_a_level_and_one_without_stays_at_the_floor(config, host_port):
    meter = Meter(bridge.DEVICE, "speaker", config)
    try:
        time.sleep(0.4)
        meter.poll()
        assert meter.silent
        sock = connect(host_port)
        assert wait_for(lambda: bridge.status() == "connected")
        end = time.monotonic() + 2
        while time.monotonic() < end and meter.silent:
            sock.sendall(tone_bytes())
            time.sleep(0.1)
            meter.poll()
        assert not meter.silent
        assert -30 < meter.level_db < 0
        sock.close()
    finally:
        meter.stop()


def test_the_level_falls_back_after_the_sound_stops(config, host_port):
    meter = Meter(bridge.DEVICE, "speaker", config)
    try:
        meter.level_db = -10.0
        levels = [meter.poll() for _ in range(20)]
        assert levels[-1] == FLOOR_DB
        assert levels == sorted(levels, reverse=True)
    finally:
        meter.stop()


def test_a_meter_that_cannot_open_says_so(config, monkeypatch):
    from echolot.audio import capture

    def broken(*_a, **_k):
        raise OSError("no parec")

    monkeypatch.setattr(capture.backend.current(), "open_reader", broken)
    meter = Meter("whatever", "mic", config)
    assert meter.failed
    meter.stop()
