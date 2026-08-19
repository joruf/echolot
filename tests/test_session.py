"""Session guards: the two cases that may stop a recording, and the ones that may not."""

from __future__ import annotations

import threading
import time
from datetime import datetime

from echolot import session as session_module
from echolot.audio import capture
from echolot.i18n import t
from echolot.audio import encoder as encoder_module
from echolot import paths
from echolot.session import Recorder, State, format_duration, free_megabytes


def test_format_duration_switches_to_hours():
    assert format_duration(0) == "00:00"
    assert format_duration(65) == "01:05"
    assert format_duration(3725) == "01:02:05"


def test_free_megabytes_works_on_a_missing_subdirectory(tmp_path):
    free = free_megabytes(tmp_path / "gibt" / "es" / "nicht")
    assert free is not None and free > 0


def test_preflight_reports_missing_ffmpeg(config, monkeypatch):
    monkeypatch.setattr(encoder_module, "probe_available", lambda: None)
    monkeypatch.setattr(capture, "probe_available", lambda: True)
    problems = Recorder(config).preflight()
    assert any("ffmpeg" in problem for problem in problems)


def test_preflight_reports_missing_parec(config, monkeypatch):
    monkeypatch.setattr(encoder_module, "probe_available", lambda: "ffmpeg 6")
    monkeypatch.setattr(capture, "probe_available", lambda: False)
    assert any("parec" in problem for problem in Recorder(config).preflight())


def test_start_refuses_without_ffmpeg_and_says_so(config, monkeypatch):
    monkeypatch.setattr(encoder_module, "probe_available", lambda: None)
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))

    assert recorder.start() is False
    assert recorder.state is State.IDLE
    assert any(kind == "error" and "ffmpeg" in text for kind, text in messages)


def test_start_refuses_when_the_disk_is_full(config, monkeypatch):
    monkeypatch.setattr(encoder_module, "probe_available", lambda: "ffmpeg 6")
    monkeypatch.setattr(capture, "probe_available", lambda: True)
    monkeypatch.setattr(session_module, "free_megabytes", lambda path: 50.0)
    config.set("disk.stop_mb", 300)

    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    assert recorder.start() is False
    # Compared against the catalogue, so the test does not depend on a language.
    assert any(
        text == t("session.disk_too_low", free="50", needed=300) for _kind, text in messages
    )


def test_start_refuses_without_any_device(config, monkeypatch):
    from echolot.audio import devices

    monkeypatch.setattr(encoder_module, "probe_available", lambda: "ffmpeg 6")
    monkeypatch.setattr(capture, "probe_available", lambda: True)
    monkeypatch.setattr(
        devices,
        "resolve",
        lambda mic, speaker: devices.Resolution((), (), "-", "-", ("nichts da",)),
    )
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    assert recorder.start() is False
    assert any(text == t("session.no_devices") for _kind, text in messages)


def test_stop_on_an_idle_recorder_does_nothing(config):
    assert Recorder(config).stop() is False


def test_pause_and_resume_require_a_running_session(config):
    recorder = Recorder(config)
    assert recorder.pause() is False
    assert recorder.resume() is False


def test_disk_guard_stops_the_recording_when_space_runs_out(config, monkeypatch):
    """The one case where stopping is the right answer: writing on would corrupt."""
    monkeypatch.setattr(session_module, "free_megabytes", lambda path: 10.0)
    config.set("disk.stop_mb", 300)
    config.set("disk.check_interval_s", 1)

    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    recorder._state = State.RECORDING  # a session without a real pipeline behind it

    guard = threading.Thread(target=recorder._disk_guard, daemon=True)
    guard.start()
    deadline = time.monotonic() + 8
    while recorder.state is State.RECORDING and time.monotonic() < deadline:
        time.sleep(0.05)
    guard.join(timeout=3)

    assert recorder.state is State.IDLE
    assert any(text == t("session.disk_full", free="10") for _kind, text in messages)


def test_disk_guard_warns_once_before_stopping(config, monkeypatch):
    monkeypatch.setattr(session_module, "free_megabytes", lambda path: 500.0)
    config.set("disk.warn_mb", 1000)
    config.set("disk.stop_mb", 100)
    config.set("disk.check_interval_s", 1)

    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    recorder._state = State.RECORDING

    guard = threading.Thread(target=recorder._disk_guard, daemon=True)
    guard.start()
    time.sleep(2.6)
    recorder._guard_stop.set()
    guard.join(timeout=3)

    warnings = [text for kind, text in messages if kind == "warning"]
    assert len(warnings) == 1  # warned, not spammed
    assert recorder.state is State.RECORDING  # and kept recording


