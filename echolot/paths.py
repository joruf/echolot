"""Filesystem paths and the file naming scheme of Echolot.

One conversation produces exactly two files that share a basename:

    Echolot_2026-08-12_10-15-03.opus    the audio, 2 discrete channels
    Echolot_2026-08-12_10-15-03.log     the speech log, JSON Lines

Keeping the basename identical is what lets a transcript script pair them up
without any index or database.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .i18n import t

APP_NAME = "Echolot"
APP_ID = "echolot"
# Fallback only: the localised versions live in echolot/locales as
# "desktop.comment" and are what a .desktop entry actually carries.
APP_COMMENT = "Record conversations from the system tray"

PROJECT_DIR = Path(__file__).resolve().parent.parent
RESOURCES_DIR = PROJECT_DIR / "resources"
MAIN_SCRIPT = PROJECT_DIR / "run.py"

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
LOG_SUFFIX = ".log"

# Echolot_<date>_<time> with an optional _2, _3 ... collision counter.
SESSION_NAME_RE = re.compile(
    rf"^{re.escape(APP_NAME)}_(\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}})(?:_(\d+))?$"
)

AUDIO_SUFFIXES = {"opus": ".opus", "flac": ".flac", "wav": ".wav"}


def _xdg_dir(variable: str, fallback: Path) -> Path:
    raw = os.environ.get(variable, "").strip()
    return Path(raw) if raw else fallback


def config_dir() -> Path:
    """Where settings and the instance lock live."""
    return _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / APP_ID


def settings_file() -> Path:
    return config_dir() / "settings.json"


def lock_file() -> Path:
    return config_dir() / f"{APP_ID}.lock"


def autostart_file() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "autostart" / f"{APP_ID}.desktop"


def downloads_dir() -> Path:
    """The user's download folder, honouring localised XDG names."""
    if shutil.which("xdg-user-dir"):
        try:
            out = subprocess.run(
                ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            if out and out != str(Path.home()):
                return Path(out)
        except (OSError, subprocess.SubprocessError):
            pass

    user_dirs = _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in user_dirs.read_text(encoding="utf-8").splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                return Path(os.path.expandvars(value.replace("$HOME", str(Path.home()))))
    except OSError:
        pass

    return Path.home() / "Downloads"


def default_recordings_dir() -> Path:
    """`~/Downloads/Echolot` - one folder per program, flat inside."""
    return downloads_dir() / APP_NAME


def audio_suffix(audio_format: str) -> str:
    """File extension for a configured format; unknown formats stay recognisable."""
    return AUDIO_SUFFIXES.get(audio_format.lower(), f".{audio_format.lower()}")


def timestamp_slug(moment: datetime | None = None) -> str:
    return (moment or datetime.now()).strftime(TIMESTAMP_FORMAT)


def session_basename(moment: datetime | None = None) -> str:
    return f"{APP_NAME}_{timestamp_slug(moment)}"


def unique_session_basename(
    directory: Path, moment: datetime | None = None, audio_format: str = "opus"
) -> str:
    """A basename whose audio and log file do not exist yet.

    Two recordings started inside the same second would otherwise overwrite each
    other; the second one becomes `..._2`.
    """
    base = session_basename(moment)
    suffix = audio_suffix(audio_format)
    candidate, counter = base, 1
    while (directory / f"{candidate}{suffix}").exists() or (
        directory / f"{candidate}{LOG_SUFFIX}"
    ).exists():
        counter += 1
        candidate = f"{base}_{counter}"
    return candidate


def parse_session_basename(basename: str) -> datetime | None:
    """Start time encoded in a session basename, or None if it is not ours."""
    match = SESSION_NAME_RE.match(basename)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), TIMESTAMP_FORMAT)
    except ValueError:
        return None


def icon_file(name: str) -> Path:
    return RESOURCES_DIR / f"{APP_ID}-{name}.svg"


def theme_icon_name() -> str:
    """Icon theme name used by the ``.desktop`` entry.

    The desktop spec accepts a theme name or an absolute path, not a file next to
    the entry, so the idle SVG is installed as ``echolot`` under hicolor.
    """
    return APP_ID


def icons_dir() -> Path:
    """User hicolor theme, honouring ``XDG_DATA_HOME``."""
    return _xdg_dir("XDG_DATA_HOME", Path.home() / ".local" / "share") / "icons" / "hicolor"


def install_theme_icon() -> Path:
    """Copy the idle SVG into the icon theme as ``echolot``.

    :return: Destination path of the installed SVG.
    """
    dest_dir = icons_dir() / "scalable" / "apps"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{APP_ID}.svg"
    src = icon_file("idle")
    if src.is_file():
        shutil.copy2(src, dest)
    cache = shutil.which("gtk-update-icon-cache")
    if cache:
        subprocess.run(
            [cache, "-f", "-t", str(icons_dir())],
            capture_output=True,
            check=False,
            timeout=10,
        )
    return dest


@dataclass(frozen=True)
class Recording:
    """One finished (or currently running) conversation on disk."""

    basename: str
    audio: Path
    log: Path | None
    started_at: datetime
    size_bytes: int

    @property
    def has_log(self) -> bool:
        return self.log is not None

    def label(self) -> str:
        """Menu label: `12.08.2026 10:15 - 3,4 MB`."""
        return f"{self.started_at.strftime('%d.%m.%Y %H:%M')} · {human_size(self.size_bytes)}"


def human_size(size_bytes: int) -> str:
    """Size for the UI, with the decimal separator of the active language."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    separator = t("common.decimal_separator")
    for unit in ("KB", "MB", "GB", "TB"):
        size_bytes /= 1024.0
        if size_bytes < 1024 or unit == "TB":
            text = f"{size_bytes:.1f}" if size_bytes < 100 else f"{size_bytes:.0f}"
            return f"{text.replace('.', separator)} {unit}"
    return f"{size_bytes:.0f} TB"


def list_recordings(directory: Path, limit: int | None = None) -> list[Recording]:
    """Recordings in `directory`, newest first.

    Files are matched by our own naming scheme, so unrelated files dropped into
    the folder are ignored instead of showing up in the menu.
    """
    known_audio = set(AUDIO_SUFFIXES.values())
    found: list[Recording] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []

    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in known_audio:
            continue
        started_at = parse_session_basename(entry.stem)
        if started_at is None:
            continue
        log = entry.with_suffix(LOG_SUFFIX)
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        found.append(
            Recording(
                basename=entry.stem,
                audio=entry,
                log=log if log.exists() else None,
                started_at=started_at,
                size_bytes=size,
            )
        )

    found.sort(key=lambda item: (item.started_at, item.basename), reverse=True)
    return found[:limit] if limit else found
