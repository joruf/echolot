"""The analysis the verdict rests on - fast, hermetic, no devices involved.

If these are wrong, a green self test means nothing, so they are tested harder
than the code they check: a known signal must read the level it actually has,
silence must read as silence, and a swapped or bleeding channel must fail.
"""

from __future__ import annotations

import math
import wave
from array import array

import pytest

from echolot import selftest
from echolot.selftest import MIC_HZ, OTHER_HZ, SideCheck, channels_of, tone_db, write_sine

RATE = 48000


def sine(hz: float, seconds: float = 0.5, amplitude: float = 0.5) -> array:
    frames = int(RATE * seconds)
    peak = amplitude * (selftest.FULL_SCALE - 1)
    return array(
        "h", (int(peak * math.sin(2 * math.pi * hz * index / RATE)) for index in range(frames))
    )


# -- measuring one frequency --------------------------------------------


def test_a_known_tone_reads_its_own_level():
    """Half of full scale is -6 dBFS; the filter has to say so."""
    measured = tone_db(sine(MIC_HZ, amplitude=0.5), MIC_HZ, RATE)
    assert measured == pytest.approx(-6.0, abs=1.0)


def test_full_scale_reads_zero():
    measured = tone_db(sine(MIC_HZ, amplitude=1.0), MIC_HZ, RATE)
    assert measured == pytest.approx(0.0, abs=1.0)


def test_another_frequency_is_not_mistaken_for_this_one():
    """The whole verdict depends on telling the two tones apart."""
    samples = sine(MIC_HZ)
    assert tone_db(samples, MIC_HZ, RATE) - tone_db(samples, OTHER_HZ, RATE) > 40


def test_silence_reads_as_nothing():
    assert tone_db(array("h", [0] * RATE), MIC_HZ, RATE) == -math.inf
    assert tone_db(array("h"), MIC_HZ, RATE) == -math.inf


def test_both_tones_at_once_are_both_found():
    """The mix layout case: one channel, both sides in it."""
    first, second = sine(MIC_HZ, amplitude=0.4), sine(OTHER_HZ, amplitude=0.4)
    mixed = array("h", (a + b for a, b in zip(first, second)))
    assert tone_db(mixed, MIC_HZ, RATE) > -12
    assert tone_db(mixed, OTHER_HZ, RATE) > -12


# -- generating and reading files ---------------------------------------


def test_a_written_sine_reads_back_as_that_sine(tmp_path):
    path = write_sine(tmp_path / "tone.wav", MIC_HZ, 0.5)
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == RATE
    left, right, rate = channels_of(path)
    assert not right  # mono
    assert tone_db(left, MIC_HZ, rate) == pytest.approx(-6.0, abs=1.0)


def test_stereo_channels_are_kept_apart(tmp_path):
    left, right = sine(MIC_HZ), sine(OTHER_HZ)
    interleaved = array("h")
    for first, second in zip(left, right):
        interleaved.append(first)
        interleaved.append(second)
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(interleaved.tobytes())

    got_left, got_right, rate = channels_of(path)
    # Each channel must carry only its own tone, or the analysis proves nothing.
    assert tone_db(got_left, MIC_HZ, rate) > -12 and tone_db(got_left, OTHER_HZ, rate) < -40
    assert tone_db(got_right, OTHER_HZ, rate) > -12 and tone_db(got_right, MIC_HZ, rate) < -40


# -- the verdict --------------------------------------------------------


def check(own: float, other: float, mixed: bool = False) -> SideCheck:
    return SideCheck(side="mic", own_db=own, other_db=other, speech_seconds=1.0, mixed=mixed)


def test_a_clean_channel_passes():
    entry = check(own=-6.0, other=-80.0)
    assert entry.heard and entry.clean and entry.reason() == ""


def test_a_silent_side_fails():
    entry = check(own=-math.inf, other=-80.0)
    assert not entry.heard and not entry.clean
    assert "nothing arrived" in entry.reason()


def test_a_side_below_the_floor_fails():
    entry = check(own=selftest.MIN_LEVEL_DB - 1, other=-90.0)
    assert not entry.clean


def test_bleed_between_the_sides_fails():
    """Both tones equally loud on one channel: the sides are not separated."""
    entry = check(own=-6.0, other=-7.0)
    assert entry.heard
    assert not entry.clean
    assert "bleeds" in entry.reason()


def test_a_mixed_channel_only_has_to_carry_the_side():
    """In the mix layout both tones share a channel by design."""
    entry = check(own=-6.0, other=-6.0, mixed=True)
    assert entry.clean and entry.reason() == ""


def test_a_mixed_channel_still_fails_when_the_side_is_absent():
    assert not check(own=-math.inf, other=-6.0, mixed=True).clean


def test_the_separation_is_reported_as_a_number():
    assert check(own=-6.0, other=-84.0).separation_db == pytest.approx(78.0)


# -- reading the session log --------------------------------------------


def test_speech_seconds_come_from_the_session_log(tmp_path):
    log = tmp_path / "session.log"
    log.write_text(
        '{"type": "session", "version": "9.9.9"}\n'
        '{"type": "speech", "src": "mic"}\n'
        '{"type": "session_end", "speech_seconds": {"mic": 3.5, "speaker": 2.25}}\n',
        encoding="utf-8",
    )
    assert selftest._speech_seconds_from_log(log) == {"mic": 3.5, "speaker": 2.25}


def test_a_log_without_an_end_line_is_not_a_crash(tmp_path):
    log = tmp_path / "session.log"
    log.write_text('{"type": "session"}\nnot json at all\n', encoding="utf-8")
    assert selftest._speech_seconds_from_log(log) == {}
    assert selftest._speech_seconds_from_log(tmp_path / "gone.log") == {}


# -- guards -------------------------------------------------------------


def test_missing_tools_are_named(monkeypatch):
    monkeypatch.setattr(selftest.shutil, "which", lambda name: None)
    assert set(selftest.missing_tools()) == set(selftest.TOOLS)


def test_the_test_refuses_to_run_without_its_tools(monkeypatch):
    """Better a clear refusal than a failure that looks like a broken recorder."""
    monkeypatch.setattr(selftest, "missing_tools", lambda: ["parec"])
    result = selftest.run_pipeline_test(seconds=0.1)
    assert result.ok is False
    assert any("parec" in problem for problem in result.problems)


def test_no_sound_server_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(selftest, "missing_tools", lambda: [])
    monkeypatch.setattr(selftest, "sound_server_available", lambda: False)
    result = selftest.run_pipeline_test(seconds=0.1)
    assert result.ok is False
    assert any("sound server" in problem for problem in result.problems)
