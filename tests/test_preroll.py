"""The pre-roll buffer: keeping the last minutes, and handing them over intact."""

from __future__ import annotations

import time
from array import array

import pytest

from echolot.audio import capture
from echolot.audio.mixer import LAYOUT_SPLIT, ChannelMetrics, Mixer
from echolot.audio.preroll import MAX_MINUTES, Handover, Preroll, PrerollRing
from echolot.speechlog import MIC, SPEAKER

BLOCK_FRAMES = 4
HAS_SOUND = capture.probe_available()


def block(value: int) -> bytes:
    return array("h", [value] * BLOCK_FRAMES).tobytes()


def metrics(peak: int = 1000, present: bool = True) -> ChannelMetrics:
    return ChannelMetrics(peak, float(peak), present)


def fill(ring: PrerollRing, count: int, first: int = 0) -> None:
    """Write `count` blocks the way the mixer does: payload, then its metrics."""
    for index in range(count):
        ring.write(block(first + index))
        ring.feed(index * 0.02, metrics(), metrics())


# -- the ring -----------------------------------------------------------


def test_ring_pairs_a_payload_with_its_metrics():
    ring = PrerollRing(max_blocks=10)
    ring.write(block(7))
    ring.feed(0.0, metrics(peak=111), metrics(peak=222))

    assert len(ring) == 1
    payload, mic_metrics, speaker_metrics = ring.entries[0]
    assert payload == block(7)
    assert (mic_metrics.peak, speaker_metrics.peak) == (111, 222)


def test_ring_ignores_metrics_without_a_payload():
    """Defends the pairing contract: metrics alone would misalign everything."""
    ring = PrerollRing(max_blocks=10)
    ring.feed(0.0, metrics(), metrics())
    assert len(ring) == 0


def test_ring_keeps_only_the_newest_blocks():
    ring = PrerollRing(max_blocks=5)
    fill(ring, 12)

    assert len(ring) == 5
    assert ring.is_full is True
    # The five most recent ones, in order.
    assert [entry[0] for entry in ring.entries] == [block(value) for value in range(7, 12)]


def test_ring_is_not_full_before_the_window_is_reached():
    ring = PrerollRing(max_blocks=100)
    fill(ring, 30)
    assert ring.is_full is False
    assert len(ring) == 30


def test_take_hands_out_the_blocks_and_forgets_them():
    ring = PrerollRing(max_blocks=10)
    fill(ring, 4)
    taken = ring.take()

    assert len(taken) == 4
    assert len(ring) == 0
    assert ring.take() == []


def test_ring_reports_its_size_in_bytes():
    ring = PrerollRing(max_blocks=10)
    fill(ring, 3)
    assert ring.bytes_written == 3 * BLOCK_FRAMES * 2


# -- settings -----------------------------------------------------------


def test_off_by_default(config):
    preroll = Preroll(config)
    assert preroll.minutes == 0
    assert preroll.active is False
    assert preroll.memory_bytes() == 0
    assert preroll.seconds_buffered == 0.0


def test_minutes_are_clamped_to_the_offered_range(config):
    preroll = Preroll(config)
    for value, expected in ((3, 3), (5, 5), (99, MAX_MINUTES), (-2, 0), ("kaputt", 0)):
        config.set("audio.preroll_minutes", value)
        assert preroll.minutes == expected


def test_memory_estimate_scales_with_minutes_and_layout(config):
    preroll = Preroll(config)
    config.set("audio.preroll_minutes", 1)
    mixed = preroll.memory_bytes()
    assert 5_000_000 < mixed < 6_000_000  # ~5.5 MB per minute

    config.set("audio.preroll_minutes", 5)
    assert preroll.memory_bytes() == 5 * mixed

    config.set("audio.layout", LAYOUT_SPLIT)
    assert preroll.memory_bytes() == 10 * mixed  # two channels


