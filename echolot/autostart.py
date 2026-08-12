"""Autostart entry in `~/.config/autostart/echolot.desktop`.

Written in the same shape as the other autostart entries on this machine, so it
behaves like them in the Cinnamon startup applications list. The comment is
carried in every available language, because a .desktop file is read by the
desktop in the desktop's language, not in ours.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import i18n, paths

COMMENT_KEY = "desktop.comment"


def python_command() -> str:
    """Interpreter to use in the entry - the one running us right now."""
    return sys.executable or "python3"


def exec_command(autostart: bool = True) -> str:
    flag = " --autostart" if autostart else ""
    return f'{python_command()} "{paths.MAIN_SCRIPT}"{flag}'


def icon_path() -> str:
    icon = paths.icon_file("idle")
    return str(icon) if icon.exists() else "media-record"


def comment_lines() -> list[str]:
    """`Comment=` in English plus one `Comment[xx]=` per translation."""
    translations = i18n.translations_of(COMMENT_KEY)
    default = translations.get(i18n.DEFAULT_LANGUAGE, paths.APP_COMMENT)
    lines = [f"Comment={default}"]
    for code, text in sorted(translations.items()):
        if code != i18n.DEFAULT_LANGUAGE:
            lines.append(f"Comment[{code}]={text}")
    return lines


def desktop_entry_text(autostart: bool = True) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={paths.APP_NAME}",
        *comment_lines(),
        f"Exec={exec_command(autostart)}",
        f"Icon={icon_path()}",
        "Terminal=false",
        "StartupNotify=false",
        f"StartupWMClass={paths.APP_ID}",
        "Categories=AudioVideo;Audio;Recorder;",
        "X-GNOME-Autostart-enabled=true",
        "",
    ]
    return "\n".join(lines)


def is_enabled() -> bool:
    path = paths.autostart_file()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "X-GNOME-Autostart-enabled=false" not in text


def enable() -> Path:
    path = paths.autostart_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_entry_text(), encoding="utf-8")
    return path


def disable() -> None:
    paths.autostart_file().unlink(missing_ok=True)


def sync(enabled: bool) -> None:
    """Make the filesystem match the setting."""
    if enabled:
        enable()
    elif paths.autostart_file().exists():
        disable()


def main(argv: list[str] | None = None) -> int:
    """`python3 -m echolot.autostart [--launcher]` prints an entry to stdout.

    Used by install.sh so the installer and the app cannot drift apart.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    print(desktop_entry_text(autostart="--launcher" not in args), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
