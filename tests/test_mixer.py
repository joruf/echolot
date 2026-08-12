"""Both layouts, and the promise that no delivered audio is ever discarded."""

from __future__ import annotations

from array import array

import pytest

from echolot.audio.mixer import (
    BACKLOG_TARGET_BLOCKS,
    LAYOUT_MIX,
    LAYOUT_SPLIT,
    MAX_SAMPLE,
    MIN_SAMPLE,
    ChannelMetrics,
    Mixer,
    amplitude_to_db,
    channels_for,
    measure,
)

BLOCK_FRAMES = 4  # tiny blocks keep the assertions readable


def block(value: int, frames: int = BLOCK_FRAMES) -> bytes:
    return array("h", [value] * frames).tobytes()


class FakeSource:
    """Stands in for a CaptureProcess."""

    def __init__(self, blocks: list[bytes] | None = None) -> None:
        self.blocks = list(blocks or [])
        self.drained = 0

    def read(self, timeout=None):
        return self.blocks.pop(0) if self.blocks else None

    def pending(self) -> int:
        return len(self.blocks)

    def drain(self) -> None:
        self.drained += 1
        self.blocks.clear()


class FakeEncoder:
    def __init__(self, fail_after: int | None = None) -> None:
        self.writes: list[bytes] = []
        self.fail_after = fail_after

    def write(self, data: bytes) -> bool:
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            return False
        self.writes.append(data)
        return True


def make_mixer(mic=None, speaker=None, encoder=None, **kwargs) -> Mixer:
    return Mixer(
        mic=mic,
        speaker=speaker,
        encoder=encoder or FakeEncoder(),
        sample_rate=48000,
        block_frames=BLOCK_FRAMES,
        **kwargs,
    )


def samples_of(data: bytes) -> list[int]:
    out = array("h")
    out.frombytes(data)
    return list(out)


def test_channel_count_per_layout():
    assert channels_for(LAYOUT_MIX) == 1
    assert channels_for(LAYOUT_SPLIT) == 2
    assert channels_for("unfug") == 1  # a broken setting must not produce 0 channels
    assert make_mixer(layout="unfug").layout == LAYOUT_MIX


def test_split_puts_the_microphone_left_and_the_other_side_right():
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_SPLIT)
    mixer._write(block(1000), block(-2000))

    written = samples_of(encoder.writes[0])
    assert len(written) == BLOCK_FRAMES * 2
    assert written[0::2] == [1000] * BLOCK_FRAMES  # channel 0 = mic
    assert written[1::2] == [-2000] * BLOCK_FRAMES  # channel 1 = speaker


def test_mix_sums_both_sides_into_one_mono_track():
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_MIX)
    mixer._write(block(1000), block(-400))

    written = samples_of(encoder.writes[0])
    assert len(written) == BLOCK_FRAMES  # mono: one sample per frame
    assert written == [600] * BLOCK_FRAMES


def test_mix_passes_a_lone_side_through_untouched():
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_MIX)
    mixer._write(block(700), None)  # only we are talking
    mixer._write(None, block(-900))  # only the other side is

    assert samples_of(encoder.writes[0]) == [700] * BLOCK_FRAMES
    assert samples_of(encoder.writes[1]) == [-900] * BLOCK_FRAMES


def test_mix_limits_loud_moments_instead_of_wrapping_around():
    """Both talking loudly at once must not turn into an overflow crackle."""
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_MIX)
    mixer._write(block(30000), block(20000))
    mixer._write(block(-30000), block(-20000))

    assert samples_of(encoder.writes[0]) == [MAX_SAMPLE] * BLOCK_FRAMES
    assert samples_of(encoder.writes[1]) == [MIN_SAMPLE] * BLOCK_FRAMES
    assert mixer.clipped_blocks == 2


def test_normal_levels_are_never_limited():
    mixer = make_mixer(layout=LAYOUT_MIX)
    mixer._write(block(8000), block(8000))
    assert mixer.clipped_blocks == 0


@pytest.mark.parametrize("layout", [LAYOUT_MIX, LAYOUT_SPLIT])
def test_missing_side_is_counted_in_both_layouts(layout):
    mixer = make_mixer(layout=layout)
    mixer._write(block(500), None)
    assert mixer.speaker_gap_blocks == 1
    assert mixer.mic_gap_blocks == 0
    assert mixer.last_speaker.present is False
    assert mixer.last_mic.present is True


