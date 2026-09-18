"""The PulseAudio backend - the half of capturing that is not shared.

Everything else about a capture (block assembly, restarting, counters) is tested
in test_capture.py and is the same on every platform. What belongs here is only
what `parec` and `pactl` are actually asked for.
"""

from __future__ import annotations

import subprocess

from echolot.audio import pulse


class FakePopen:
    """Records the command line without starting anything."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = None
        self._code = None

    def poll(self):
        return self._code


def test_parec_is_asked_for_raw_mono_pcm(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    reader = pulse.open_reader("alsa_output.test.monitor", sample_rate=48000, latency_ms=20)

    args = reader.args
    assert args[0] == "parec"
    assert "--raw" in args
    assert args[args.index("-d") + 1] == "alsa_output.test.monitor"
    assert "--channels=1" in args  # the server does the downmix
    assert "--format=s16le" in args
    assert "--rate=48000" in args
    assert "--latency-msec=20" in args
    assert "--client-name=Echolot" in args  # so it is recognisable in pavucontrol


def test_a_reader_without_a_pipe_reads_as_finished(monkeypatch):
    """No stdout must read as "stream over", never raise."""
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    reader = pulse.open_reader("x", sample_rate=48000)
    assert reader.read(1024) == b""


def test_a_broken_pipe_reads_as_finished(monkeypatch):
    class Broken:
        def read(self, count):
            raise OSError("pipe went away")

    def popen(args, **kwargs):
        process = FakePopen(args, **kwargs)
        process.stdout = Broken()
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    reader = pulse.open_reader("x", sample_rate=48000)
    # The supervisor restarts on b"", so a dead pipe must not escape as an error.
    assert reader.read(1024) == b""


def test_availability_follows_pactl(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    assert pulse.available() is True
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    )
    assert pulse.available() is False


def test_a_missing_pactl_is_not_a_crash(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("pactl")

    monkeypatch.setattr(subprocess, "run", missing)
    assert pulse.available() is False


def test_the_watcher_is_told_what_to_listen_to():
    assert pulse.WATCH_COMMAND[0] == "pactl"
    assert "on source" in pulse.WATCH_MARKERS
