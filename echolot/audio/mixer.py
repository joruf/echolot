"""The timeline of a recording: two mono sides in, one audio stream out.

Two layouts, both fed from the same two sides:

    LAYOUT_MIX    (default)  one mono channel, both voices summed - a normal
                             recording you just listen to
    LAYOUT_SPLIT             two channels, left = microphone, right = monitor,
                             so the voices stay separable in the audio itself

Who said what is measured *before* mixing, per side, so the speech log is
identical in both layouts. Mixing therefore does not cost the transcript its
speaker attribution - it only moves that information from the audio into the log.
What a mix cannot recover is two people talking at the same time.

The mixer never blocks on a side. If one side delivers nothing - process died,
device is being switched - silence is used for it instead, so the timeline stays
gap-free and the two sides stay aligned. The microphone is the clock master
because it always exists; if it stalls too, the monotonic clock takes over and
missing time is filled with silence rather than being quietly swallowed.
"""

from __future__ import annotations

import math
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Callable

from ..i18n import t

# Never inject more than a second of catch-up silence in one go, otherwise a
# long system stall would produce a burst of writes.
MAX_CATCHUP_BLOCKS = 50
# Buffered blocks that may sit in a side without being considered a backlog.
# Both sides deliver in bursts that are not aligned with each other, so this has
# to be large enough that ordinary burst offsets are not mistaken for a stall.
BACKLOG_TARGET_BLOCKS = 5
# How long the follower side may take to hand over its block before silence is
# written for it. Same reasoning: a burst that is a few blocks late is normal.
FOLLOWER_GRACE_SECONDS = 0.1
# Only wall-clock time beyond this is treated as genuinely lost and filled with
# silence; below it, the next real block simply arrives a moment late.
CATCHUP_TOLERANCE_SECONDS = 0.2
FULL_SCALE = 32768.0

#: Canonical side names. Defined here because this is the lowest layer that
#: knows about the two sides at all; speechlog and preroll import them.
SIDE_MIC = "mic"
SIDE_SPEAKER = "speaker"

LAYOUT_MIX = "mix"
LAYOUT_SPLIT = "split"
LAYOUTS = (LAYOUT_MIX, LAYOUT_SPLIT)
MAX_SAMPLE = 32767
MIN_SAMPLE = -32768


def channels_for(layout: str) -> int:
    return 2 if layout == LAYOUT_SPLIT else 1


def amplitude_to_db(value: float) -> float:
    """Amplitude (0..32768) as dBFS, floored at -120 dB."""
    if value <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(min(float(value), FULL_SCALE) / FULL_SCALE))


@dataclass(frozen=True)
class ChannelMetrics:
    """Loudness of one channel in one block."""

    peak: int
    mean_abs: float
    present: bool  # False when this block was filled in as silence

    @property
    def peak_db(self) -> float:
        return amplitude_to_db(self.peak)

    @property
    def level_db(self) -> float:
        """Mean absolute value in dB - steadier than peak, better for a VAD."""
        return amplitude_to_db(self.mean_abs)


SILENT_METRICS = ChannelMetrics(peak=0, mean_abs=0.0, present=False)


def sum_samples(parts: list[array]) -> tuple[array, bool]:
    """Sum mono blocks with limiting; returns the result and whether it clipped.

    Summed, not averaged: dividing would cost the whole recording level for the
    sake of a few peaks. Used both for the mono mix of the two sides and for a
    side that consists of several devices.
    """
    if not parts:
        return array("h"), False
    if len(parts) == 1:
        return parts[0], False

    total = list(parts[0])
    for part in parts[1:]:
        total = [first + second for first, second in zip(total, part)]
    clipped = max(total) > MAX_SAMPLE or min(total) < MIN_SAMPLE
    if clipped:
        total = [
            MAX_SAMPLE if value > MAX_SAMPLE else MIN_SAMPLE if value < MIN_SAMPLE else value
            for value in total
        ]
    return array("h", total), clipped


def measure(samples: array) -> tuple[int, float]:
    """Peak and mean absolute amplitude of a mono block."""
    if not samples:
        return 0, 0.0
    peak = max(max(samples), -min(samples))
    return peak, sum(map(abs, samples)) / len(samples)


