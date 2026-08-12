"""Encoding side of the pipeline: raw PCM in, one audio file out.

Mono for a mixed recording, two channels for a split one. In the split case the
channels have to stay separable, otherwise the layout would be pointless: for
Opus that means `-mapping_family 255`, which codes both channels discretely
instead of using stereo coupling, so nothing bleeds from your microphone into the
other side's channel.

Ogg/Opus is also written page by page, so a file left behind by a crash or a
power loss stays playable up to the last written page.
"""

from __future__ import annotations

import collections
import subprocess
import threading
from pathlib import Path

FFMPEG = "ffmpeg"
STDERR_KEEP_LINES = 40


class EncoderError(RuntimeError):
    pass


class Encoder:
    """A running ffmpeg process fed through stdin."""

    def __init__(
        self,
        path: Path,
        *,
        audio_format: str = "opus",
        bitrate_kbps: int = 64,
        sample_rate: int = 48000,
        channels: int = 2,
        title: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.audio_format = audio_format
        self.bitrate_kbps = bitrate_kbps
        self.sample_rate = sample_rate
        self.channels = channels
        self.title = title
        self.bytes_written = 0
        self._process: subprocess.Popen | None = None
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=STDERR_KEEP_LINES)
        self._stderr_thread: threading.Thread | None = None
        self._broken = False

    # -- command line ---------------------------------------------------

    def command(self) -> list[str]:
        args = [
            FFMPEG,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
        ]
        if self.title:
            args += ["-metadata", f"title={self.title}"]

        if self.audio_format == "opus":
            args += [
                "-c:a", "libopus",
                "-b:a", f"{self.bitrate_kbps}k",
                "-vbr", "on",
                "-application", "voip",
                "-frame_duration", "20",
            ]
            if self.channels > 1:
                # Discrete channels: keeps mic and speaker from bleeding into
                # each other, which stereo coupling would do at low bitrates.
                # Pointless for a single channel, so only set when there are two.
                args += ["-mapping_family", "255"]
        elif self.audio_format == "flac":
            args += ["-c:a", "flac", "-compression_level", "5"]
        else:
            args += ["-c:a", "pcm_s16le"]

        # Push data out continuously instead of buffering it in ffmpeg, so an
        # aborted session loses at most a fraction of a second.
        args += ["-flush_packets", "1", "-y", str(self.path)]
        return args

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise EncoderError(f"ffmpeg konnte nicht gestartet werden: {exc}") from exc

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="echolot-ffmpeg-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._stderr_lines.append(line)

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_lines)

    def write(self, data: bytes) -> bool:
        """Feed one block. False once the pipe is gone (ffmpeg died)."""
        process = self._process
        if process is None or process.stdin is None or self._broken:
            return False
        try:
            process.stdin.write(data)
            self.bytes_written += len(data)
            return True
        except (BrokenPipeError, ValueError, OSError):
            self._broken = True
            return False

    def close(self, timeout: float = 15.0) -> int | None:
        """Close stdin and let ffmpeg finalise the container."""
        process = self._process
        if process is None:
            return None
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        return process.returncode


def probe_available() -> str | None:
    """Version string of the available ffmpeg, or None if it is missing."""
    try:
        result = subprocess.run(
            [FFMPEG, "-version"], capture_output=True, text=True, timeout=6, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()[0] if result.stdout else "ffmpeg"
