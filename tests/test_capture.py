"""Capture buffering and the parec command line."""

from __future__ import annotations

from array import array

from echolot.audio.capture import BACKOFF_SECONDS, CaptureProcess, _Stream

BLOCK_FRAMES = 480


def block(value: int) -> bytes:
    return array("h", [value] * BLOCK_FRAMES).tobytes()


def make_capture(**kwargs) -> CaptureProcess:
    options = dict(side="mic", sample_rate=48000, block_frames=BLOCK_FRAMES, buffer_seconds=0.1)
    options.update(kwargs)
    return CaptureProcess("alsa_input.test", **options)


def test_blocks_come_back_in_order():
    process = make_capture()
    for value in (1, 2, 3):
        process._push(block(value))
    assert process.pending() == 3
    assert process.read() == block(1)
    assert process.read() == block(2)
    assert process.total_blocks == 3


def test_empty_buffer_returns_none_without_blocking():
    assert make_capture().read(timeout=0) is None


def test_overflow_drops_the_oldest_and_is_counted():
    """Only reachable if the mixer stalls; staying near real time beats piling up."""
    process = make_capture(buffer_seconds=0.05)  # 5 blocks of 10 ms
    for value in range(10):
        process._push(block(value))
    assert process.overruns > 0
    assert process.pending() == process._capacity
    assert process.read() != block(0)  # the oldest is gone


def test_drain_empties_the_buffer():
    process = make_capture()
    process._push(block(1))
    process.drain()
    assert process.pending() == 0


def test_events_carry_the_side():
    events = []
    process = make_capture(on_event=lambda event, fields: events.append((event, fields)))
    process._emit("source_error", device="x")
    assert events == [("source_error", {"side": "mic", "device": "x"})]


def test_backoff_is_bounded():
    """A device that never comes back must not spin the CPU."""
    assert BACKOFF_SECONDS[-1] <= 5.0
    assert BACKOFF_SECONDS[0] < BACKOFF_SECONDS[-1]