class Mixer:
    """Pulls blocks from both sides, writes interleaved PCM to the encoder.

    Deliberately *not* a `threading.Thread` subclass: `Thread` uses `_stop`
    internally, and shadowing it breaks `join()` in a way that only shows up
    while shutting down a real recording.
    """

    def __init__(
        self,
        *,
        mic,
        speaker,
        encoder,
        sample_rate: int = 48000,
        block_frames: int = 960,
        layout: str = LAYOUT_MIX,
        initial_blocks: int = 0,
        silent_alert_seconds: float = 0.0,
        on_metrics: Callable[[float, ChannelMetrics, ChannelMetrics], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_silent_side: Callable[[str, float], None] | None = None,
    ) -> None:
        self.mic = mic
        self.speaker = speaker
        self.encoder = encoder
        self.sample_rate = sample_rate
        self.block_frames = block_frames
        self.block_seconds = block_frames / sample_rate
        self.layout = layout if layout in LAYOUTS else LAYOUT_MIX
        self.channels = channels_for(self.layout)
        self.on_metrics = on_metrics
        self.on_error = on_error
        self.on_silent_side = on_silent_side

        # Consecutive blocks in which a side delivered data that was nothing but
        # exact zeros, per side. Reported once, while the conversation still runs.
        self.zero_blocks = {SIDE_MIC: 0, SIDE_SPEAKER: 0}
        self._alert_blocks = (
            int(silent_alert_seconds / self.block_seconds) if silent_alert_seconds > 0 else 0
        )
        self._alerted: set[str] = set()

        # Blocks already in the file before this mixer writes anything - the
        # pre-roll. The timeline continues from there, so log timestamps keep
        # matching the audio.
        self.initial_blocks = max(0, int(initial_blocks))
        self.blocks_written = self.initial_blocks
        self.mic_gap_blocks = 0
        self.speaker_gap_blocks = 0
        self.silence_filled_blocks = 0
        self.clipped_blocks = 0
        self.last_mic = SILENT_METRICS
        self.last_speaker = SILENT_METRICS

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._paused = False
        self._pause_started = 0.0
        self._paused_seconds = 0.0
        self._timeline_start = 0.0
        self._silence = array("h", bytes(block_frames * 2))
        self._out = array("h", bytes(block_frames * 2 * self.channels))
        self._encoder_failed = False

    # -- public state ---------------------------------------------------

    @property
    def seconds_written(self) -> float:
        return self.blocks_written * self.block_seconds

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def encoder_failed(self) -> bool:
        return self._encoder_failed

    @property
    def stopping(self) -> bool:
        """True once stop() was called - lets a pre-roll flush abort early."""
        return self._stop_event.is_set()

    def levels(self) -> tuple[ChannelMetrics, ChannelMetrics]:
        return self.last_mic, self.last_speaker

    # -- control --------------------------------------------------------

    def pause(self) -> None:
        if self._paused:
            return
        self._pause_started = time.monotonic()
        self._paused = True

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused_seconds += time.monotonic() - self._pause_started
        self._paused = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name="echolot-mixer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def runs_in_current_thread(self) -> bool:
        """True when called from the mixer thread itself (never join from there)."""
        return self._thread is threading.current_thread()

    # -- loop -----------------------------------------------------------

    def run(self) -> None:
        # Pretend the pre-roll was written in real time, so the wall-clock
        # arithmetic below stays valid for everything that follows.
        self._timeline_start = time.monotonic() - self.initial_blocks * self.block_seconds
        # Generous on purpose. parec hands data over in bursts, and every block
        # we declare missing inserts silence, which shifts that channel against
        # the other one for the rest of the recording. Waiting a fifth of a
        # second costs nothing for a recorder, and a side that is starving for
        # longer than that is a real outage worth logging. The backlog drain
        # below makes the long timeout safe: audio piling up on the other side
        # is written out in order, never dropped.
        wait = max(0.2, self.block_seconds * 5)
        master = self.mic if self.mic is not None else self.speaker

        while not self._stop_event.is_set():
            if self._paused:
                self._idle_while_paused()
                continue

            if master is None:
                # No device at all: keep the timeline alive so the file length
                # still matches the wall clock.
                time.sleep(self.block_seconds)
                self._catch_up()
                continue

            master_block = master.read(timeout=wait)
            if master is self.mic:
                mic_block = master_block
                speaker_block = self._pull(self.speaker, FOLLOWER_GRACE_SECONDS)
            else:
                mic_block = self._pull(self.mic, FOLLOWER_GRACE_SECONDS)
                speaker_block = master_block

            self._write(mic_block, speaker_block)
            if self._stop_event.is_set():
                break
            self._drain_backlog()
            self._catch_up()

    def _pull(self, source, grace: float = 0.0) -> bytes | None:
        """Oldest buffered block of the follower side."""
        return source.read(timeout=grace) if source is not None else None

    def _drain_backlog(self) -> None:
        """Write out blocks that piled up on one side.

        Happens when the other side stalls: the loop then paces on a timeout
        instead of on real audio and falls behind. Blocks are written in order
        and never discarded - a backlog on the speaker side is the other person
        talking while our microphone is being restarted.
        """
        written = 0
        while written < MAX_CATCHUP_BLOCKS and not self._stop_event.is_set():
            pending = max(
                self.mic.pending() if self.mic is not None else 0,
                self.speaker.pending() if self.speaker is not None else 0,
            )
            if pending <= BACKLOG_TARGET_BLOCKS:
                return
            self._write(self._pull(self.mic), self._pull(self.speaker))
            written += 1

    def _idle_while_paused(self) -> None:
        # Keep the buffers empty: a paused recording must not resume by
        # replaying seconds of stale audio.
        if self.mic is not None:
            self.mic.drain()
        if self.speaker is not None:
            self.speaker.drain()
        time.sleep(self.block_seconds)

    def _as_samples(self, block: bytes | None) -> tuple[array, bool]:
        if block is None:
            return self._silence, False
        expected = self.block_frames * 2
        if len(block) != expected:
            block = block[:expected].ljust(expected, b"\x00")
        samples = array("h")
        samples.frombytes(block)
        return samples, True

    def _render(
        self,
        mic_samples: array,
        mic_present: bool,
        speaker_samples: array,
        speaker_present: bool,
    ) -> bytes:
        """Turn the two sides into the bytes the encoder expects."""
        if self.channels == 2:
            out = self._out
            out[0::2] = mic_samples
            out[1::2] = speaker_samples
            return out.tobytes()

        # Mono. A side that delivered nothing needs no addition at all, which is
        # also the common case: usually only one person is talking.
        if not speaker_present:
            return mic_samples.tobytes()
        if not mic_present:
            return speaker_samples.tobytes()

        # Both sides loud at the same instant is the only case that can clip.
        total, clipped = sum_samples([mic_samples, speaker_samples])
        if clipped:
            self.clipped_blocks += 1
        return total.tobytes()

    def _write(self, mic_block: bytes | None, speaker_block: bytes | None) -> None:
        mic_samples, mic_present = self._as_samples(mic_block)
        speaker_samples, speaker_present = self._as_samples(speaker_block)

        start_seconds = self.blocks_written * self.block_seconds
        payload = self._render(mic_samples, mic_present, speaker_samples, speaker_present)

        if not self.encoder.write(payload):
            self._encoder_failed = True
            self._stop_event.set()
            if self.on_error is not None:
                self.on_error(t("session.encoder_died"))
            return

        self.blocks_written += 1
        if not mic_present:
            self.mic_gap_blocks += 1
        if not speaker_present:
            self.speaker_gap_blocks += 1

        mic_peak, mic_mav = measure(mic_samples) if mic_present else (0, 0.0)
        speaker_peak, speaker_mav = measure(speaker_samples) if speaker_present else (0, 0.0)
        mic_metrics = ChannelMetrics(mic_peak, mic_mav, mic_present)
        speaker_metrics = ChannelMetrics(speaker_peak, speaker_mav, speaker_present)
        self.last_mic = mic_metrics
        self.last_speaker = speaker_metrics
        self._watch_for_digital_silence(SIDE_MIC, mic_metrics)
        self._watch_for_digital_silence(SIDE_SPEAKER, speaker_metrics)

        if self.on_metrics is not None:
            self.on_metrics(start_seconds, mic_metrics, speaker_metrics)

    def _watch_for_digital_silence(self, side: str, metrics: ChannelMetrics) -> None:
        """Notice a side that delivers data consisting of nothing but zeros.

        This is a different thing from nobody talking, and that difference is the
        whole point: any real source carries a noise floor, so samples that are
        all exactly zero mean there is no audio stream at all - typically the
        conversation is playing somewhere this machine cannot see. Reporting that
        while the conversation is still running is the only moment it helps;
        afterwards it is merely an explanation.

        Blocks that were filled in carry no information and are skipped, so an
        outage neither triggers nor clears the alert.
        """
        if not metrics.present:
            return
        if metrics.peak > 0:
            self.zero_blocks[side] = 0
            return

        self.zero_blocks[side] += 1
        if (
            not self._alert_blocks
            or side in self._alerted
            or self.zero_blocks[side] < self._alert_blocks
        ):
            return
        self._alerted.add(side)
        if self.on_silent_side is not None:
            self.on_silent_side(side, self.zero_blocks[side] * self.block_seconds)

    def _catch_up(self) -> None:
        """Fill in wall-clock time in which neither side produced any audio.

        This is the both-sides-dead case (sound server gone, all devices
        removed). It runs only while both buffers are empty, so real audio is
        never replaced by silence, and only beyond a tolerance, so ordinary
        jitter does not leave holes.
        """
        injected = 0
        while injected < MAX_CATCHUP_BLOCKS and not self._stop_event.is_set():
            target = (
                self._timeline_start
                + self._paused_seconds
                + self.blocks_written * self.block_seconds
            )
            if time.monotonic() - target <= CATCHUP_TOLERANCE_SECONDS:
                return
            if (self.mic is not None and self.mic.pending() > 0) or (
                self.speaker is not None and self.speaker.pending() > 0
            ):
                return
            self._write(None, None)
            self.silence_filled_blocks += 1
            injected += 1
