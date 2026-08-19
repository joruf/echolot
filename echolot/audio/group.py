"""A side of the conversation that consists of several devices.

Recording everything the machine offers means a side is no longer one device: the
microphone side is every input, the other side is every output monitor. The mixer
must not have to know that - it asks one object for the next block, exactly as it
does for a single capture process. This is that object.

Members are summed. A device that is idle delivers digital silence and therefore
contributes nothing, which is what makes "record everything" cheap: only what is
actually playing ends up audible.
"""

from __future__ import annotations

from array import array

from .capture import CaptureProcess
from .mixer import sum_samples


class SourceGroup:
    """Several capture processes presented to the mixer as one source."""

    def __init__(self, processes: list[CaptureProcess], block_frames: int) -> None:
        self.processes = list(processes)
        self.block_frames = block_frames
        self.clipped_blocks = 0
        self._silence = array("h", bytes(block_frames * 2))

    # -- membership -----------------------------------------------------

    @property
    def devices(self) -> list[str]:
        return [process.device for process in self.processes]

    def add(self, process: CaptureProcess) -> None:
        """Take in a device that appeared while the recording was running."""
        self.processes.append(process)

    def __len__(self) -> int:
        return len(self.processes)

    # -- the interface the mixer uses -----------------------------------

    def read(self, timeout: float | None = None) -> bytes | None:
        """One block, summed over every member that has something to say.

        The first member sets the pace, the rest contribute what they already
        have. Waiting for all of them would let one idle device stall a side.
        """
        if not self.processes:
            return None
        if len(self.processes) == 1:
            return self.processes[0].read(timeout=timeout)

        first = self.processes[0].read(timeout=timeout)
        others = [process.read(timeout=0) for process in self.processes[1:]]
        blocks = [block for block in [first, *others] if block is not None]
        if not blocks:
            return None

        parts = []
        for block in blocks:
            samples = array("h")
            samples.frombytes(self._sized(block))
            parts.append(samples)
        total, clipped = sum_samples(parts)
        if clipped:
            self.clipped_blocks += 1
        return total.tobytes()

    def pending(self) -> int:
        return max((process.pending() for process in self.processes), default=0)

    def drain(self) -> None:
        for process in self.processes:
            process.drain()

    def _sized(self, block: bytes) -> bytes:
        expected = self.block_frames * 2
        if len(block) == expected:
            return block
        return block[:expected].ljust(expected, b"\x00")

    # -- what the session uses ------------------------------------------

    @property
    def device(self) -> str | None:
        """The leading device, for code that names a single one."""
        return self.processes[0].device if self.processes else None

    def set_on_event(self, callback) -> None:
        for process in self.processes:
            process.on_event = callback

    def wait_for_first_block(self, timeout: float = 3.0) -> bool:
        """True as soon as any member has delivered - one live device is enough."""
        if not self.processes:
            return False
        share = max(0.2, timeout / len(self.processes))
        return any(process.wait_for_first_block(share) for process in self.processes)

    def retarget(self, device: str, timeout: float = 2.0) -> bool:
        """Only meaningful while the side is a single device."""
        if len(self.processes) != 1:
            return False
        return self.processes[0].retarget(device, timeout=timeout)

    # -- lifecycle ------------------------------------------------------

    def stop(self, timeout: float = 1.0) -> None:
        for process in self.processes:
            process.stop(timeout=timeout)

    @property
    def alive(self) -> bool:
        return any(process.alive for process in self.processes)

    @property
    def total_blocks(self) -> int:
        return sum(process.total_blocks for process in self.processes)

    @property
    def restarts(self) -> int:
        return sum(process.restarts for process in self.processes)

    @property
    def overruns(self) -> int:
        return sum(process.overruns for process in self.processes)
