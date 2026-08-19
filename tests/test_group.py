"""A side made of several devices: summed, never dropped, never blocking."""

from __future__ import annotations

from array import array

from echolot.audio.group import SourceGroup
from echolot.audio.mixer import MAX_SAMPLE, MIN_SAMPLE

FRAMES = 4


def block(value: int, frames: int = FRAMES) -> bytes:
    return array("h", [value] * frames).tobytes()


def samples_of(data: bytes) -> list[int]:
    out = array("h")
    out.frombytes(data)
    return list(out)


class FakeProcess:
    """Stands in for a CaptureProcess member."""

    def __init__(self, device: str, blocks: list[bytes] | None = None) -> None:
        self.device = device
        self.blocks = list(blocks or [])
        self.on_event = None
        self.drained = 0
        self.stopped = False
        self.total_blocks = 0
        self.restarts = 0
        self.overruns = 0
        self.first_block_waits: list[float] = []
        self.alive = True

    def read(self, timeout=None):
        return self.blocks.pop(0) if self.blocks else None

    def pending(self) -> int:
        return len(self.blocks)

    def drain(self) -> None:
        self.drained += 1
        self.blocks.clear()

    def stop(self, timeout: float = 1.0) -> None:
        self.stopped = True

    def wait_for_first_block(self, timeout: float = 3.0) -> bool:
        self.first_block_waits.append(timeout)
        return bool(self.blocks)

    def retarget(self, device: str, timeout: float = 2.0) -> bool:
        self.device = device
        return True


def group_of(*processes: FakeProcess) -> SourceGroup:
    return SourceGroup(list(processes), FRAMES)


# -- reading ------------------------------------------------------------


def test_a_single_device_is_passed_through_unchanged():
    """The common case must not pay for the feature."""
    only = FakeProcess("mic", [block(1234)])
    assert group_of(only).read() == block(1234)


def test_several_devices_are_summed():
    group = group_of(FakeProcess("a", [block(1000)]), FakeProcess("b", [block(-400)]))
    assert samples_of(group.read()) == [600] * FRAMES


def test_a_device_with_nothing_to_say_contributes_nothing():
    """An idle device delivers digital silence, which is why this is cheap."""
    group = group_of(FakeProcess("a", [block(700)]), FakeProcess("b", []))
    assert samples_of(group.read()) == [700] * FRAMES


def test_the_leading_device_being_empty_does_not_lose_the_others():
    group = group_of(FakeProcess("a", []), FakeProcess("b", [block(500)]))
    assert samples_of(group.read()) == [500] * FRAMES


def test_nothing_anywhere_reads_as_nothing():
    assert group_of(FakeProcess("a"), FakeProcess("b")).read() is None
    assert SourceGroup([], FRAMES).read() is None


def test_loud_devices_at_once_are_limited_and_counted():
    group = group_of(FakeProcess("a", [block(30000)]), FakeProcess("b", [block(20000)]))
    assert samples_of(group.read()) == [MAX_SAMPLE] * FRAMES
    assert group.clipped_blocks == 1

    group = group_of(FakeProcess("a", [block(-30000)]), FakeProcess("b", [block(-20000)]))
    assert samples_of(group.read()) == [MIN_SAMPLE] * FRAMES


def test_a_short_block_is_padded_rather_than_breaking_the_sum():
    group = group_of(
        FakeProcess("a", [array("h", [7, 7]).tobytes()]), FakeProcess("b", [block(1)])
    )
    assert samples_of(group.read()) == [8, 8, 1, 1]


# -- the rest of the interface the mixer and session use ----------------


def test_pending_is_the_busiest_member():
    group = group_of(FakeProcess("a", [block(1)]), FakeProcess("b", [block(1)] * 5))
    assert group.pending() == 5


def test_drain_empties_every_member():
    first, second = FakeProcess("a", [block(1)]), FakeProcess("b", [block(2)])
    group_of(first, second).drain()
    assert (first.drained, second.drained) == (1, 1)
    assert first.blocks == [] and second.blocks == []


def test_stop_stops_every_member():
    first, second = FakeProcess("a"), FakeProcess("b")
    group_of(first, second).stop()
    assert first.stopped and second.stopped


def test_counters_are_added_up():
    first, second = FakeProcess("a"), FakeProcess("b")
    first.total_blocks, second.total_blocks = 100, 40
    first.restarts, second.overruns = 2, 3
    group = group_of(first, second)
    assert group.total_blocks == 140
    assert group.restarts == 2
    assert group.overruns == 3


def test_one_live_device_is_enough_to_count_as_delivering():
    group = group_of(FakeProcess("a"), FakeProcess("b", [block(1)]))
    assert group.wait_for_first_block(timeout=1.0) is True
    assert group_of(FakeProcess("a"), FakeProcess("b")).wait_for_first_block(0.5) is False


def test_a_device_that_appeared_later_joins_the_side():
    """What "record everything" means while a recording is already running."""
    group = group_of(FakeProcess("a", [block(100)]))
    group.add(FakeProcess("b", [block(100)]))

    assert group.devices == ["a", "b"]
    assert len(group) == 2
    assert samples_of(group.read()) == [200] * FRAMES


def test_retarget_only_applies_to_a_single_device_side():
    single = FakeProcess("a")
    group = group_of(single)
    assert group.retarget("b") is True
    assert single.device == "b"

    # With several devices there is nothing to switch over - they are all kept.
    assert group_of(FakeProcess("a"), FakeProcess("b")).retarget("c") is False


def test_the_event_callback_reaches_every_member():
    first, second = FakeProcess("a"), FakeProcess("b")
    group_of(first, second).set_on_event("callback")
    assert first.on_event == "callback" and second.on_event == "callback"
