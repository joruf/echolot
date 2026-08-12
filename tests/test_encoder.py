"""Encoder command line - this is where channel separation is decided."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from echolot.audio.encoder import Encoder, probe_available

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_two_channel_opus_encodes_them_discretely(tmp_path):
    """Without mapping_family 255 stereo coupling would bleed one side into the other."""
    command = Encoder(
        tmp_path / "a.opus", audio_format="opus", bitrate_kbps=64, channels=2
    ).command()
    assert "libopus" in command
    assert command[command.index("-mapping_family") + 1] == "255"
    assert command[command.index("-b:a") + 1] == "64k"
    assert command[-1] == str(tmp_path / "a.opus")


def test_mono_opus_leaves_the_channel_mapping_alone(tmp_path):
    """Discrete mapping is meaningless for a single channel."""
    command = Encoder(tmp_path / "a.opus", audio_format="opus", channels=1).command()
    assert "libopus" in command
    assert "-mapping_family" not in command
    assert command[command.index("-ac") + 1] == "1"


def test_input_is_raw_pcm_with_the_requested_channel_count(tmp_path):
    command = Encoder(tmp_path / "a.opus", sample_rate=48000, channels=2).command()
    assert command[command.index("-f") + 1] == "s16le"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-i") + 1] == "pipe:0"


def test_packets_are_flushed_so_a_crash_leaves_a_playable_file(tmp_path):
    assert "-flush_packets" in Encoder(tmp_path / "a.opus").command()


def test_lossless_and_uncompressed_formats(tmp_path):
    assert "flac" in Encoder(tmp_path / "a.flac", audio_format="flac").command()
    assert "pcm_s16le" in Encoder(tmp_path / "a.wav", audio_format="wav").command()


def test_write_before_start_is_refused(tmp_path):
    assert Encoder(tmp_path / "a.opus").write(b"\x00\x00") is False
    assert Encoder(tmp_path / "a.opus").close() is None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg fehlt")
def test_probe_available_returns_a_version():
    assert "ffmpeg" in (probe_available() or "").lower()


def probe_channels(path) -> str:
    return subprocess.run(
        [
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-show_entries", "stream=channels,codec_name",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg fehlt")
def test_real_two_channel_opus_file(tmp_path):
    """End to end through ffmpeg: two channels in, two channels on disk."""
    from array import array

    target = tmp_path / "Echolot_split.opus"
    encoder = Encoder(target, audio_format="opus", bitrate_kbps=64, channels=2)
    encoder.start()

    frames = 48000 // 5  # 200 ms
    pcm = array("h")
    for index in range(frames):
        pcm.append(8000 if index % 100 < 50 else -8000)  # left: square wave
        pcm.append(0)  # right: silence
    assert encoder.write(pcm.tobytes()) is True
    assert encoder.close() == 0

    output = probe_channels(target)
    assert "opus" in output
    assert "2" in output.split()
    assert target.stat().st_size > 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg fehlt")
def test_real_mono_opus_file(tmp_path):
    """The default layout: one mixed track."""
    from array import array

    target = tmp_path / "Echolot_mix.opus"
    encoder = Encoder(target, audio_format="opus", bitrate_kbps=64, channels=1)
    encoder.start()

    pcm = array("h", [8000 if index % 100 < 50 else -8000 for index in range(48000 // 5)])
    assert encoder.write(pcm.tobytes()) is True
    assert encoder.close() == 0

    output = probe_channels(target)
    assert "opus" in output
    assert "1" in output.split()
    assert target.stat().st_size > 0
