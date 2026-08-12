"""Desktop notifications with a plain-text fallback.

libnotify is used when available; otherwise `notify-send`; and if neither works
(headless run, no session bus) the message goes to stdout. A notification must
never be the reason a recording fails to start.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from . import paths

_URGENCY = {"info": "normal", "warning": "normal", "error": "critical"}
_ICONS = {"info": "media-record", "warning": "dialog-warning", "error": "dialog-error"}


class Notifier:
    """Sends short status messages to the desktop."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._notify_module = None
        self._handles: dict[str, object] = {}
        self._init_libnotify()

    def _init_libnotify(self) -> None:
        try:
            import gi

            gi.require_version("Notify", "0.7")
            from gi.repository import Notify
        except (ImportError, ValueError):
            return
        try:
            if Notify.is_initted() or Notify.init(paths.APP_NAME):
                self._notify_module = Notify
        except Exception:  # noqa: BLE001 - no session bus, missing service, ...
            self._notify_module = None

    def send(self, title: str, text: str, kind: str = "info") -> None:
        if not self.enabled:
            return
        if self._send_libnotify(title, text, kind):
            return
        if self._send_command(title, text, kind):
            return
        print(f"[{kind}] {title}: {text}", file=sys.stderr)

    def _send_libnotify(self, title: str, text: str, kind: str) -> bool:
        Notify = self._notify_module
        if Notify is None:
            return False
        try:
            # Info messages reuse one handle so start/stop replace each other
            # instead of piling up; warnings and errors get their own.
            key = "status" if kind == "info" else kind
            handle = self._handles.get(key)
            if handle is None:
                handle = Notify.Notification.new(title, text, _ICONS.get(kind, "media-record"))
                self._handles[key] = handle
            else:
                handle.update(title, text, _ICONS.get(kind, "media-record"))
            handle.set_urgency(
                Notify.Urgency.CRITICAL if kind == "error" else Notify.Urgency.NORMAL
            )
            handle.set_hint("desktop-entry", _string_hint(f"{paths.APP_ID}"))
            handle.show()
            return True
        except Exception:  # noqa: BLE001 - fall through to notify-send
            return False

    def _send_command(self, title: str, text: str, kind: str) -> bool:
        if not shutil.which("notify-send"):
            return False
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-a", paths.APP_NAME,
                    "-u", _URGENCY.get(kind, "normal"),
                    "-i", _ICONS.get(kind, "media-record"),
                    title,
                    text,
                ],
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True


def _string_hint(value: str):
    """GLib string variant for notification hints, if GLib is importable."""
    try:
        from gi.repository import GLib

        return GLib.Variant.new_string(value)
    except Exception:  # noqa: BLE001
        return value
