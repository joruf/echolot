"""Device resolution, driven by real `pactl` output captured on the target machine.

The dangerous failure here is silent: resolving the wrong source records an empty
channel, and that is only noticed after the conversation.
"""

from __future__ import annotations

import json

import pytest

from echolot.audio import devices

SINK = "alsa_output.pci-0000_02_02.0.analog-stereo"
MONITOR = f"{SINK}.monitor"
MIC = "alsa_input.pci-0000_02_02.0.analog-stereo"

SHORT_SOURCES = f"610\t{MONITOR}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n" f"611\t{MIC}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
SHORT_SINKS = f"610\t{SINK}\tPipeWire\ts16le 2ch 48000Hz\tSUSPENDED\n"
JSON_SINKS = json.dumps([{"name": SINK, "description": "Analoges Stereo", "monitor_source": MONITOR}])
JSON_SOURCES = json.dumps(
    [
        {"name": MONITOR, "description": "Monitor of Analoges Stereo"},
        {"name": MIC, "description": "Analoges Stereo"},
    ]
)


@pytest.fixture
def pactl(monkeypatch):
    """Fake `pactl` whose answers can be adjusted per test."""
    answers = {
        ("pactl", "list", "short", "sources"): SHORT_SOURCES,
        ("pactl", "list", "short", "sinks"): SHORT_SINKS,
        ("pactl", "-f", "json", "list", "sinks"): JSON_SINKS,
        ("pactl", "-f", "json", "list", "sources"): JSON_SOURCES,
        ("pactl", "get-default-sink"): f"{SINK}\n",
        ("pactl", "get-default-source"): f"{MIC}\n",
    }
    monkeypatch.setattr(devices, "_run", lambda args: answers.get(tuple(args), ""))
    return answers


def test_list_sources_separates_monitors(pactl):
    found = {device.name: device for device in devices.list_sources()}
    assert found[MIC].is_monitor is False
    assert found[MONITOR].is_monitor is True
    assert found[MIC].description == "Analoges Stereo"


def test_default_monitor_comes_from_the_default_sink(pactl):
    assert devices.default_monitor() == MONITOR


def test_resolve_auto_picks_mic_and_monitor(pactl):
    resolution = devices.resolve("auto", "auto")
    assert (resolution.mic, resolution.speaker) == (MIC, MONITOR)
    assert resolution.complete is True
    assert resolution.problems == ()


def test_resolve_keeps_explicitly_chosen_devices(pactl):
    resolution = devices.resolve(MIC, MONITOR)
    assert (resolution.mic, resolution.speaker) == (MIC, MONITOR)
    assert resolution.problems == ()


def test_resolve_falls_back_when_chosen_device_is_gone(pactl):
    resolution = devices.resolve("alsa_input.headset_weg", "auto")
    assert resolution.mic == MIC  # fell back to the default
    assert any("headset_weg" in problem for problem in resolution.problems)


def test_resolve_reports_missing_monitor(pactl, monkeypatch):
    """A machine without any monitor source cannot record the other side."""
    answers = dict(pactl)
    answers[("pactl", "list", "short", "sources")] = f"611\t{MIC}\tPipeWire\ts16le\tSUSPENDED\n"
    answers[("pactl", "-f", "json", "list", "sinks")] = "[]"
    monkeypatch.setattr(devices, "_run", lambda args: answers.get(tuple(args), ""))

    resolution = devices.resolve("auto", "auto")
    assert resolution.mic == MIC
    assert resolution.speaker is None
    assert resolution.complete is False
    assert any("monitor" in problem.lower() for problem in resolution.problems)


def test_monitor_is_detected_without_json(pactl, monkeypatch):
    """pactl warns on non-ASCII descriptions; recording must not depend on JSON."""
    answers = dict(pactl)
    answers[("pactl", "-f", "json", "list", "sinks")] = "kaputt{"
    answers[("pactl", "-f", "json", "list", "sources")] = "auch kaputt"
    monkeypatch.setattr(devices, "_run", lambda args: answers.get(tuple(args), ""))

    resolution = devices.resolve("auto", "auto")
    assert resolution.speaker == MONITOR  # recognised by the .monitor convention
    assert resolution.complete is True


def test_missing_pactl_is_not_fatal(monkeypatch):
    monkeypatch.setattr(devices, "_run", lambda args: "")
    resolution = devices.resolve("auto", "auto")
    assert resolution.complete is False
    assert resolution.problems
