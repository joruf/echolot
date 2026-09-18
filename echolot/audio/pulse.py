"""PulseAudio/PipeWire backend: everything Echolot knows about `pactl` and `parec`.

`pactl -f json` is the nicest format but it prints warnings for non-ASCII device
descriptions, so names and states come from the ASCII-safe short listing and JSON
is only consulted for the human readable labels. If JSON fails entirely, labels
are derived from the device name and recording still works - a missing label is
cosmetic, a missing device is not.

The reader is a `parec` child process. Two separate processes rather than one
ffmpeg with two inputs: a device change would otherwise mean restarting the whole
thing, and the file would tear.
"""

from __future__ import annotations

import json
import subprocess
import threading

from ..i18n import t
from .backend import Device

NAME = "pulseaudio"
PACTL_TIMEOUT = 6
PAREC = "parec"
#: What the watcher listens to, and the lines worth waking up for.
WATCH_COMMAND = ["pactl", "subscribe"]
WATCH_MARKERS = ("on server", "on sink", "on source", "on card")


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


# -- the capture stream --------------------------------------------------


class ParecReader:
    """`parec` as a pipe of raw mono PCM.

    Deliberately nothing but the process: the block assembly, the counters and
    the restarting all live in `capture.py`, where they are the same on every
    platform.
    """

    def __init__(
        self, device: str, *, sample_rate: int, channels: int = 1, latency_ms: int = 20
    ) -> None:
        self.device = device
        self.args = [
            PAREC,
            "--raw",
            "-d", device,
            "--format=s16le",
            f"--rate={sample_rate}",
            f"--channels={channels}",
            f"--latency-msec={latency_ms}",
            "--client-name=Echolot",
        ]
        self._process = subprocess.Popen(
            self.args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )

    def read(self, count: int) -> bytes:
        stream = self._process.stdout
        if stream is None:
            return b""
        try:
            return stream.read(count) or b""
        except (OSError, ValueError):
            return b""

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    def close(self, timeout: float = 1.0) -> None:
        process = self._process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass


def open_reader(device: str, *, sample_rate: int, channels: int = 1, latency_ms: int = 20):
    """Start reading from a device. Raises OSError when the tool is missing."""
    return ParecReader(
        device, sample_rate=sample_rate, channels=channels, latency_ms=latency_ms
    )


def available() -> bool:
    """True when parec exists and the sound server answers."""
    try:
        result = subprocess.run(
            ["pactl", "info"], capture_output=True, timeout=PACTL_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
