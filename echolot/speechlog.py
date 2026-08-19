"""The speech log: who talked when, as JSON Lines next to the audio file.

One line per event, written and flushed immediately - a log that only survives a
clean shutdown would be worthless for exactly the sessions you care about.

    {"type":"session", ...}                                  header, first line
    {"type":"speech","src":"mic","start":12.4,"end":15.1}     one utterance
    {"type":"device_change", ...}                             routing changed
    {"type":"source_error", ...}                              a side went silent
    {"type":"session_end", ...}                               totals, last line

Segments are written when they *end*, so lines are not globally sorted by start
time; a consumer sorts by `start`. All times are seconds from the start of the
audio file, which makes them directly usable as transcript offsets.
"""

from __future__ import annotations

import collections
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .audio.mixer import SIDE_MIC, SIDE_SPEAKER, ChannelMetrics

#: Re-exported so everything above this layer keeps importing them from here.
MIC = SIDE_MIC
SPEAKER = SIDE_SPEAKER

# How far above the measured noise floor a block has to be to count as speech.
ADAPTIVE_MARGIN_DB = 9.0
INITIAL_FLOOR_DB = -70.0
# Window the noise floor is estimated over, split into buckets so the minimum can
# be maintained in constant time.
NOISE_WINDOW_SECONDS = 3.0
NOISE_BUCKETS = 4


class NoiseFloor:
    """Sliding-window minimum of the level - the classic noise estimator.

    Estimating the floor only from pauses sounds obvious and is wrong: in a room
    whose background noise sits above the fixed threshold, every block counts as
    speech, no pause is ever detected, nothing is ever learned, and the log
    degenerates into one endless utterance. The minimum over a few seconds does
    not have that failure mode - natural speech has gaps between words, so the
    quietest block in the window is the room and not the voice.
    """

    def __init__(
        self,
        block_seconds: float,
        window_seconds: float = NOISE_WINDOW_SECONDS,
        buckets: int = NOISE_BUCKETS,
    ) -> None:
        self.blocks_per_bucket = max(1, int(window_seconds / buckets / max(block_seconds, 1e-6)))
        self._buckets: collections.deque[float] = collections.deque(maxlen=max(1, buckets))
        self._current: float | None = None
        self._count = 0
        self.value = INITIAL_FLOOR_DB

    def update(self, level_db: float) -> float:
        self._current = level_db if self._current is None else min(self._current, level_db)
        self._count += 1
        if self._count >= self.blocks_per_bucket:
            self._buckets.append(self._current)
            self._current = None
            self._count = 0
        candidates = list(self._buckets)
        if self._current is not None:
            candidates.append(self._current)
        self.value = min(candidates) if candidates else INITIAL_FLOOR_DB
        return self.value


@dataclass(frozen=True)
class Segment:
    """One continuous utterance on one channel."""

    src: str
    start: float
    end: float
    peak_db: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class ChannelVad:
    """Energy based voice activity detection for a single channel.

    Deliberately simple and dependency free: it decides *when* someone spoke, it
    does not try to decide *what* was said. Short blips are dropped, short pauses
    inside a sentence are bridged.
    """

    def __init__(
        self,
        src: str,
        *,
        threshold_db: float = -45.0,
        min_segment_ms: int = 250,
        hangover_ms: int = 400,
        adaptive: bool = True,
        block_ms: int = 20,
    ) -> None:
        self.src = src
        self.threshold_db = float(threshold_db)
        self.min_segment_seconds = max(0.0, min_segment_ms / 1000.0)
        self.hangover_seconds = max(0.0, hangover_ms / 1000.0)
        self.adaptive = adaptive
        self.block_seconds = block_ms / 1000.0
        self.noise = NoiseFloor(self.block_seconds)

        self._start: float | None = None
        self._last_voice_end: float = 0.0
        self._peak_db: float = -120.0

    @property
    def in_speech(self) -> bool:
        return self._start is not None

    @property
    def floor_db(self) -> float:
        return self.noise.value

    def effective_threshold_db(self) -> float:
        if not self.adaptive:
            return self.threshold_db
        return max(self.threshold_db, self.floor_db + ADAPTIVE_MARGIN_DB)

    def feed(self, block_start: float, metrics: ChannelMetrics) -> Segment | None:
        """Consume one block; returns a segment when one just ended."""
        block_end = block_start + self.block_seconds
        # A filled-in block carries no information: it is neither speech nor a
        # valid noise sample, so it must not train the floor either.
        if metrics.present and self.adaptive:
            self.noise.update(metrics.level_db)

        threshold = self.effective_threshold_db()
        voiced = metrics.present and metrics.level_db > threshold

        if voiced:
            if self._start is None:
                self._start = block_start
                self._peak_db = metrics.peak_db
            else:
                self._peak_db = max(self._peak_db, metrics.peak_db)
            self._last_voice_end = block_end
            return None

        if self._start is None:
            return None
        if block_end - self._last_voice_end < self.hangover_seconds:
            return None
        return self._close()

    def flush(self) -> Segment | None:
        """End of recording: close a segment that is still open."""
        return self._close() if self._start is not None else None

    def _close(self) -> Segment | None:
        start, self._start = self._start, None
        end = self._last_voice_end
        peak_db, self._peak_db = self._peak_db, -120.0
        if start is None or end - start < self.min_segment_seconds:
            return None
        return Segment(src=self.src, start=start, end=end, peak_db=peak_db)


