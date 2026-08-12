"""Settings of Echolot, stored as JSON in `~/.config/echolot/settings.json`.

The file is meant to be hand-editable, so every value is clamped on load: a typo
in the config must not take the tray down, it must fall back to something sane.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import i18n, paths
from .audio.mixer import LAYOUT_MIX, LAYOUTS, channels_for
from .audio.preroll import MAX_MINUTES as MAX_PREROLL_MINUTES

AUTO = "auto"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    # Interface language. English is the default; every file in echolot/locales
    # is offered, so adding a language needs no code change.
    "language": i18n.DEFAULT_LANGUAGE,
    # None means "use ~/Downloads/Echolot", resolved at runtime so that moving
    # the download folder keeps working.
    "recordings_dir": None,
    "audio": {
        "format": "opus",  # opus | flac | wav
        # mix   = one mono track, both voices together (a normal recording)
        # split = two channels, left = microphone, right = the other side
        # Either way the log says who spoke when: it is measured before mixing.
        "layout": "mix",
        # Minutes of audio kept in RAM while idle, so a recording can start in
        # the past. 0 = off. Costs a permanently open microphone and about
        # 5.5 MB per minute (11 MB with two channels).
        "preroll_minutes": 0,
        "bitrate_kbps": 64,
        "sample_rate": 48000,
        "block_ms": 20,
    },
    "devices": {
        "mic": AUTO,  # AUTO or a PulseAudio/PipeWire source name
        "speaker": AUTO,  # AUTO means "monitor of the default sink"
        "follow_default": True,  # switch along when the default device changes
    },
    "tray": {
        "blink": True,
        "blink_interval_ms": 700,
    },
    "notifications": {
        "on_start": True,
        "on_stop": True,
        "on_error": True,
    },
    "vad": {
        "threshold_db": -45.0,
        "min_segment_ms": 250,
        "hangover_ms": 400,
        "adaptive_noise_floor": True,
    },
    "disk": {
        "warn_mb": 1024,
        "stop_mb": 300,
        "check_interval_s": 15,
    },
    "autostart": True,
    "recent_limit": 5,
}

_FORMATS = ("opus", "flac", "wav")

# key -> (minimum, maximum); applied after type coercion.
_RANGES: dict[str, tuple[float, float]] = {
    "audio.bitrate_kbps": (16, 512),
    "audio.preroll_minutes": (0, MAX_PREROLL_MINUTES),
    "audio.block_ms": (10, 100),
    "tray.blink_interval_ms": (200, 5000),
    "vad.threshold_db": (-90.0, -5.0),
    "vad.min_segment_ms": (0, 5000),
    "vad.hangover_ms": (0, 5000),
    "disk.warn_mb": (0, 1_000_000),
    "disk.stop_mb": (0, 1_000_000),
    "disk.check_interval_s": (1, 3600),
    "recent_limit": (1, 25),
}

_INT_KEYS = (
    "audio.bitrate_kbps",
    "audio.preroll_minutes",
    "audio.block_ms",
    "audio.sample_rate",
    "tray.blink_interval_ms",
    "vad.min_segment_ms",
    "vad.hangover_ms",
    "disk.warn_mb",
    "disk.stop_mb",
    "disk.check_interval_s",
    "recent_limit",
)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _dig(data: dict, dotted: str) -> tuple[dict, str] | tuple[None, None]:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return None, None
        node = nxt
    return node, parts[-1]


class Config:
    """Loaded settings with dotted-path access."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.settings_file()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load_error: str | None = None
        # True when the file exists but predates settings added since it was
        # written. Saving it once keeps the hand-editable file complete.
        self.needs_migration = False

    # -- loading / saving ------------------------------------------------

    def load(self) -> "Config":
        self.load_error = None
        self.needs_migration = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root is not an object")
            self.data = _deep_merge(DEFAULTS, raw)
            self.needs_migration = bool(set(DEFAULTS_FLAT) - set(_flatten(raw)))
        except FileNotFoundError:
            self.data = copy.deepcopy(DEFAULTS)
        except (OSError, ValueError) as exc:
            # A broken file must not stop the tray from coming up.
            self.load_error = f"{self.path}: {exc}"
            self.data = copy.deepcopy(DEFAULTS)
        self.validate()
        return self

    def save(self) -> None:
        """Atomic write, so a crash mid-save cannot leave a truncated file."""
        self.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=".settings-", delete=False
        )
        try:
            with handle as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(handle.name, self.path)
        except OSError:
            Path(handle.name).unlink(missing_ok=True)
            raise

    # -- access ---------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node, key = _dig(self.data, dotted)
        if node is None:
            return default
        return node.get(key, default)

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def update(self, values: dict[str, Any]) -> None:
        for dotted, value in values.items():
            self.set(dotted, value)

    # -- derived values -------------------------------------------------

    @property
    def recordings_dir(self) -> Path:
        configured = self.get("recordings_dir")
        if configured:
            return Path(str(configured)).expanduser()
        return paths.default_recordings_dir()

    @property
    def audio_format(self) -> str:
        return str(self.get("audio.format", "opus"))

    @property
    def audio_suffix(self) -> str:
        return paths.audio_suffix(self.audio_format)

    @property
    def language(self) -> str:
        return str(self.get("language", i18n.DEFAULT_LANGUAGE))

    def apply_language(self) -> None:
        """Make the translator use the configured language."""
        i18n.set_language(self.language)

    @property
    def audio_layout(self) -> str:
        layout = str(self.get("audio.layout", LAYOUT_MIX))
        return layout if layout in LAYOUTS else LAYOUT_MIX

    @property
    def audio_channels(self) -> int:
        return channels_for(self.audio_layout)

    @property
    def block_frames(self) -> int:
        """Samples per channel in one mixer block."""
        return int(self.get("audio.sample_rate") * self.get("audio.block_ms") / 1000)

    # -- sanitising -----------------------------------------------------

    def validate(self) -> None:
        """Coerce types and clamp ranges; unusable values fall back to defaults."""
        fmt = str(self.get("audio.format", "opus")).lower()
        self.set("audio.format", fmt if fmt in _FORMATS else "opus")

        layout = str(self.get("audio.layout", LAYOUT_MIX)).lower()
        self.set("audio.layout", layout if layout in LAYOUTS else LAYOUT_MIX)

        language = str(self.get("language", i18n.DEFAULT_LANGUAGE)).lower()
        self.set(
            "language",
            language if language in i18n.language_codes() else i18n.DEFAULT_LANGUAGE,
        )

        rate = self.get("audio.sample_rate")
        try:
            rate = int(rate)
        except (TypeError, ValueError):
            rate = 48000
        # Opus only encodes these rates; anything else gets resampled anyway, so
        # we pick the nearest supported one to keep the pipeline honest.
        self.set("audio.sample_rate", rate if rate in (8000, 12000, 16000, 24000, 48000) else 48000)

        for key in _INT_KEYS:
            try:
                self.set(key, int(float(self.get(key, DEFAULTS_FLAT.get(key, 0)))))
            except (TypeError, ValueError):
                self.set(key, DEFAULTS_FLAT.get(key, 0))

        try:
            self.set("vad.threshold_db", float(self.get("vad.threshold_db")))
        except (TypeError, ValueError):
            self.set("vad.threshold_db", DEFAULTS["vad"]["threshold_db"])

        for key, (low, high) in _RANGES.items():
            value = self.get(key)
            if isinstance(value, (int, float)):
                clamped = min(max(value, low), high)
                self.set(key, int(clamped) if key in _INT_KEYS else clamped)

        for key in (
            "devices.follow_default",
            "tray.blink",
            "notifications.on_start",
            "notifications.on_stop",
            "notifications.on_error",
            "vad.adaptive_noise_floor",
            "autostart",
        ):
            self.set(key, bool(self.get(key)))

        for key in ("devices.mic", "devices.speaker"):
            value = self.get(key)
            self.set(key, AUTO if not isinstance(value, str) or not value.strip() else value.strip())

        # A stop threshold above the warn threshold would warn after stopping.
        if self.get("disk.stop_mb") > self.get("disk.warn_mb"):
            self.set("disk.warn_mb", self.get("disk.stop_mb"))


def _flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, f"{dotted}."))
        else:
            out[dotted] = value
    return out


DEFAULTS_FLAT = _flatten(DEFAULTS)
