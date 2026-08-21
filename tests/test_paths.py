"""Naming scheme: the audio file and its log must always pair up."""

from __future__ import annotations

from datetime import datetime

from echolot import paths


def test_session_basename_uses_date_and_time():
    moment = datetime(2026, 8, 12, 10, 15, 3)
    assert paths.session_basename(moment) == "Echolot_2026-08-12_10-15-03"


def test_audio_and_log_share_the_basename(tmp_path):
    moment = datetime(2026, 8, 12, 10, 15, 3)
    base = paths.unique_session_basename(tmp_path, moment)
    audio = tmp_path / f"{base}{paths.audio_suffix('opus')}"
    log = tmp_path / f"{base}{paths.LOG_SUFFIX}"
    assert audio.stem == log.stem


def test_unique_basename_avoids_collisions(tmp_path):
    moment = datetime(2026, 8, 12, 10, 15, 3)
    first = paths.unique_session_basename(tmp_path, moment)
    (tmp_path / f"{first}.opus").touch()
    second = paths.unique_session_basename(tmp_path, moment)
    assert second == f"{first}_2"

    # A log file alone also blocks the name, otherwise the pair would break.
    (tmp_path / f"{second}.log").touch()
    assert paths.unique_session_basename(tmp_path, moment) == f"{first}_3"


def test_parse_session_basename_roundtrip():
    moment = datetime(2026, 8, 12, 10, 15, 3)
    name = paths.session_basename(moment)
    assert paths.parse_session_basename(name) == moment
    assert paths.parse_session_basename(f"{name}_2") == moment


def test_parse_session_basename_rejects_foreign_names():
    assert paths.parse_session_basename("Besprechung") is None
    assert paths.parse_session_basename("Echolot_2026-13-99_99-99-99") is None
    assert paths.parse_session_basename("Fremd_2026-08-12_10-15-03") is None


def test_audio_suffix_per_format():
    assert paths.audio_suffix("opus") == ".opus"
    assert paths.audio_suffix("FLAC") == ".flac"
    assert paths.audio_suffix("wav") == ".wav"


def test_human_size_uses_the_separator_of_the_active_language():
    """English is the default, so a period. The comma languages are in test_i18n."""
    assert paths.human_size(512) == "512 B"
    assert paths.human_size(2048) == "2.0 KB"
    assert paths.human_size(5 * 1024 * 1024) == "5.0 MB"
    assert paths.human_size(300 * 1024 * 1024) == "300 MB"


def test_list_recordings_newest_first_and_filtered(tmp_path):
    for stamp in ("2026-08-12_10-15-03", "2026-08-12_11-00-00", "2026-08-10_08-00-00"):
        (tmp_path / f"Echolot_{stamp}.opus").write_bytes(b"x" * 10)
    (tmp_path / "Echolot_2026-08-12_11-00-00.log").write_text("{}\n")
    (tmp_path / "urlaub.mp3").touch()  # not ours
    (tmp_path / "Echolot_2026-08-12_11-00-00.txt").touch()  # not an audio file

    found = paths.list_recordings(tmp_path)
    assert [item.basename for item in found] == [
        "Echolot_2026-08-12_11-00-00",
        "Echolot_2026-08-12_10-15-03",
        "Echolot_2026-08-10_08-00-00",
    ]
    assert found[0].has_log is True
    assert found[1].has_log is False
    assert found[0].size_bytes == 10
    assert paths.list_recordings(tmp_path, limit=2) == found[:2]


def test_list_recordings_survives_missing_directory(tmp_path):
    assert paths.list_recordings(tmp_path / "gibtsnicht") == []


def test_default_recordings_dir_is_downloads_subfolder():
    assert paths.default_recordings_dir().name == paths.APP_NAME


def test_install_theme_icon_copies_idle_svg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    dest = paths.install_theme_icon()
    assert dest == tmp_path / "icons" / "hicolor" / "scalable" / "apps" / "echolot.svg"
    assert dest.is_file()
    assert dest.read_bytes() == paths.icon_file("idle").read_bytes()