def test_signature_changes_when_the_buffer_would_become_unusable(config):
    preroll = Preroll(config)
    config.set("audio.preroll_minutes", 2)
    base = preroll.signature()

    for key, value in (
        ("audio.preroll_minutes", 3),
        ("audio.layout", LAYOUT_SPLIT),
        ("audio.sample_rate", 24000),
        ("audio.block_ms", 40),
        ("devices.mic", "alsa_input.anderes"),
        ("devices.speaker", "alsa_output.anderes.monitor"),
        ("devices.follow_default", False),
    ):
        previous = config.get(key)
        config.set(key, value)
        assert preroll.signature() != base, key
        config.set(key, previous)
    assert preroll.signature() == base


def test_ensure_does_nothing_while_switched_off(config):
    preroll = Preroll(config)
    preroll.ensure()
    assert preroll.active is False


def test_hand_over_without_a_buffer_returns_nothing(config):
    assert Preroll(config).hand_over() is None


# -- handover -----------------------------------------------------------


def test_handover_reports_its_length():
    entries = [(block(index), metrics(), metrics()) for index in range(150)]
    handover = Handover(entries=entries, block_seconds=0.02)
    assert handover.blocks == 150
    assert handover.seconds == pytest.approx(3.0)


def test_mixer_continues_the_timeline_after_a_pre_roll():
    """Log timestamps have to keep matching the audio, pre-roll included."""

    class Sink:
        def __init__(self):
            self.writes = 0

        def write(self, payload):
            self.writes += 1
            return True

    seen: list[float] = []
    mixer = Mixer(
        mic=None,
        speaker=None,
        encoder=Sink(),
        sample_rate=48000,
        block_frames=BLOCK_FRAMES,
        initial_blocks=100,
        on_metrics=lambda t, mic, spk: seen.append(t),
    )
    assert mixer.blocks_written == 100
    assert mixer.seconds_written == pytest.approx(100 * BLOCK_FRAMES / 48000)

    mixer._write(block(1), block(1))
    # The first live block is timed after the buffered ones, not at zero.
    assert seen == [pytest.approx(100 * BLOCK_FRAMES / 48000)]


# -- against the real sound server --------------------------------------


@pytest.mark.skipif(not HAS_SOUND, reason="parec/Soundserver nicht verfügbar")
def test_buffer_fills_up_and_can_be_handed_over(config):
    """Really captures while idle, and hands the audio over alive."""
    config.set("audio.preroll_minutes", 1)
    preroll = Preroll(config)
    assert preroll.start() is True
    try:
        deadline = time.monotonic() + 6
        while preroll.seconds_buffered < 0.5 and time.monotonic() < deadline:
            time.sleep(0.1)

        assert preroll.active is True
        assert preroll.seconds_buffered >= 0.5
        assert preroll.ring is not None and preroll.ring.is_full is False

        handover = preroll.hand_over()
        assert handover is not None
        assert handover.blocks >= 25  # at least half a second of 20 ms blocks
        assert handover.seconds >= 0.5
        # The capture processes are handed over still running - restarting them
        # would tear a hole exactly at the moment of the double click.
        assert handover.captures
        assert all(process.alive for process in handover.captures.values())
        assert preroll.active is False
        assert set(handover.captures) <= {MIC, SPEAKER}
    finally:
        for process in (preroll.hand_over() or Handover(entries=[])).captures.values():
            process.stop()
        preroll.stop()


@pytest.mark.skipif(not HAS_SOUND, reason="parec/Soundserver nicht verfügbar")
def test_ensure_rebuilds_the_buffer_when_the_layout_changes(config):
    config.set("audio.preroll_minutes", 1)
    preroll = Preroll(config)
    preroll.ensure()
    try:
        assert preroll.active is True
        first_ring = preroll.ring

        config.set("audio.layout", LAYOUT_SPLIT)
        preroll.ensure()
        assert preroll.active is True
        assert preroll.ring is not first_ring  # rebuilt, not reused

        config.set("audio.preroll_minutes", 0)
        preroll.ensure()
        assert preroll.active is False
    finally:
        preroll.stop()