def test_silence_for_a_missing_side_keeps_the_split_channels_aligned():
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_SPLIT)
    mixer._write(block(500), None)
    written = samples_of(encoder.writes[0])
    assert written[0::2] == [500] * BLOCK_FRAMES
    assert written[1::2] == [0] * BLOCK_FRAMES


def test_short_block_is_padded_not_rejected():
    encoder = FakeEncoder()
    mixer = make_mixer(encoder=encoder, layout=LAYOUT_SPLIT)
    mixer._write(array("h", [7, 7]).tobytes(), None)
    written = samples_of(encoder.writes[0])
    assert written[0::2] == [7, 7, 0, 0]


@pytest.mark.parametrize("layout,channel_index", [(LAYOUT_MIX, 0), (LAYOUT_SPLIT, 1)])
def test_backlog_is_written_out_in_order_without_dropping(layout, channel_index):
    """A microphone outage must not cost the other side's audio, in either layout."""
    speaker = FakeSource([block(index + 1) for index in range(12)])
    mic = FakeSource([])
    encoder = FakeEncoder()
    mixer = make_mixer(mic=mic, speaker=speaker, encoder=encoder, layout=layout)

    mixer._drain_backlog()

    written_speaker = [samples_of(data)[channel_index] for data in encoder.writes]
    assert written_speaker == [index + 1 for index in range(12 - BACKLOG_TARGET_BLOCKS)]
    assert speaker.pending() == BACKLOG_TARGET_BLOCKS
    assert mixer.mic_gap_blocks == len(encoder.writes)  # mic side was silent
    assert mixer.speaker_gap_blocks == 0  # nothing lost on the speaker side


def test_drain_stops_when_both_sides_are_calm():
    mixer = make_mixer(mic=FakeSource([block(1)]), speaker=FakeSource([block(2)]))
    mixer._drain_backlog()
    assert mixer.blocks_written == 0


def test_encoder_failure_stops_the_mixer_and_reports_once():
    errors: list[str] = []
    encoder = FakeEncoder(fail_after=1)
    mixer = make_mixer(encoder=encoder, on_error=errors.append)

    mixer._write(block(1), block(1))
    mixer._write(block(2), block(2))

    assert mixer.blocks_written == 1
    assert mixer.encoder_failed is True
    assert len(errors) == 1


def test_metrics_are_reported_per_block():
    seen: list[tuple[float, ChannelMetrics, ChannelMetrics]] = []
    mixer = make_mixer(on_metrics=lambda t, mic, spk: seen.append((t, mic, spk)))
    mixer._write(block(16384), None)
    mixer._write(block(0), block(32767))

    assert [entry[0] for entry in seen] == [0.0, BLOCK_FRAMES / 48000]
    assert seen[0][1].peak == 16384
    assert seen[0][2].present is False
    assert seen[1][2].peak == 32767


def test_pause_keeps_the_timeline_and_empties_the_buffers():
    mic, speaker = FakeSource([block(1)]), FakeSource([block(2)])
    mixer = make_mixer(mic=mic, speaker=speaker)
    mixer.pause()
    assert mixer.paused is True
    mixer._idle_while_paused()
    assert mic.drained == 1 and speaker.drained == 1
    assert mixer.blocks_written == 0
    mixer.resume()
    assert mixer.paused is False


def test_measure_returns_peak_and_mean_absolute():
    peak, mean_abs = measure(array("h", [100, -300, 200, -200]))
    assert peak == 300
    assert mean_abs == 200.0
    assert measure(array("h", [])) == (0, 0.0)


def test_decibel_scale():
    assert amplitude_to_db(32768) == 0.0
    assert amplitude_to_db(0) == -120.0
    assert -6.1 < amplitude_to_db(16384) < -6.0


def test_seconds_written_tracks_blocks():
    mixer = make_mixer()
    for _ in range(50):
        mixer._write(block(1), block(1))
    assert mixer.blocks_written == 50
    assert abs(mixer.seconds_written - 50 * BLOCK_FRAMES / 48000) < 1e-9
