"""Capture side of the pipeline: one `parec` child process per conversation side.

Each side is captured as mono - the sound server does the downmix, which is both
cheaper and more correct than doing it in Python. A side owns a bounded block
buffer that the mixer pulls from.

Two failure modes matter and are handled here, because both would silently eat
the other person's voice:

* the process dies (device unplugged, server restart) -> respawn with backoff
* the device changes mid-recording -> `retarget()` starts the new process first
  and only then stops the old one, so no block is lost in between
"""

from __future__ import annotations

import collections
import subprocess
import threading
import time
from typing import Callable

PAREC = "parec"
BACKOFF_SECONDS = (0.5, 1.0, 2.0, 4.0, 5.0)
RETARGET_TIMEOUT = 2.0


class _Stream:
    """A single parec process reading from one device.

    Blocks are only handed to the owner while `active` is set. A stream that is
    still warming up during a device change therefore produces no output yet.
    """

    def __init__(
        self,
        device: str,
        *,
        sample_rate: int,
        block_bytes: int,
        latency_ms: int,
        on_block: Callable[[bytes], None],
        name: str,
    ) -> None:
        self.device = device
        self.block_bytes = block_bytes
        self.name = name
        self.active = False
        self.blocks = 0
        self.first_block = threading.Event()
        self.exit_code: int | None = None
        self._on_block = on_block
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._args = [
            PAREC,
            "--raw",
            "-d", device,
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
            f"--latency-msec={latency_ms}",
            "--client-name=Echolot",
        ]

    def start(self) -> None:
        self._process = subprocess.Popen(
            self._args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        )
        self._thread = threading.Thread(target=self._read_loop, name=self.name, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        stdout = process.stdout
        block_bytes = self.block_bytes
        pending = b""
        while not self._stop.is_set():
            try:
                chunk = stdout.read(block_bytes - len(pending))
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk
            if len(pending) < block_bytes:
                continue
            self.blocks += 1
            if not self.first_block.is_set():
                self.first_block.set()
            if self.active:
                self._on_block(pending)
            pending = b""

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process is not None else None

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self.active = False
        process = self._process
        if process is not None:
            self.exit_code = process.poll()
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
        if self._thread is not None:
            self._thread.join(timeout=timeout)


class CaptureProcess:
    """One conversation side: a device, a block buffer and a supervisor."""

    def __init__(
        self,
        device: str,
        *,
        side: str,
        sample_rate: int = 48000,
        block_frames: int = 960,
        buffer_seconds: float = 5.0,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.device = device
        self.side = side
        self.sample_rate = sample_rate
        self.block_frames = block_frames
        self.block_bytes = block_frames * 2  # mono, s16
        self.block_seconds = block_frames / sample_rate
        self.on_event = on_event

        self.restarts = 0
        self.overruns = 0
        self.total_blocks = 0
        self.outage_since: float | None = None

        capacity = max(4, int(buffer_seconds / self.block_seconds))
        self._buffer: collections.deque[bytes] = collections.deque()
        self._capacity = capacity
        self._condition = threading.Condition()
        self._should_run = threading.Event()
        self._stream: _Stream | None = None
        self._supervisor: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- buffer ---------------------------------------------------------

    def _push(self, block: bytes) -> None:
        with self._condition:
            if len(self._buffer) >= self._capacity:
                # The mixer is not keeping up. Dropping the oldest block keeps
                # us close to real time instead of drifting further behind.
                self._buffer.popleft()
                self.overruns += 1
            self._buffer.append(block)
            self.total_blocks += 1
            self._condition.notify()

    def read(self, timeout: float | None = None) -> bytes | None:
        """Oldest buffered block, or None if none arrived within `timeout`.

        A falsy timeout (0 or None) makes this a non-blocking peek.
        """
        with self._condition:
            if not self._buffer and timeout:
                self._condition.wait(timeout)
            if not self._buffer:
                return None
            return self._buffer.popleft()

    def pending(self) -> int:
        with self._condition:
            return len(self._buffer)

    def drain(self) -> None:
        with self._condition:
            self._buffer.clear()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._should_run.set()
        with self._lock:
            self._stream = self._spawn(self.device)
            self._stream.active = True
        self._supervisor = threading.Thread(
            target=self._supervise, name=f"echolot-supervise-{self.side}", daemon=True
        )
        self._supervisor.start()

    def _spawn(self, device: str) -> _Stream:
        latency = max(10, int(self.block_seconds * 1000))
        stream = _Stream(
            device,
            sample_rate=self.sample_rate,
            block_bytes=self.block_bytes,
            latency_ms=latency,
            on_block=self._push,
            name=f"echolot-read-{self.side}",
        )
        stream.start()
        return stream

    def wait_for_first_block(self, timeout: float = 3.0) -> bool:
        with self._lock:
            stream = self._stream
        if stream is None:
            return False
        return stream.first_block.wait(timeout)

    @property
    def alive(self) -> bool:
        with self._lock:
            return self._stream is not None and self._stream.alive

    def _emit(self, event: str, **fields) -> None:
        if self.on_event is not None:
            self.on_event(event, {"side": self.side, **fields})

    def _supervise(self) -> None:
        """Restart the capture process whenever it dies unexpectedly."""
        attempt = 0
        while self._should_run.is_set():
            time.sleep(0.25)
            if not self._should_run.is_set():
                break
            with self._lock:
                stream = self._stream
                device = self.device
            if stream is not None and stream.alive:
                if self.outage_since is not None:
                    self._emit(
                        "source_recovered",
                        device=device,
                        outage_seconds=round(time.monotonic() - self.outage_since, 2),
                    )
                    self.outage_since = None
                attempt = 0
                continue

            if self.outage_since is None:
                self.outage_since = time.monotonic()
                self._emit(
                    "source_error",
                    device=device,
                    exit_code=stream.returncode if stream is not None else None,
                )

            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            time.sleep(delay)
            if not self._should_run.is_set():
                break
            try:
                replacement = self._spawn(device)
            except OSError as exc:
                self._emit("source_spawn_failed", device=device, error=str(exc))
                continue
            replacement.active = True
            with self._lock:
                old, self._stream = self._stream, replacement
            if old is not None:
                old.stop(timeout=0.5)
            self.restarts += 1

    def retarget(self, device: str, timeout: float = RETARGET_TIMEOUT) -> bool:
        """Switch to another device without leaving a hole in the recording.

        The new process has to deliver a block before the old one is stopped;
        if it never does, the old device keeps running and False is returned.
        """
        with self._lock:
            if device == self.device and self._stream is not None and self._stream.alive:
                return True
        try:
            replacement = self._spawn(device)
        except OSError as exc:
            self._emit("source_spawn_failed", device=device, error=str(exc))
            return False

        if not replacement.first_block.wait(timeout):
            replacement.stop(timeout=0.5)
            self._emit("retarget_failed", device=device)
            return False

        with self._lock:
            old, previous = self._stream, self.device
            replacement.active = True
            if old is not None:
                old.active = False
            self._stream = replacement
            self.device = device
        if old is not None:
            old.stop(timeout=0.5)
        self.outage_since = None
        self._emit("device_change", **{"from": previous, "to": device})
        return True

    def stop(self, timeout: float = 1.0) -> None:
        self._should_run.clear()
        if self._supervisor is not None:
            self._supervisor.join(timeout=timeout + 1.0)
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop(timeout=timeout)
        with self._condition:
            self._condition.notify_all()


def probe_available() -> bool:
    """True when parec exists and can talk to the sound server."""
    try:
        result = subprocess.run(
            [PAREC, "--version"], capture_output=True, text=True, timeout=6, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
