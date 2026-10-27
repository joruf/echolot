"""The host bridge: the other side's audio, streamed in over TCP.

Everything here talks to a real listener on a real (loopback) socket with the
same wire format the Windows sender uses, so a change to either end shows up as
a failing test instead of a recording without the other person.
"""

from __future__ import annotations

import json
import math
import socket
import struct
import time
from array import array

import pytest

from echolot.audio import bridge
from echolot.audio.capture import CaptureProcess
from echolot.audio.group import SourceGroup
from echolot.session import Recorder


@pytest.fixture(autouse=True)
def bridge_on():
    """On, on a free port, and off again afterwards."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    bridge.configure("on", free)
    yield free
    bridge.configure("off", bridge.DEFAULT_PORT)


def tone(rate: int, seconds: float, channels: int = 2, amplitude: int = 8000) -> bytes:
    samples = array("h")
    for n in range(int(rate * seconds)):
        value = int(amplitude * math.sin(2 * math.pi * 440 * n / rate))
        samples.extend([value] * channels)
    return samples.tobytes()


def connect(port: int, rate: int = 48000, channels: int = 2) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    header = {"rate": rate, "channels": channels, "format": "s16le", "device": "fake",
              "host_time": time.time()}
    sock.sendall((json.dumps(header) + "\n").encode())
    return sock


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def peak(raw: bytes) -> int:
    samples = array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    return max((abs(v) for v in samples), default=0)


def read_seconds(reader, seconds: float, rate: int = 48000) -> bytes:
    out = b""
    while len(out) < int(seconds * rate) * 2:
        out += reader.read(1920)
    return out


# -- settings ---------------------------------------------------------------


def test_off_by_default_and_on_when_asked():
    bridge.configure("off")
    assert not bridge.enabled()
    bridge.configure("on")
    assert bridge.enabled()


def test_auto_follows_the_hypervisor(monkeypatch):
    bridge.configure("auto")
    monkeypatch.setattr(bridge.virt, "hypervisor", lambda: "vmware")
    assert bridge.enabled()
    monkeypatch.setattr(bridge.virt, "hypervisor", lambda: None)
    assert not bridge.enabled()


def test_the_pseudo_device_is_offered_as_an_output_only_when_enabled():
    from echolot.audio import pulse

    assert bridge.DEVICE in [d.name for d in pulse.list_sources() if d.is_monitor]
    bridge.configure("off")
    assert bridge.DEVICE not in [d.name for d in pulse.list_sources()]


def test_config_validates_the_bridge_section(config):
    config.set("bridge.mode", True)
    config.set("bridge.port", "99999999")
    config.validate()
    assert config.get("bridge.mode") == "on"
    assert config.get("bridge.port") == 65535
    config.set("bridge.mode", "nonsense")
    config.validate()
    assert config.get("bridge.mode") == "off"


# -- conversion -------------------------------------------------------------


def test_downmix_averages_channels():
    assert list(bridge.downmix(array("h", [100, 300, -10, 10]), 2)) == [200, 0]
    assert list(bridge.downmix(array("h", [3, 6, 9, 0, 0, 0]), 3)) == [6, 0]


@pytest.mark.parametrize("source,target", [(44100, 48000), (48000, 44100), (48000, 16000), (48000, 48000)])
def test_resampling_keeps_the_duration_and_the_level(source, target):
    resampler = bridge.Resampler(source, target)
    mono = array("h")
    mono.frombytes(tone(source, 1.0, channels=1))
    out = array("h")
    for start in range(0, len(mono), 777):  # odd chunking must not matter
        out.extend(resampler.process(mono[start:start + 777]))
    assert abs(len(out) - target) < target * 0.01
    assert 6000 < max(out) < 8200


# -- the listener -----------------------------------------------------------


def test_the_port_is_open_only_while_somebody_reads(bridge_on):
    port = bridge_on
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    reader = bridge.open_reader(sample_rate=48000)
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        reader.close()
    assert wait_for(lambda: not bridge.HUB.is_open)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()


def test_a_busy_port_is_reported_not_swallowed(bridge_on):
    with socket.socket() as holder:
        holder.bind(("0.0.0.0", bridge_on))
        holder.listen(1)
        with pytest.raises(OSError):
            bridge.open_reader(sample_rate=48000)


def test_host_audio_arrives_as_mono_at_the_recording_rate(bridge_on):
    reader = bridge.open_reader(sample_rate=48000)
    try:
        sock = connect(bridge_on, rate=44100)
        assert wait_for(lambda: bridge.status() == "connected")
        sock.sendall(tone(44100, 1.0))
        raw = read_seconds(reader, 0.9)
        assert 6000 < peak(raw) < 8200
        sock.close()
    finally:
        reader.close()


def test_without_a_host_the_reader_hands_out_paced_silence(bridge_on):
    reader = bridge.open_reader(sample_rate=48000)
    try:
        started = time.monotonic()
        raw = read_seconds(reader, 0.5)
        elapsed = time.monotonic() - started
        assert peak(raw) == 0
        assert 0.35 < elapsed < 1.2  # on the wall clock, not as fast as possible
    finally:
        reader.close()


def test_a_silent_stretch_and_a_reconnect_keep_the_reader_alive(bridge_on):
    reader = bridge.open_reader(sample_rate=48000)
    try:
        first = connect(bridge_on)
        assert wait_for(lambda: bridge.status() == "connected")
        first.close()
        assert wait_for(lambda: bridge.status() == "waiting")
        assert reader.alive
        assert peak(read_seconds(reader, 0.2)) == 0
        second = connect(bridge_on)
        assert wait_for(lambda: bridge.status() == "connected")
        second.sendall(tone(48000, 0.5))
        assert peak(read_seconds(reader, 0.4)) > 6000
        second.close()
    finally:
        reader.close()


def test_a_newer_connection_replaces_the_older(bridge_on):
    reader = bridge.open_reader(sample_rate=48000)
    try:
        old = connect(bridge_on)
        assert wait_for(lambda: bridge.status() == "connected")
        new = connect(bridge_on, channels=1)
        assert wait_for(lambda: bridge.HUB.info.get("channels") == 1)
        new.sendall(tone(48000, 0.5, channels=1))
        assert peak(read_seconds(reader, 0.4)) > 6000
        old.close()
        new.close()
    finally:
        reader.close()


def test_closing_before_the_header_means_no_headset(bridge_on):
    events: list[str] = []
    unsubscribe = bridge.subscribe(lambda event, fields: events.append(event))
    reader = bridge.open_reader(sample_rate=48000)
    try:
        socket.create_connection(("127.0.0.1", bridge_on), timeout=1).close()
        assert wait_for(lambda: "no_headset" in events)
        assert bridge.status() == "no_header"
    finally:
        unsubscribe()
        reader.close()


@pytest.mark.parametrize("line", [b"not json\n", b'{"rate": 5, "channels": 2}\n',
                                  b'{"rate": 48000, "channels": 2, "format": "f32le"}\n'])
def test_an_unreadable_header_is_refused(bridge_on, line):
    events: list[str] = []
    unsubscribe = bridge.subscribe(lambda event, fields: events.append(event))
    reader = bridge.open_reader(sample_rate=48000)
    try:
        with socket.create_connection(("127.0.0.1", bridge_on), timeout=1) as sock:
            sock.sendall(line)
            assert wait_for(lambda: "bad_header" in events)
        assert bridge.status() == "no_header"
    finally:
        unsubscribe()
        reader.close()


def test_only_private_peers_may_send():
    assert bridge._is_local("127.0.0.1")
    assert bridge._is_local("192.168.68.1")
    assert not bridge._is_local("8.8.8.8")
    assert not bridge._is_local("nonsense")


def test_events_name_the_connection_and_its_end(bridge_on):
    events: list[tuple[str, dict]] = []
    unsubscribe = bridge.subscribe(lambda event, fields: events.append((event, fields)))
    reader = bridge.open_reader(sample_rate=48000)
    try:
        sock = connect(bridge_on)
        assert wait_for(lambda: any(e == "connected" for e, _ in events))
        sock.close()
        assert wait_for(lambda: any(e == "disconnected" for e, _ in events))
        connected = next(f for e, f in events if e == "connected")
        assert connected["rate"] == 48000 and connected["device"] == "fake"
        assert abs(connected["host_clock_offset_s"]) < 2
    finally:
        unsubscribe()
        reader.close()


# -- through the capture pipeline -------------------------------------------


def test_the_bridge_is_recorded_like_any_other_source(bridge_on):
    process = CaptureProcess(bridge.DEVICE, side="speaker", sample_rate=48000, block_frames=960)
    group = SourceGroup([process], 960)
    process.start()
    try:
        sock = connect(bridge_on)
        assert wait_for(lambda: bridge.status() == "connected")
        sock.sendall(tone(48000, 1.0))
        loud = 0
        end = time.monotonic() + 3
        while time.monotonic() < end and loud < 20:
            block = group.read(timeout=0.2)
            if block and peak(block) > 6000:
                loud += 1
        assert loud >= 20
        sock.close()
    finally:
        group.stop()


# -- what the recorder tells the user ---------------------------------------


class Collector:
    def __init__(self):
        self.messages: list[tuple[str, str, bool]] = []

    def __call__(self, title, text, kind, urgent=False):
        self.messages.append((kind, text, urgent))


def test_a_missing_host_is_announced_after_a_few_seconds(config, monkeypatch, bridge_on):
    import echolot.session as session

    monkeypatch.setattr(session, "BRIDGE_CONNECT_TIMEOUT", 0.3)
    collector = Collector()
    recorder = Recorder(config, on_notify=collector)
    config.set("devices.speaker", [bridge.DEVICE])
    config.set("devices.mic", [])
    assert recorder.start() or collector.messages  # mic side may be unavailable here
    try:
        assert wait_for(lambda: any(kind == "warning" and urgent for kind, _, urgent in collector.messages), 3)
    finally:
        recorder.stop()


def test_a_recording_with_the_host_streaming_contains_the_other_side(config, bridge_on):
    from echolot import selftest

    if selftest.missing_tools() or not selftest.sound_server_available():
        pytest.skip("needs the sound server for the microphone side")
    recorder = Recorder(config, on_notify=Collector())
    config.set("audio.layout", "split")
    config.set("audio.format", "wav")
    config.set("devices.speaker", [bridge.DEVICE])
    assert recorder.start()
    try:
        sock = connect(bridge_on)
        assert wait_for(lambda: bridge.status() == "connected")
        for _ in range(30):
            sock.sendall(tone(48000, 0.1))
            time.sleep(0.1)
        sock.close()
    finally:
        recorder.stop()
    audio = recorder.files.audio
    _left, right, rate = selftest.channels_of(audio)
    assert peak(right.tobytes()) > 4000, "the host's audio is not in the right channel"
    log = recorder.files.log.read_text(encoding="utf-8")
    assert "bridge_connected" in log
