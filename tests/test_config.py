"""The settings file is hand-editable, so bad values must never break the tray."""

from __future__ import annotations

import json

from echolot import paths
from echolot.config import AUTO, DEFAULTS, Config


def test_defaults_are_complete():
    cfg = Config()
    assert cfg.get("audio.format") == "opus"
    assert cfg.get("devices.mic") == AUTO
    assert cfg.get("tray.blink") is True
    assert cfg.get("vad.hangover_ms") == 400


def test_missing_file_falls_back_to_defaults(tmp_path):
    cfg = Config(path=tmp_path / "nope.json").load()
    assert cfg.load_error is None
    assert cfg.data["audio"]["format"] == "opus"


def test_broken_file_is_reported_but_survives(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{ das ist kein json", encoding="utf-8")
    cfg = Config(path=target).load()
    assert cfg.load_error is not None
    assert cfg.get("audio.format") == "opus"


def test_partial_file_is_merged_with_defaults(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"audio": {"bitrate_kbps": 96}}), encoding="utf-8")
    cfg = Config(path=target).load()
    assert cfg.get("audio.bitrate_kbps") == 96
    assert cfg.get("audio.format") == "opus"
    assert cfg.get("disk.stop_mb") == DEFAULTS["disk"]["stop_mb"]


def test_older_file_is_flagged_for_migration(tmp_path):
    """A file written by an earlier version lacks the settings added since."""
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"audio": {"format": "opus"}}), encoding="utf-8")
    cfg = Config(path=target).load()
    assert cfg.needs_migration is True
    assert cfg.audio_layout == "mix"  # the new setting still has its default

    cfg.save()
    again = Config(path=target).load()
    assert again.needs_migration is False
    assert json.loads(target.read_text(encoding="utf-8"))["audio"]["layout"] == "mix"


def test_complete_file_needs_no_migration(tmp_path):
    target = tmp_path / "settings.json"
    Config(path=target).save()
    assert Config(path=target).load().needs_migration is False


def test_save_and_reload_roundtrip(tmp_path):
    target = tmp_path / "settings.json"
    cfg = Config(path=target)
    cfg.set("tray.blink_interval_ms", 900)
    cfg.set("devices.mic", "alsa_input.test")
    cfg.save()

    again = Config(path=target).load()
    assert again.get("tray.blink_interval_ms") == 900
    assert again.get("devices.mic") == "alsa_input.test"


def test_validate_clamps_and_coerces(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("audio.format", "mp3")
    cfg.set("audio.bitrate_kbps", 99999)
    cfg.set("audio.sample_rate", 44100)
    cfg.set("vad.threshold_db", -400)
    cfg.set("tray.blink_interval_ms", "700")
    cfg.set("recent_limit", 0)
    cfg.validate()

    assert cfg.get("audio.format") == "opus"
    assert cfg.get("audio.bitrate_kbps") == 512
    assert cfg.get("audio.sample_rate") == 48000  # 44100 is not an Opus rate
    assert cfg.get("vad.threshold_db") == -90.0
    assert cfg.get("tray.blink_interval_ms") == 700
    assert cfg.get("recent_limit") == 1


def test_layout_defaults_to_one_mixed_track(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    assert cfg.audio_layout == "mix"
    assert cfg.audio_channels == 1


def test_layout_split_means_two_channels(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("audio.layout", "split")
    assert cfg.audio_channels == 2


def test_unknown_layout_falls_back_to_the_mix(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("audio.layout", "quadrofonie")
    cfg.validate()
    assert cfg.get("audio.layout") == "mix"
    assert cfg.audio_channels == 1


def test_silent_side_warning_is_on_by_default(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    assert cfg.get("warnings.silent_side_seconds") == 20


def test_silent_side_warning_is_clamped_and_switchable(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("warnings.silent_side_seconds", 99999)
    cfg.validate()
    assert cfg.get("warnings.silent_side_seconds") == 600

    cfg.set("warnings.silent_side_seconds", 0)  # off is a valid choice
    cfg.validate()
    assert cfg.get("warnings.silent_side_seconds") == 0


def test_validate_keeps_warning_above_stop_threshold(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("disk.warn_mb", 100)
    cfg.set("disk.stop_mb", 900)
    cfg.validate()
    assert cfg.get("disk.warn_mb") >= cfg.get("disk.stop_mb")


def test_validate_normalises_device_values(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("devices.mic", "   ")
    cfg.set("devices.speaker", 42)
    cfg.validate()
    assert cfg.get("devices.mic") == AUTO
    assert cfg.get("devices.speaker") == AUTO


def test_recordings_dir_defaults_to_downloads_subfolder(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    assert cfg.recordings_dir == paths.default_recordings_dir()
    cfg.set("recordings_dir", str(tmp_path / "wo anders"))
    assert cfg.recordings_dir == tmp_path / "wo anders"


def test_block_frames_matches_sample_rate_and_block_length(tmp_path):
    cfg = Config(path=tmp_path / "settings.json")
    assert cfg.block_frames == 960  # 20 ms at 48 kHz
    cfg.set("audio.block_ms", 40)
    assert cfg.block_frames == 1920


def test_save_is_atomic_and_leaves_no_temporary_files(tmp_path):
    target = tmp_path / "settings.json"
    cfg = Config(path=target)
    cfg.save()
    leftovers = [item.name for item in tmp_path.iterdir() if item.name.startswith(".settings-")]
    assert leftovers == []
    assert json.loads(target.read_text(encoding="utf-8"))["audio"]["format"] == "opus"