class StubEncoder:
    """An encoder that has already finished with a given exit code."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.bytes_written = 4096
        self.stderr_tail = "Exiting normally, received signal 15."

    def close(self, timeout: float = 15.0) -> int:
        return self.returncode

    def write(self, data: bytes) -> bool:
        return True


def prepare_finished_session(recorder, tmp_path, audio_bytes: bytes, returncode: int):
    """Put a recorder into the state stop() sees at the end of a session."""
    from echolot.session import SessionFiles

    audio = tmp_path / "Echolot_2026-08-12_10-15-03.opus"
    audio.write_bytes(audio_bytes)
    recorder.files = SessionFiles(
        basename=audio.stem,
        audio=audio,
        log=tmp_path / "Echolot_2026-08-12_10-15-03.log",
        started_at=datetime.now(),
    )
    recorder._encoder = StubEncoder(returncode)
    recorder._state = State.RECORDING


def test_encoder_killed_by_a_signal_is_a_warning_when_the_file_is_usable(config, tmp_path):
    """Logging out kills ffmpeg too; that must not discredit a good recording."""
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_finished_session(recorder, tmp_path, b"x" * 4096, returncode=255)

    assert recorder.stop(reason="quit") is True
    kinds = [kind for kind, _text in messages]
    assert "error" not in kinds
    assert any(
        kind == "warning"
        and text
        == t(
            "session.encoder_aborted",
            duration=format_duration(0.0),
            size=paths.human_size(4096),
            code=255,
        )
        for kind, text in messages
    )


def test_encoder_failure_without_a_file_is_an_error(config, tmp_path):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_finished_session(recorder, tmp_path, b"", returncode=1)

    assert recorder.stop() is True
    # The tool name and its exit code appear in every language.
    assert any(kind == "error" and "ffmpeg 1" in text for kind, text in messages)


class StubCapture:
    """Stands in for a CaptureProcess during teardown."""

    total_blocks = 0
    restarts = 0
    overruns = 0

    def stop(self, timeout: float = 1.0) -> None:
        pass


class StubSpeechLog:
    """A finished speech log with known totals."""

    def __init__(self, speech_seconds: dict) -> None:
        self.speech_seconds = speech_seconds
        self.closed_with: dict | None = None

    def close(self, **totals) -> None:
        self.closed_with = totals

    def event(self, *args, **kwargs) -> None:
        pass


def prepare_with_log(recorder, tmp_path, speech_seconds, sides, duration):
    """A session about to stop, with a known speech balance and length."""
    prepare_finished_session(recorder, tmp_path, b"x" * 4096, returncode=0)
    recorder._speechlog = StubSpeechLog(speech_seconds)
    recorder._captures = {side: StubCapture() for side in sides}

    class StubMixer:
        seconds_written = duration
        blocks_written = int(duration * 50)
        mic_gap_blocks = 0
        speaker_gap_blocks = 0
        silence_filled_blocks = 0
        clipped_blocks = 0

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    recorder._mixer = StubMixer()


def test_a_side_that_stayed_silent_all_recording_is_reported(config, tmp_path):
    """The whole point: never discover an empty other-side track days later."""
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 180.0, "speaker": 0.0}, ("mic", "speaker"), duration=1130.0
    )

    recorder.stop()

    warnings = [text for kind, text in messages if kind == "warning"]
    assert len(warnings) == 1
    assert t("common.other") in warnings[0]
    assert t("common.mic") not in warnings[0]  # the microphone was fine
    assert "18:50" in warnings[0]  # and it says how long that went on


def test_both_sides_silent_are_named_together(config, tmp_path):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 0.0, "speaker": 0.0}, ("mic", "speaker"), duration=600.0
    )

    recorder.stop()
    warnings = [text for kind, text in messages if kind == "warning"]
    assert len(warnings) == 1
    assert t("common.mic") in warnings[0] and t("common.other") in warnings[0]


def test_a_silent_side_is_reported_while_the_conversation_still_runs(config):
    """The warning that matters: early enough to fix the routing."""
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))

    recorder._on_silent_side("speaker", 20.0)

    assert messages == [
        (
            "warning",
            t("session.side_no_audio", sides=t("common.other"), duration=format_duration(20.0)),
        )
    ]
    assert "speaker" in recorder._warned_silent_sides


def test_the_end_of_recording_warning_does_not_repeat_the_early_one(config, tmp_path):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 180.0, "speaker": 0.0}, ("mic", "speaker"), duration=1130.0
    )
    recorder._warned_silent_sides = {"speaker"}  # already said, during the conversation

    recorder.stop()

    assert [kind for kind, _text in messages] == ["info"]


def test_no_warning_when_both_sides_were_heard(config, tmp_path):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 12.0, "speaker": 400.0}, ("mic", "speaker"), duration=900.0
    )

    recorder.stop()
    assert [kind for kind, _text in messages] == ["info"]


def test_no_warning_for_a_short_recording(config, tmp_path):
    """Ten seconds of silence proves nothing and must not nag."""
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 0.0, "speaker": 0.0}, ("mic", "speaker"), duration=10.0
    )

    recorder.stop()
    assert [kind for kind, _text in messages] == ["info"]


def test_a_side_without_a_device_is_not_blamed(config, tmp_path):
    """No microphone at all is already reported at start; not again at the end."""
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_with_log(
        recorder, tmp_path, {"mic": 0.0, "speaker": 300.0}, ("speaker",), duration=900.0
    )

    recorder.stop()
    assert [kind for kind, _text in messages] == ["info"]


def test_clean_stop_reports_duration_and_size(config, tmp_path):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    prepare_finished_session(recorder, tmp_path, b"x" * 2048, returncode=0)

    assert recorder.stop() is True
    assert [kind for kind, _text in messages] == ["info"]
    assert recorder.last_result is not None


def test_capture_event_is_logged_and_announced(config):
    messages = []
    recorder = Recorder(config, on_notify=lambda title, text, kind: messages.append((kind, text)))
    recorder._on_capture_event("source_error", {"side": "speaker", "device": "monitor"})
    expected = t("session.source_lost", side=t("common.other_track"))
    assert any(kind == "warning" and text == expected for kind, text in messages)
