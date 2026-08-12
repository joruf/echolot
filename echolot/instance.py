"""Single instance handling and signal-based remote control.

Only one Echolot may own the tray icon. A second start does not open a second
icon; it signals the running one instead. That also gives us a keyboard shortcut
for free: bind `run.py --toggle` to a key and recording starts without the tray.

    SIGUSR1  ping - re-show the icon and say hello
    SIGUSR2  toggle recording (same action as a double click)
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
from pathlib import Path

from . import paths

PING = signal.SIGUSR1
TOGGLE = signal.SIGUSR2


class InstanceLock:
    """Advisory lock on `~/.config/echolot/echolot.lock` holding our PID.

    The lock is held through an open file descriptor for the whole process
    lifetime; the kernel releases it even if we are killed hard, so a stale lock
    file never blocks the next start.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.lock_file()
        self._handle = None

    def acquire(self) -> bool:
        """True if we now own the lock, False if another instance holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()
        self._handle = None

    def owner_pid(self) -> int | None:
        """PID written by the instance that holds the lock, if it still runs."""
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return None
        if pid <= 0 or pid == os.getpid():
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def signal_owner(self, sig: signal.Signals) -> bool:
        """Send `sig` to the running instance. False if there is none."""
        pid = self.owner_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, sig)
        except OSError:
            return False
        return True
