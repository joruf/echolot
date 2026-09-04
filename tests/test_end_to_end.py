"""The test that actually matters: is a conversation recorded, both sides?

This one records for real - virtual devices, a tone into each, the whole pipeline
from `parec` through the mixer to the encoded file - and then looks inside the
file for each side's own tone on its own channel. Nothing about it is mocked,
which is the point: it fails when recording is broken, not when an interface
changed.

Skipped when there is no sound server or the PulseAudio tools are missing, so a
machine without audio does not report a broken recorder.
"""

from __future__ import annotations

import pytest

from echolot import selftest
from echolot.speechlog import MIC, SPEAKER

pytestmark = pytest.mark.skipif(
    bool(selftest.missing_tools()) or not selftest.sound_server_available(),
    reason="needs a running sound server and the PulseAudio tools",
)

SECONDS = 4.0


@pytest.fixture(scope="module")
def split_recording():
    """One real recording in the split layout, shared by the checks below."""
    return selftest.run_pipeline_test(seconds=SECONDS, layout="split")


@pytest.fixture(scope="module")
def mixed_recording():
    return selftest.run_pipeline_test(seconds=SECONDS, layout="mix")


# -- both sides, on their own channels ----------------------------------


def test_the_recording_passes_as_a_whole(split_recording):
    assert split_recording.ok, split_recording.problems


def test_the_microphone_side_is_recorded(split_recording):
    """Your own voice - the side that was never in doubt, and is now proven."""
    check = split_recording.check(MIC)
    assert check is not None
    assert check.heard, check.reason()
    assert check.own_db > selftest.MIN_LEVEL_DB


def test_the_other_side_is_recorded(split_recording):
    """The side that matters: what the person on the phone said."""
    check = split_recording.check(SPEAKER)
    assert check is not None
    assert check.heard, check.reason()
    assert check.own_db > selftest.MIN_LEVEL_DB


def test_the_sides_are_not_swapped(split_recording):
    """440 Hz went into the microphone, 880 Hz into the other side.

    A swap would leave both channels loud and pass any "is there sound" check;
    only asking for the right frequency in the right channel catches it.
    """
    for side in (MIC, SPEAKER):
        check = split_recording.check(side)
        assert check.own_db > check.other_db + selftest.MIN_SEPARATION_DB, (
            f"{side}: own {check.own_db:.1f} dB vs other {check.other_db:.1f} dB"
        )


def test_neither_side_bleeds_into_the_other(split_recording):
    for side in (MIC, SPEAKER):
        assert split_recording.check(side).clean


def test_speech_is_detected_on_both_sides(split_recording):
    """The log has to agree with the audio, or the speech log lies."""
    for side in (MIC, SPEAKER):
        assert split_recording.check(side).speech_seconds > 0.5


def test_a_file_was_actually_written(split_recording):
    assert split_recording.recording is not None
    assert split_recording.recording.exists()
    assert split_recording.recording.stat().st_size > 1000


# -- the default layout, where both sides share one channel -------------


def test_both_sides_are_in_the_mixed_file(mixed_recording):
    """The default: one mono channel that must still carry both voices."""
    assert mixed_recording.ok, mixed_recording.problems
    for side in (MIC, SPEAKER):
        check = mixed_recording.check(side)
        assert check.mixed is True
        assert check.own_db > selftest.MIN_LEVEL_DB, f"{side} missing from the mix"


# -- the machine is left as it was --------------------------------------


def test_no_virtual_devices_are_left_behind():
    """A test that leaves devices on the machine breaks the next recording."""
    import subprocess

    listing = subprocess.run(
        ["pactl", "list", "short", "modules"], capture_output=True, text=True, check=False
    ).stdout
    assert "echolot_selftest" not in listing


# -- every output format, not just the one that is easy to analyse ------


@pytest.mark.parametrize("audio_format", ["opus", "flac", "wav"])
def test_both_sides_survive_every_format(audio_format):
    """Opus is the default, so testing only WAV would prove the wrong thing.

    The lossy path is decoded back before analysis: what matters is not that
    bytes were written but that both voices can still be got out of the file.
    """
    result = selftest.run_pipeline_test(
        seconds=SECONDS, layout="split", audio_format=audio_format
    )
    assert result.ok, f"{audio_format}: {result.problems}"
    assert result.recording.suffix == f".{audio_format}"
    for side in (MIC, SPEAKER):
        check = result.check(side)
        assert check.clean, f"{audio_format}/{side}: {check.reason()}"
