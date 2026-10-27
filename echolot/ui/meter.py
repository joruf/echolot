"""Live level of one device, for looking at before a recording starts.

The question this answers is "will the recording work?", asked while nothing is
being recorded: which of the selected tracks carries sound right now. Each meter
therefore opens its own short-lived capture and closes it again; it never touches
the recording pipeline, so it works the same idle, paused and recording.
"""

from __future__ import annotations

from array import array

from ..audio import capture
from ..audio.mixer import ChannelMetrics, measure

FLOOR_DB = -60.0
# Bars fall back slowly so a short word stays visible between two refreshes.
DECAY_DB_PER_STEP = 4.0


class Meter:
    """A capture on one device and the decaying level read from it."""

    def __init__(self, device: str, side: str, config) -> None:
        self.device = device
        self.side = side
        self.level_db = FLOOR_DB
        self.failed = False
        self._process: capture.CaptureProcess | None = capture.CaptureProcess(
            device,
            side=side,
            sample_rate=int(config.get("audio.sample_rate")),
            block_frames=config.block_frames,
            buffer_seconds=1.0,
        )
        try:
            self._process.start()
        except OSError:
            self._process = None
            self.failed = True

    def poll(self) -> float:
        """Take everything that arrived since the last call; returns the shown dB."""
        peak, total, count = 0, 0.0, 0
        while self._process is not None:
            block = self._process.read(timeout=0)
            if block is None:
                break
            samples = array("h")
            samples.frombytes(block)
            block_peak, block_mean = measure(samples)
            peak = max(peak, block_peak)
            total += block_mean
            count += 1
        fallen = self.level_db - DECAY_DB_PER_STEP
        if count:
            fallen = max(fallen, ChannelMetrics(peak, total / count, True).level_db)
        self.level_db = max(FLOOR_DB, fallen)
        return self.level_db

    @property
    def silent(self) -> bool:
        return self.level_db <= FLOOR_DB

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            process.stop()
