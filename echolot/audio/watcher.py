"""Watches the sound server so a device change cannot cut a recording short.

`pactl subscribe` streams one line per change. Plugging in a headset produces a
burst of them, so events are debounced before the devices are resolved again and
the result is compared with what is currently being recorded.

If subscribe is unavailable, the watcher falls back to polling. A recording that
keeps the wrong device is a lost conversation, so this must never be the part
that silently gives up.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Callable

from . import devices

DEBOUNCE_SECONDS = 0.4
POLL_SECONDS = 3.0
RESTART_DELAY_SECONDS = 2.0

INTERESTING = ("on server", "on sink", "on source", "on card")


class DeviceWatcher:
    """Calls `on_change(resolution)` whenever the resolved devices change."""

    def __init__(
        self,
        mic_setting: str,
        speaker_setting: str,
        on_change: Callable[[devices.Resolution], None],
        *,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ) -> None:
        self.mic_setting = mic_setting
        self.speaker_setting = speaker_setting
        self.on_change = on_change
        self.debounce_seconds = debounce_seconds
        self.last: devices.Resolution | None = None
        self.changes = 0

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._process: subprocess.Popen | None = None

    def start(self, current: devices.Resolution | None = None) -> None:
        self.last = current
        for target, name in ((self._subscribe_loop, "subscribe"), (self._resolve_loop, "resolve")):
            thread = threading.Thread(target=target, name=f"echolot-watch-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def update_settings(self, mic_setting: str, speaker_setting: str) -> None:
        """Follow different configured devices without a restart."""
        self.mic_setting = mic_setting
        self.speaker_setting = speaker_setting
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    # -- event sources --------------------------------------------------

    def _subscribe_loop(self) -> None:
        """Follow `pactl subscribe`, restarting it if the server goes away."""
        while not self._stop.is_set():
            try:
                self._process = subprocess.Popen(
                    ["pactl", "subscribe"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError:
                self._sleep(RESTART_DELAY_SECONDS)
                continue

            stream = self._process.stdout
            if stream is None:
                self._sleep(RESTART_DELAY_SECONDS)
                continue
            for line in stream:
                if self._stop.is_set():
                    return
                if any(marker in line for marker in INTERESTING):
                    self._wake.set()
            # subscribe ended: the server restarted or died. Re-check devices
            # immediately, then reconnect.
            self._wake.set()
            self._sleep(RESTART_DELAY_SECONDS)

    def _resolve_loop(self) -> None:
        """Debounce wake-ups and poll as a safety net."""
        while not self._stop.is_set():
            triggered = self._wake.wait(POLL_SECONDS)
            if self._stop.is_set():
                return
            if triggered:
                self._wake.clear()
                # Let the burst of events settle before asking pactl.
                time.sleep(self.debounce_seconds)
                self._wake.clear()
            self._check()

    def _check(self) -> None:
        resolution = devices.resolve(self.mic_setting, self.speaker_setting)
        previous = self.last
        if previous is not None and (previous.mics, previous.speakers) == (
            resolution.mics,
            resolution.speakers,
        ):
            return
        self.last = resolution
        if previous is None:
            return
        self.changes += 1
        try:
            self.on_change(resolution)
        except Exception:  # noqa: BLE001 - a callback must never kill the watcher
            pass

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)
