"""Voice activity detection and the JSON Lines log a transcript is built from."""

from __future__ import annotations

import json
import math

from echolot.audio.mixer import ChannelMetrics
from echolot.speechlog import MIC, SPEAKER, ChannelVad, NoiseFloor, SpeechLog

BLOCK_MS = 20
BLOCK_SECONDS = BLOCK_MS / 1000


def metrics(level_db: float, present: bool = True) -> ChannelMetrics:
    """A block at a given loudness."""
    if level_db <= -120:
        return ChannelMetrics(0, 0.0, present)
    amplitude = 32768 * math.pow(10, level_db / 20)
    return ChannelMetrics(int(amplitude), amplitude, present)


def feed(vad: ChannelVad, pattern: list[tuple[float, int]]):
    """Feed (level_db, block_count) pairs and collect closed segments."""
    segments = []
    t = 0.0
    for level_db, blocks in pattern:
        for _ in range(blocks):
            segment = vad.feed(t, metrics(level_db))
            if segment is not None:
                segments.append(segment)
            t += BLOCK_SECONDS
    return segments, t


def make_vad(**kwargs) -> ChannelVad:
    options = dict(
        threshold_db=-45.0, min_segment_ms=250, hangover_ms=400, adaptive=False, block_ms=BLOCK_MS
    )
    options.update(kwargs)
    return ChannelVad(MIC, **options)


def test_one_utterance_is_measured_correctly():
    vad = make_vad()
    segments, _ = feed(vad, [(-70, 10), (-20, 50), (-70, 30)])
    assert len(segments) == 1
    assert abs(segments[0].start - 0.2) < 1e-6
    assert abs(segments[0].duration - 1.0) < 1e-6
    assert segments[0].src == MIC


def test_short_blip_is_ignored():
    vad = make_vad(min_segment_ms=250)
    segments, _ = feed(vad, [(-70, 5), (-20, 5), (-70, 30)])  # 100 ms of noise
    assert segments == []


def test_short_pause_inside_a_sentence_is_bridged():
    vad = make_vad(hangover_ms=400)
    segments, _ = feed(vad, [(-20, 30), (-70, 10), (-20, 30), (-70, 40)])
    assert len(segments) == 1
    assert segments[0].duration > 1.3


def test_long_pause_splits_utterances():
    vad = make_vad(hangover_ms=400)
    segments, _ = feed(vad, [(-20, 30), (-70, 40), (-20, 30), (-70, 40)])
    assert len(segments) == 2


def test_open_segment_is_closed_on_flush():
    vad = make_vad()
    feed(vad, [(-20, 40)])
    segment = vad.flush()
    assert segment is not None
    assert abs(segment.duration - 0.8) < 1e-6
    assert vad.flush() is None


def test_filled_in_blocks_count_as_silence_and_do_not_train_the_floor():
    """Blocks invented during an outage carry no information."""
    vad = make_vad(adaptive=True)
    floor_before = vad.floor_db
    for index in range(50):
        assert vad.feed(index * BLOCK_SECONDS, metrics(-10, present=False)) is None
    assert vad.floor_db == floor_before
    assert vad.in_speech is False


def test_adaptive_threshold_follows_a_noisy_room():
    """Constant background noise must not be logged as speech."""
    vad = make_vad(threshold_db=-60.0, adaptive=True)
    segments, _ = feed(vad, [(-40, 200)])
    assert segments == []
    assert vad.effective_threshold_db() > -40


def test_speech_above_a_noisy_floor_is_still_detected():
    vad = make_vad(threshold_db=-60.0, adaptive=True)
    segments, _ = feed(vad, [(-40, 100), (-15, 50), (-40, 60)])
    assert len(segments) == 1
    assert abs(segments[0].duration - 1.0) < 0.1


def test_noise_floor_is_the_minimum_of_the_recent_window():
    floor = NoiseFloor(BLOCK_SECONDS, window_seconds=1.0, buckets=4)
    for _ in range(10):
        floor.update(-30.0)
    assert floor.value == -30.0

    floor.update(-55.0)  # one quiet block pulls the floor down at once
    assert floor.value == -55.0

    for _ in range(60):  # after the window has passed, it is forgotten again
        floor.update(-30.0)
    assert floor.value == -30.0


def test_long_speech_with_word_gaps_stays_one_utterance():
    """The window minimum must survive normal speech: gaps between words are enough."""
    vad = make_vad(threshold_db=-60.0, hangover_ms=400, adaptive=True)
    pattern = [(-70, 20)]
    for _ in range(10):  # 10 x (word 0.8 s + gap 0.1 s) = 9 s of talking
        pattern.append((-18, 40))
        pattern.append((-70, 5))
    pattern.append((-70, 40))

    segments, _ = feed(vad, pattern)
    assert len(segments) == 1
    assert segments[0].duration > 8.0


def test_log_is_valid_json_lines_with_header_and_totals(tmp_path):
    path = tmp_path / "Echolot_2026-08-12_10-15-03.log"
    log = SpeechLog(path, threshold_db=-45.0, adaptive=False, block_ms=BLOCK_MS)
    log.open({"app": "Echolot", "audio": "Echolot_2026-08-12_10-15-03.opus"})

    t = 0.0
    for level_db, blocks, side in ((-20, 40, MIC), (-70, 40, MIC)):
        for _ in range(blocks):
            log.feed(t, metrics(level_db), metrics(-90))
            t += BLOCK_SECONDS

    log.event("device_change", t, **{"from": "a", "to": "b"})
    log.close(duration=round(t, 2))

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = [entry["type"] for entry in entries]
    assert kinds[0] == "session"
    assert kinds[-1] == "session_end"
    assert "speech" in kinds and "device_change" in kinds

    speech = next(entry for entry in entries if entry["type"] == "speech")
    assert speech["src"] == MIC
    assert speech["end"] > speech["start"]
    assert set(speech) >= {"src", "start", "end", "duration", "peak_db"}

    end = entries[-1]
    assert end["speech_seconds"][MIC] > 0.7
    assert end["speech_seconds"][SPEAKER] == 0.0
    assert end["duration"] == round(t, 2)


def test_both_channels_are_tracked_separately(tmp_path):
    path = tmp_path / "beide.log"
    log = SpeechLog(path, adaptive=False, block_ms=BLOCK_MS)
    log.open({})
    t = 0.0
    for _ in range(40):  # both talking at the same time
        log.feed(t, metrics(-20), metrics(-25))
        t += BLOCK_SECONDS
    for _ in range(40):
        log.feed(t, metrics(-90), metrics(-90))
        t += BLOCK_SECONDS
    log.close()

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    speech = [entry for entry in entries if entry["type"] == "speech"]
    assert {entry["src"] for entry in speech} == {MIC, SPEAKER}
    assert log.speech_seconds[MIC] > 0 and log.speech_seconds[SPEAKER] > 0


def test_events_before_open_do_not_raise(tmp_path):
    log = SpeechLog(tmp_path / "zu.log", block_ms=BLOCK_MS)
    log.event("source_error", 1.0, side=MIC)  # no open() yet
    assert log.lines_written == 0
