"""Pre-roll: keep the last few minutes in RAM so a recording can start in the past.

The situation this exists for: someone starts talking, says the one thing that
matters, and only then do you reach for the icon. With a pre-roll of two minutes
the file begins two minutes *before* the double click.

That requires capturing continuously while nothing is being recorded, which is
the honest cost of the feature: two `parec` processes stay open all the time and
the microphone counts as in use. It is therefore off by default.

Memory is the other cost, and it is linear: 5.5 MB per minute for a mixed
recording, 11 MB per minute for two separate channels.

Handing the buffer over to a real recording is deliberately *not* instant: on the
target machine 5 minutes of buffered audio take about 5 seconds to push through
the encoder. That is why the capture buffers here are much larger than during a
normal recording - the live audio has to survive being queued while the pre-roll
is still being written.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from . import devices
from . import watcher as watcher_module
from .capture import CaptureProcess
from .mixer import LAYOUT_MIX, SIDE_MIC, SIDE_SPEAKER, ChannelMetrics, Mixer, channels_for

MAX_MINUTES = 5
# Room for the live audio to queue while the buffer is being flushed into the
# encoder. Measured worst case is ~5 s for a 5 minute buffer; 30 s is generous
# enough that a slow machine cannot lose the beginning of the conversation.
FLUSH_HEADROOM_SECONDS = 30.0

MIC = SIDE_MIC
SPEAKER = SIDE_SPEAKER

Entry = tuple[bytes, ChannelMetrics, ChannelMetrics]


class PrerollRing:
    """A sink shaped like an encoder that keeps only the most recent blocks.

    The mixer writes the rendered block first and reports that block's metrics
    immediately afterwards, so both are paired here into one entry. Keeping them
    together is what lets the speech log stay consistent with the audio when the
    buffer is handed over.
    """

    def __init__(self, max_blocks: int) -> None:
        self.entries: deque[Entry] = deque(maxlen=max(1, int(max_blocks)))
        self._pending: bytes | None = None

    # -- encoder side ---------------------------------------------------

    def write(self, payload: bytes) -> bool:
        self._pending = payload
        return True

    def close(self, timeout: float = 0.0) -> None:
        self._pending = None

    @property
    def bytes_written(self) -> int:
        return sum(len(entry[0]) for entry in self.entries)

    # -- metrics side ---------------------------------------------------

    def feed(
        self, block_start: float, mic_metrics: ChannelMetrics, speaker_metrics: ChannelMetrics
    ) -> None:
        payload, self._pending = self._pending, None
        if payload is None:
            return
        self.entries.append((payload, mic_metrics, speaker_metrics))

    # -- state ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def is_full(self) -> bool:
        return len(self.entries) == self.entries.maxlen

    def clear(self) -> None:
        self.entries.clear()
        self._pending = None

    def take(self) -> list[Entry]:
        """Hand out the buffered blocks and forget them."""
        entries = list(self.entries)
        self.clear()
        return entries


@dataclass
class Handover:
    """What a starting recording inherits from the pre-roll."""

    entries: list[Entry]
    captures: dict[str, CaptureProcess] = field(default_factory=dict)
    resolution: devices.Resolution | None = None
    block_seconds: float = 0.02

    @property
    def blocks(self) -> int:
        return len(self.entries)

    @property
    def seconds(self) -> float:
        return self.blocks * self.block_seconds


class Preroll:
    """Captures into a ring buffer for as long as nothing is being recorded."""

    def __init__(self, config) -> None:
        self.config = config
        self.ring: PrerollRing | None = None
        self.resolution: devices.Resolution | None = None
        self.problems: tuple[str, ...] = ()
        self._captures: dict[str, CaptureProcess] = {}
        self._mixer: Mixer | None = None
        self._watcher: watcher_module.DeviceWatcher | None = None
        self._signature: tuple | None = None
        self._lock = threading.RLock()

    # -- settings -------------------------------------------------------

    @property
    def minutes(self) -> int:
        try:
            value = int(self.config.get("audio.preroll_minutes", 0))
        except (TypeError, ValueError):
            return 0
        return max(0, min(MAX_MINUTES, value))

    def signature(self) -> tuple:
        """Settings that make a running buffer unusable when they change.

        The buffer holds already-rendered blocks, so a different layout or sample
        rate cannot simply be appended to - it has to be rebuilt.
        """
        config = self.config
        return (
            self.minutes,
            config.audio_layout,
            int(config.get("audio.sample_rate")),
            int(config.get("audio.block_ms")),
            config.get("devices.mic"),
            config.get("devices.speaker"),
            bool(config.get("devices.follow_default")),
        )

    @property
    def active(self) -> bool:
        return self._mixer is not None

    @property
    def block_seconds(self) -> float:
        return int(self.config.get("audio.block_ms")) / 1000.0

    @property
    def blocks_buffered(self) -> int:
        return len(self.ring) if self.ring is not None else 0

    @property
    def seconds_buffered(self) -> float:
        return self.blocks_buffered * self.block_seconds

    def memory_bytes(self) -> int:
        """Roughly what a full buffer costs in RAM."""
        rate = int(self.config.get("audio.sample_rate"))
        channels = channels_for(self.config.audio_layout)
        return int(self.minutes * 60 * rate * 2 * channels)

    # -- lifecycle ------------------------------------------------------

    def ensure(self) -> None:
        """Make the running buffer match the settings: start, stop or rebuild."""
        with self._lock:
            wanted = self.signature()
            if self.minutes <= 0:
                self.stop()
                return
            if self.active and wanted == self._signature:
                return
            self.stop()
            self.start()

    def start(self) -> bool:
        with self._lock:
            if self.active or self.minutes <= 0:
                return False

            config = self.config
            resolution = devices.resolve(
                config.get("devices.mic"), config.get("devices.speaker")
            )
            self.resolution = resolution
            self.problems = resolution.problems
            if not resolution.mic and not resolution.speaker:
                return False

            sample_rate = int(config.get("audio.sample_rate"))
            block_frames = config.block_frames
            blocks = int(self.minutes * 60 / self.block_seconds)
            ring = PrerollRing(blocks)

            captures: dict[str, CaptureProcess] = {}
            for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
                if not device:
                    continue
                process = CaptureProcess(
                    device,
                    side=side,
                    sample_rate=sample_rate,
                    block_frames=block_frames,
                    buffer_seconds=FLUSH_HEADROOM_SECONDS,
                )
                try:
                    process.start()
                except OSError:
                    continue
                captures[side] = process

            if not captures:
                return False

            mixer = Mixer(
                mic=captures.get(MIC),
                speaker=captures.get(SPEAKER),
                encoder=ring,
                sample_rate=sample_rate,
                block_frames=block_frames,
                layout=config.audio_layout,
                on_metrics=ring.feed,
            )
            mixer.start()

            self.ring = ring
            self._captures = captures
            self._mixer = mixer
            self._signature = self.signature()

            if config.get("devices.follow_default"):
                watcher = watcher_module.DeviceWatcher(
                    config.get("devices.mic"),
                    config.get("devices.speaker"),
                    self._on_devices_changed,
                )
                watcher.start(current=resolution)
                self._watcher = watcher
            return True

    def stop(self) -> None:
        """Stop capturing and throw the buffer away."""
        with self._lock:
            watcher, self._watcher = self._watcher, None
            mixer, self._mixer = self._mixer, None
            captures, self._captures = self._captures, {}
            ring, self.ring = self.ring, None
            self._signature = None

        if watcher is not None:
            watcher.stop()
        if mixer is not None:
            mixer.stop()
            mixer.join(timeout=2.0)
        for process in captures.values():
            process.stop()
        if ring is not None:
            ring.clear()

    def hand_over(self) -> Handover | None:
        """Give the buffer and the running captures to a starting recording.

        The capture processes are handed over **alive**. Stopping and recreating
        them would open a gap at exactly the moment the user pressed record, which
        is the one moment that must not have one.
        """
        with self._lock:
            if not self.active:
                return None
            watcher, self._watcher = self._watcher, None
            mixer, self._mixer = self._mixer, None
            captures, self._captures = self._captures, {}
            ring, self.ring = self.ring, None
            resolution = self.resolution
            block_seconds = self.block_seconds
            self._signature = None

        if watcher is not None:
            watcher.stop()
        # Stop the mixer and wait for it before touching the ring: only then is
        # nothing appending to it any more.
        if mixer is not None:
            mixer.stop()
            mixer.join(timeout=2.0)

        return Handover(
            entries=ring.take() if ring is not None else [],
            captures=captures,
            resolution=resolution,
            block_seconds=block_seconds,
        )

    # -- devices --------------------------------------------------------

    def _on_devices_changed(self, resolution: devices.Resolution) -> None:
        """Follow the default devices while idle, too.

        Without this, plugging in a headset would poison the buffer for as long
        as it is deep: minutes of the wrong source, unnoticed.
        """
        with self._lock:
            if not self.active:
                return
            self.resolution = resolution
            captures = dict(self._captures)
        for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
            process = captures.get(side)
            if process is not None and device and process.device != device:
                process.retarget(device)