class SpeechLog:
    """JSON Lines writer for one session."""

    def __init__(
        self,
        path: Path,
        *,
        threshold_db: float = -45.0,
        min_segment_ms: int = 250,
        hangover_ms: int = 400,
        adaptive: bool = True,
        block_ms: int = 20,
    ) -> None:
        self.path = Path(path)
        self.block_ms = block_ms
        self.lines_written = 0
        self.segments_written = 0
        self.speech_seconds: dict[str, float] = {MIC: 0.0, SPEAKER: 0.0}
        self.write_error: str | None = None

        vad_args = dict(
            threshold_db=threshold_db,
            min_segment_ms=min_segment_ms,
            hangover_ms=hangover_ms,
            adaptive=adaptive,
            block_ms=block_ms,
        )
        self.vad = {MIC: ChannelVad(MIC, **vad_args), SPEAKER: ChannelVad(SPEAKER, **vad_args)}

        self._stream = None
        self._lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def open(self, header: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("w", encoding="utf-8")
        self._write({"type": "session", **header})

    def close(self, **totals: Any) -> None:
        for vad in self.vad.values():
            segment = vad.flush()
            if segment is not None:
                self._emit_segment(segment)
        self._write(
            {
                "type": "session_end",
                "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "speech_seconds": {
                    key: round(value, 2) for key, value in self.speech_seconds.items()
                },
                "segments": self.segments_written,
                **totals,
            }
        )
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    # -- writing --------------------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        with self._lock:
            stream = self._stream
            if stream is None:
                return
            try:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                stream.flush()
                self.lines_written += 1
            except OSError as exc:
                # Losing the log must not take the audio down with it.
                self.write_error = str(exc)

    def _emit_segment(self, segment: Segment) -> None:
        self.speech_seconds[segment.src] = self.speech_seconds.get(segment.src, 0.0) + segment.duration
        self.segments_written += 1
        self._write(
            {
                "type": "speech",
                "src": segment.src,
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "duration": round(segment.duration, 2),
                "peak_db": round(segment.peak_db, 1),
            }
        )

    def feed(
        self, block_start: float, mic_metrics: ChannelMetrics, speaker_metrics: ChannelMetrics
    ) -> None:
        """Called for every mixer block, from the mixer thread."""
        for src, metrics in ((MIC, mic_metrics), (SPEAKER, speaker_metrics)):
            segment = self.vad[src].feed(block_start, metrics)
            if segment is not None:
                self._emit_segment(segment)

    def event(self, event_type: str, at_seconds: float | None = None, **fields: Any) -> None:
        """Record something that is not speech: routing, failures, pauses."""
        payload: dict[str, Any] = {"type": event_type}
        if at_seconds is not None:
            payload["t"] = round(at_seconds, 2)
        payload.update(fields)
        self._write(payload)
