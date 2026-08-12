"""Device discovery on PipeWire/PulseAudio via `pactl`.

Two things are needed for every recording:

    mic      the source you speak into        -> default source
    speaker  what the other side says         -> monitor source of the default sink

`pactl -f json` is the nicest format but it prints warnings for non-ASCII device
descriptions, so names and states come from the ASCII-safe short listing and JSON
is only consulted for the human readable labels. If JSON fails entirely, labels
are derived from the device name and recording still works - a missing label is
cosmetic, a missing device is not.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ..i18n import t

PACTL_TIMEOUT = 6


@dataclass(frozen=True)
class Device:
    """A capturable source."""

    name: str
    description: str
    is_monitor: bool
    state: str = ""

    def label(self) -> str:
        return self.description or self.name


@dataclass(frozen=True)
class Resolution:
    """The concrete devices a session will record from."""

    mic: str | None
    speaker: str | None
    mic_label: str
    speaker_label: str
    problems: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.mic and self.speaker)


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=PACTL_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _short_list(kind: str) -> list[tuple[str, str]]:
    """`pactl list short <kind>` reduced to (name, state) pairs."""
    out: list[tuple[str, str]] = []
    for line in _run(["pactl", "list", "short", kind]).splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[1]:
            out.append((fields[1], fields[-1].strip() if len(fields) >= 5 else ""))
    return out


def _json_list(kind: str) -> list[dict]:
    """Best effort JSON listing; empty list when pactl output is unusable."""
    raw = _run(["pactl", "-f", "json", "list", kind])
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def monitor_sources() -> dict[str, str]:
    """Map monitor source name -> owning sink name, straight from the sinks."""
    mapping: dict[str, str] = {}
    for sink in _json_list("sinks"):
        monitor = sink.get("monitor_source")
        name = sink.get("name")
        if isinstance(monitor, str) and monitor and isinstance(name, str):
            mapping[monitor] = name
    return mapping


def list_sources() -> list[Device]:
    """All capturable sources: real inputs first, then monitors."""
    labels = {
        entry.get("name"): entry.get("description", "")
        for entry in _json_list("sources")
        if isinstance(entry.get("name"), str)
    }
    monitors = monitor_sources()

    devices: list[Device] = []
    for name, state in _short_list("sources"):
        is_monitor = name in monitors or name.endswith(".monitor")
        devices.append(
            Device(
                name=name,
                description=str(labels.get(name) or _friendly_name(name)),
                is_monitor=is_monitor,
                state=state,
            )
        )
    devices.sort(key=lambda device: (device.is_monitor, device.label().lower()))
    return devices


def _friendly_name(name: str) -> str:
    """Readable fallback label when no description is available."""
    text = name.split(".")[-1] if "." in name else name
    if name.endswith(".monitor"):
        text = t(
            "common.monitor_prefix", name=name.removesuffix(".monitor").split(".")[-1]
        )
    return text.replace("_", " ").replace("-", " ").strip() or name


def default_sink() -> str | None:
    value = _run(["pactl", "get-default-sink"]).strip()
    return value or None


def default_source() -> str | None:
    value = _run(["pactl", "get-default-source"]).strip()
    return value or None


def default_monitor(sources: list[Device] | None = None) -> str | None:
    """Monitor source belonging to the current default sink.

    Falls back to `<sink>.monitor` (the universal naming convention) and, as a
    last resort, to any monitor source that exists - recording the wrong output
    is still better than recording nothing at all.
    """
    sink = default_sink()
    known = {name for name, _ in _short_list("sources")}

    if sink:
        for monitor, owner in monitor_sources().items():
            if owner == sink and monitor in known:
                return monitor
        guess = f"{sink}.monitor"
        if guess in known:
            return guess

    for device in sources if sources is not None else list_sources():
        if device.is_monitor:
            return device.name
    return None


def resolve(mic_setting: str, speaker_setting: str) -> Resolution:
    """Turn the configured values ('auto' or a device name) into real devices."""
    sources = list_sources()
    known = {device.name: device for device in sources}
    problems: list[str] = []

    if mic_setting and mic_setting != "auto":
        mic = mic_setting if mic_setting in known else None
        if mic is None:
            problems.append(t("devices.mic_missing", name=mic_setting))
            mic = default_source()
    else:
        mic = default_source()
    if mic and mic not in known:
        # A default that is not in the source list would fail at capture time.
        problems.append(t("devices.mic_unusable", name=mic))
        mic = next((d.name for d in sources if not d.is_monitor), None)
    if not mic:
        problems.append(t("devices.mic_none"))

    if speaker_setting and speaker_setting != "auto":
        speaker = speaker_setting if speaker_setting in known else None
        if speaker is None:
            problems.append(t("devices.output_missing", name=speaker_setting))
            speaker = default_monitor(sources)
    else:
        speaker = default_monitor(sources)
    if not speaker:
        problems.append(t("devices.monitor_none"))

    return Resolution(
        mic=mic,
        speaker=speaker,
        mic_label=known[mic].label() if mic in known else (mic or "-"),
        speaker_label=known[speaker].label() if speaker in known else (speaker or "-"),
        problems=tuple(problems),
    )
