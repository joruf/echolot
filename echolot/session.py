"""The recording session: state machine, file naming, guards.

Everything that can go wrong during a conversation is handled here, and the rule
throughout is the same: keep recording. A missing microphone, a dying capture
process, a device switch or a broken log must not end the session, because the
part you cannot get back is what the other person just said.

Only two situations stop a running recording on their own: the encoder dying
(there is nothing left to write into) and the disk running out (writing would
corrupt what already exists). Both are announced, both are written to the log.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from . import __build__, __version__, paths
from .i18n import t
from .audio import capture, devices
from .audio import encoder as encoder_module
from .audio import preroll as preroll_module
from .audio import watcher as watcher_module
from .audio.mixer import LAYOUT_SPLIT, SILENT_METRICS, ChannelMetrics, Mixer
from .speechlog import MIC, SPEAKER, SpeechLog

FIRST_BLOCK_TIMEOUT = 2.0
MIXER_JOIN_TIMEOUT = 5.0
# A pre-roll flush aborts as soon as the mixer is stopped, so this only has to
# cover the block that was already being written.
FLUSH_JOIN_TIMEOUT = 5.0
# Below this length a recording is too short to conclude anything from a silent
# side; above it, silence for the whole session is a routing problem.
SILENT_SIDE_MIN_SECONDS = 30.0


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    ERROR = "error"


@dataclass(frozen=True)
class SessionFiles:
    """The two files a session writes."""

    basename: str
    audio: Path
    log: Path
    started_at: datetime


def free_megabytes(path: Path) -> float | None:
    """Free space at `path` in MB, or None if it cannot be determined."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        stats = os.statvfs(probe)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize / (1024 * 1024)


class Recorder:
    """Owns the pipeline. All public methods are safe to call from any thread."""

    def __init__(
        self,
        config,
        *,
        on_state: Callable[[State], None] | None = None,
        on_notify: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_state = on_state
        self.on_notify = on_notify

        self.files: SessionFiles | None = None
        self.resolution: devices.Resolution | None = None
        self.last_error: str | None = None
        self.last_result: str | None = None

        self.preroll = preroll_module.Preroll(config)

        self._state = State.IDLE
        self._lock = threading.RLock()
        self._stopping = False
        self._shutting_down = False
        self._flush: threading.Thread | None = None
        self._mixer: Mixer | None = None
        self._encoder: encoder_module.Encoder | None = None
        self._speechlog: SpeechLog | None = None
        self._captures: dict[str, capture.CaptureProcess] = {}
        self._watcher: watcher_module.DeviceWatcher | None = None
        self._guard_stop = threading.Event()
        self._guard: threading.Thread | None = None
        self._warned_low_disk = False

    # -- state ----------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def active(self) -> bool:
        return self._state in (State.RECORDING, State.PAUSED)

    @property
    def elapsed_seconds(self) -> float:
        mixer = self._mixer
        return mixer.seconds_written if mixer is not None else 0.0

    def levels(self) -> tuple[ChannelMetrics, ChannelMetrics]:
        mixer = self._mixer
        return mixer.levels() if mixer is not None else (SILENT_METRICS, SILENT_METRICS)

    def device_labels(self) -> tuple[str, str]:
        resolution = self.resolution
        if resolution is None:
            return "-", "-"
        return resolution.mic_label, resolution.speaker_label

    def _set_state(self, state: State) -> None:
        self._state = state
        if self.on_state is not None:
            self.on_state(state)

    def _notify(self, title: str, text: str, kind: str = "info") -> None:
        if kind == "error":
            self.last_error = text
        if self.on_notify is not None:
            self.on_notify(title, text, kind)

    # -- preflight ------------------------------------------------------

    def preflight(self) -> list[str]:
        """Problems that would keep a recording from working, in the user's language."""
        problems: list[str] = []
        if encoder_module.probe_available() is None:
            problems.append(t("session.ffmpeg_missing"))
        if not capture.probe_available():
            problems.append(t("session.parec_missing"))
        directory = self.config.recordings_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(t("session.folder_failed", directory=directory, error=exc))
        else:
            if not os.access(directory, os.W_OK):
                problems.append(t("session.folder_readonly", directory=directory))
        free = free_megabytes(directory)
        if free is not None and free < self.config.get("disk.stop_mb"):
            problems.append(t("session.disk_low", free=f"{free:.0f}"))
        resolution = devices.resolve(
            self.config.get("devices.mic"), self.config.get("devices.speaker")
        )
        if not resolution.mic and not resolution.speaker:
            problems.append(t("session.no_devices"))
        return problems

    # -- start ----------------------------------------------------------

    def start(self) -> bool:
        with self._lock:
            if self.active or self._stopping:
                return False
            self.last_error = None
            self.last_result = None

            config = self.config
            if encoder_module.probe_available() is None:
                self._notify(paths.APP_NAME, t("session.ffmpeg_missing_short"), "error")
                return False
            if not capture.probe_available():
                self._notify(paths.APP_NAME, t("session.parec_missing_short"), "error")
                return False

            directory = config.recordings_dir
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._notify(paths.APP_NAME, t("app.folder_failed", error=exc), "error")
                return False

            free = free_megabytes(directory)
            stop_mb = config.get("disk.stop_mb")
            if free is not None and free < stop_mb:
                self._notify(
                    paths.APP_NAME,
                    t("session.disk_too_low", free=f"{free:.0f}", needed=stop_mb),
                    "error",
                )
                return False

            resolution = devices.resolve(
                config.get("devices.mic"), config.get("devices.speaker")
            )
            if not resolution.mic and not resolution.speaker:
                self._notify(paths.APP_NAME, t("session.no_devices"), "error")
                return False
            self.resolution = resolution

            # Take over the pre-roll buffer, if one is running. This also ends the
            # buffering, so the audio in RAM is exactly what is written next.
            handover = self.preroll.hand_over()

            started_at = datetime.now()
            audio_format = config.audio_format
            basename = paths.unique_session_basename(directory, started_at, audio_format)
            audio_path = directory / f"{basename}{config.audio_suffix}"
            log_path = directory / f"{basename}{paths.LOG_SUFFIX}"

            sample_rate = int(config.get("audio.sample_rate"))
            block_ms = int(config.get("audio.block_ms"))
            block_frames = config.block_frames

            layout = config.audio_layout
            encoder = encoder_module.Encoder(
                audio_path,
                audio_format=audio_format,
                bitrate_kbps=int(config.get("audio.bitrate_kbps")),
                sample_rate=sample_rate,
                channels=config.audio_channels,
                title=basename,
            )
            try:
                encoder.start()
            except encoder_module.EncoderError as exc:
                self._notify(paths.APP_NAME, str(exc), "error")
                return False

            speech = SpeechLog(
                log_path,
                threshold_db=float(config.get("vad.threshold_db")),
                min_segment_ms=int(config.get("vad.min_segment_ms")),
                hangover_ms=int(config.get("vad.hangover_ms")),
                adaptive=bool(config.get("vad.adaptive_noise_floor")),
                block_ms=block_ms,
            )
            try:
                speech.open(
                    {
                        "app": paths.APP_NAME,
                        "version": __version__,
                        "build": __build__,
                        "started_at": started_at.astimezone().isoformat(timespec="seconds"),
                        "audio": audio_path.name,
                        "log": log_path.name,
                        "format": audio_format,
                        "layout": layout,
                        # Seconds of audio that were already in RAM when the
                        # recording was started: everything before this point in
                        # the file happened before the double click.
                        "preroll_seconds": round(handover.seconds, 2) if handover else 0.0,
                        "sample_rate": sample_rate,
                        "block_ms": block_ms,
                        # In a mixed recording the voices are no longer separable
                        # in the audio, so the speech entries below are the only
                        # place that says who was talking.
                        "channels": (
                            {"0": MIC, "1": SPEAKER}
                            if layout == LAYOUT_SPLIT
                            else {"0": f"{MIC}+{SPEAKER}"}
                        ),
                        "devices": {
                            MIC: {"name": resolution.mic, "label": resolution.mic_label},
                            SPEAKER: {
                                "name": resolution.speaker,
                                "label": resolution.speaker_label,
                            },
                        },
                        "vad": {
                            "threshold_db": float(config.get("vad.threshold_db")),
                            "min_segment_ms": int(config.get("vad.min_segment_ms")),
                            "hangover_ms": int(config.get("vad.hangover_ms")),
                            "adaptive": bool(config.get("vad.adaptive_noise_floor")),
                        },
                    }
                )
            except OSError as exc:
                encoder.close(timeout=2)
                self._notify(paths.APP_NAME, t("session.log_not_writable", error=exc), "error")
                return False

            for problem in resolution.problems:
                speech.event("device_problem", 0.0, message=problem)

            # Capture processes inherited from the pre-roll keep running: stopping
            # and recreating them would tear a hole at exactly the moment the user
            # pressed record.
            self._captures = dict(handover.captures) if handover else {}
            fresh: dict[str, capture.CaptureProcess] = {}
            for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
                if not device:
                    if side not in self._captures:
                        speech.event("side_unavailable", 0.0, side=side)
                    continue
                if side in self._captures:
                    self._captures[side].on_event = self._on_capture_event
                    continue
                process = capture.CaptureProcess(
                    device,
                    side=side,
                    sample_rate=sample_rate,
                    block_frames=block_frames,
                    on_event=self._on_capture_event,
                )
                try:
                    process.start()
                except OSError as exc:
                    speech.event("source_spawn_failed", 0.0, side=side, error=str(exc))
                    continue
                self._captures[side] = process
                fresh[side] = process

            # Wait for real audio before starting the timeline, otherwise the
            # capture start-up delay would show up as silence at the front.
            # Inherited processes have been delivering for minutes already.
            deadline = time.monotonic() + FIRST_BLOCK_TIMEOUT
            for side, process in fresh.items():
                remaining = max(0.2, deadline - time.monotonic())
                if not process.wait_for_first_block(remaining):
                    speech.event("side_silent_at_start", 0.0, side=side, device=process.device)
                    self._notify(
                        paths.APP_NAME,
                        t(
                            "session.side_silent",
                            side=t("common.mic") if side == MIC else t("common.other_track"),
                        ),
                        "warning",
                    )

            # A device that changed while the buffer was filling: switch the
            # inherited process over, gap-free, and let the log say so.
            if handover:
                for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
                    process = self._captures.get(side)
                    if process is not None and device and process.device != device:
                        speech.event(
                            "preroll_device_changed",
                            0.0,
                            side=side,
                            **{"from": process.device, "to": device},
                        )
                        process.retarget(device)

            self._encoder = encoder
            self._speechlog = speech
            self.files = SessionFiles(
                basename=basename, audio=audio_path, log=log_path, started_at=started_at
            )

            mixer = Mixer(
                mic=self._captures.get(MIC),
                speaker=self._captures.get(SPEAKER),
                encoder=encoder,
                sample_rate=sample_rate,
                block_frames=block_frames,
                layout=layout,
                initial_blocks=handover.blocks if handover else 0,
                on_metrics=speech.feed,
                on_error=self._on_mixer_error,
            )
            self._mixer = mixer

            if handover and handover.entries:
                # Pushing minutes of buffered audio through the encoder takes
                # seconds, so it must not happen on the calling thread: the tray
                # has to turn red immediately, and the live audio queues in the
                # capture buffers meanwhile (sized for exactly this).
                self._flush = threading.Thread(
                    target=self._flush_preroll,
                    args=(handover, encoder, speech, mixer),
                    name="echolot-preroll-flush",
                    daemon=True,
                )
                self._flush.start()
            else:
                mixer.start()

            self._warned_low_disk = False
            self._guard_stop = threading.Event()
            self._guard = threading.Thread(
                target=self._disk_guard, name="echolot-disk-guard", daemon=True
            )
            self._guard.start()

            if config.get("devices.follow_default"):
                self._watcher = watcher_module.DeviceWatcher(
                    config.get("devices.mic"),
                    config.get("devices.speaker"),
                    self._on_devices_changed,
                )
                self._watcher.start(current=resolution)

            self._set_state(State.RECORDING)
            if config.get("notifications.on_start"):
                preroll_note = (
                    t("session.recording_preroll", duration=format_duration(handover.seconds))
                    if handover and handover.entries
                    else ""
                )
                self._notify(
                    t("session.recording_title"),
                    t(
                        "session.recording_body",
                        mic=resolution.mic_label,
                        speaker=resolution.speaker_label,
                        file=audio_path.name,
                    )
                    + preroll_note,
                )
            return True

    def _warn_about_silent_sides(self, speech, captures: dict, duration: float) -> None:
        """Say it now if a side never carried a single word.

        A whole conversation with nothing on the other side's track is not a
        subtlety to be found in the log next week - it means the audio never
        reached this machine, and the recording is half worthless. The one thing
        worse than that happening is not being told.
        """
        if duration < SILENT_SIDE_MIN_SECONDS:
            return
        silent = [
            side
            for side in (MIC, SPEAKER)
            if side in captures and speech.speech_seconds.get(side, 0.0) <= 0.0
        ]
        if not silent:
            return
        labels = " + ".join(
            t("common.mic") if side == MIC else t("common.other") for side in silent
        )
        self._notify(
            paths.APP_NAME,
            t("session.side_silent_session", sides=labels, duration=format_duration(duration)),
            "warning",
        )

    def _flush_preroll(self, handover, encoder, speech, mixer) -> None:
        """Write the buffered audio and its speech metrics, then go live.

        Order matters twice over: the audio has to be in the file before anything
        live is appended, and the log entries have to describe the same stretch of
        time, otherwise the timestamps in the log would not match the file.
        """
        written = 0
        block_seconds = handover.block_seconds
        for payload, mic_metrics, speaker_metrics in handover.entries:
            if mixer.stopping:
                break
            if not encoder.write(payload):
                break
            speech.feed(written * block_seconds, mic_metrics, speaker_metrics)
            written += 1

        speech.event(
            "preroll_written",
            written * block_seconds,
            blocks=written,
            of_blocks=handover.blocks,
            complete=written == handover.blocks,
        )
        # The moment the user actually pressed record - everything before this is
        # the buffer.
        speech.event("recording_started", handover.blocks * block_seconds)
        if not mixer.stopping:
            mixer.start()

    # -- stop -----------------------------------------------------------

    def stop(self, reason: str | None = None) -> bool:
        with self._lock:
            if not self.active and self._state is not State.ERROR:
                return False
            if self._stopping:
                return False
            self._stopping = True

        try:
            watcher = self._watcher
            self._watcher = None
            if watcher is not None:
                watcher.stop()

            self._guard_stop.set()
            guard, self._guard = self._guard, None
            if guard is not None and guard is not threading.current_thread():
                guard.join(timeout=2)

            mixer, self._mixer = self._mixer, None
            if mixer is not None:
                mixer.stop()

            # Stopping the mixer is also the abort signal for a pre-roll flush
            # still in progress, so wait for that before closing the encoder.
            flush, self._flush = self._flush, None
            if flush is not None and flush is not threading.current_thread():
                flush.join(timeout=FLUSH_JOIN_TIMEOUT)

            if mixer is not None:
                # join() is a no-op when called from the mixer thread itself,
                # which happens when the encoder failed mid-recording.
                mixer.join(timeout=MIXER_JOIN_TIMEOUT)

            captures, self._captures = self._captures, {}
            for process in captures.values():
                process.stop()

            encoder, self._encoder = self._encoder, None
            returncode = encoder.close() if encoder is not None else None

            speech, self._speechlog = self._speechlog, None
            files = self.files
            if speech is not None:
                totals: dict = {
                    "duration": round(mixer.seconds_written, 2) if mixer is not None else 0.0,
                    "blocks": mixer.blocks_written if mixer is not None else 0,
                    "gap_blocks": {
                        MIC: mixer.mic_gap_blocks if mixer is not None else 0,
                        SPEAKER: mixer.speaker_gap_blocks if mixer is not None else 0,
                    },
                    "silence_filled_blocks": mixer.silence_filled_blocks if mixer is not None else 0,
                    # Blocks in which both sides were loud enough at the same
                    # instant that the mono sum had to be limited.
                    "clipped_blocks": mixer.clipped_blocks if mixer is not None else 0,
                    # Blocks each side actually delivered. Compared with `blocks`
                    # this shows whether a side lagged behind - the number to look
                    # at when a recording sounds out of sync.
                    "captured_blocks": {
                        side: process.total_blocks for side, process in captures.items()
                    },
                    "restarts": {side: process.restarts for side, process in captures.items()},
                    "overruns": {side: process.overruns for side, process in captures.items()},
                    "audio_bytes": encoder.bytes_written if encoder is not None else 0,
                    "encoder_returncode": returncode,
                }
                if reason:
                    totals["stopped_because"] = reason
                speech.close(**totals)

            duration = mixer.seconds_written if mixer is not None else 0.0
            if speech is not None:
                self._warn_about_silent_sides(speech, captures, duration)

            size = 0
            if files is not None:
                try:
                    size = files.audio.stat().st_size
                except OSError:
                    size = 0
                self.last_result = f"{files.audio.name} · {format_duration(duration)}"

            if returncode not in (0, None) and size <= 0:
                # No usable file: this is the one case that deserves an error.
                detail = encoder.stderr_tail if encoder is not None else ""
                self._notify(
                    paths.APP_NAME,
                    t(
                        "session.failed",
                        code=returncode,
                        detail=detail.splitlines()[-1] if detail else "",
                    ),
                    "error",
                )
            elif returncode not in (0, None):
                # A file exists and plays - typically ffmpeg got the same signal we
                # did, for instance when logging out. Calling that "broken" would
                # discredit a perfectly good recording, so it is a warning with the
                # facts, and the code itself is in the log.
                self._notify(
                    t("session.stopped_title"),
                    t(
                        "session.encoder_aborted",
                        duration=format_duration(duration),
                        size=paths.human_size(size),
                        code=returncode,
                    ),
                    "warning",
                )
            elif self.config.get("notifications.on_stop"):
                self._notify(
                    t("session.stopped_title"),
                    t(
                        "session.stopped_body",
                        duration=format_duration(duration),
                        size=paths.human_size(size),
                        file=files.audio.name if files else "",
                    ),
                )
            self._set_state(State.IDLE)
            return True
        finally:
            self._stopping = False
            # Start buffering again right away, so the next conversation also has
            # its lead-up available.
            self.apply_preroll_settings()

    # -- pause / toggle -------------------------------------------------

    def pause(self) -> bool:
        with self._lock:
            mixer = self._mixer
            if self._state is not State.RECORDING or mixer is None:
                return False
            mixer.pause()
            if self._speechlog is not None:
                self._speechlog.event("pause", mixer.seconds_written)
            self._set_state(State.PAUSED)
            return True

    def resume(self) -> bool:
        with self._lock:
            mixer = self._mixer
            if self._state is not State.PAUSED or mixer is None:
                return False
            mixer.resume()
            if self._speechlog is not None:
                self._speechlog.event("resume", mixer.seconds_written)
            self._set_state(State.RECORDING)
            return True

    def toggle_pause(self) -> bool:
        return self.resume() if self._state is State.PAUSED else self.pause()

    def toggle(self) -> bool:
        """What a double click does."""
        return self.stop() if self.active else self.start()

    # -- pre-roll -------------------------------------------------------

    def apply_preroll_settings(self) -> None:
        """Make the pre-roll buffer match the settings.

        Called at startup, after a settings change and after every recording. Does
        nothing while a recording is running - the buffer has been handed over to
        it and gets rebuilt when it ends.
        """
        if self.active or self._stopping or self._shutting_down:
            return
        try:
            self.preroll.ensure()
        except OSError as exc:  # pragma: no cover - depends on the sound server
            self._notify(paths.APP_NAME, t("session.preroll_failed", error=exc), "warning")

    def preroll_status(self) -> str | None:
        """Short status line for the tooltip, or None when the buffer is off."""
        if not self.preroll.active:
            return None
        return t(
            "tooltip.preroll",
            buffered=format_duration(self.preroll.seconds_buffered),
            total=f"{self.preroll.minutes}:00",
        )

    def shutdown(self) -> None:
        """Give up everything, including the idle buffer."""
        self._shutting_down = True
        if self.active:
            self.stop(reason="quit")
        self.preroll.stop()

    # -- device settings ------------------------------------------------

    def apply_device_settings(self) -> None:
        """Re-read the configured devices and act on the change.

        Switching a running recording has to wait for the new capture process to
        deliver its first block, so the work happens on a background thread -
        this is called from the UI thread and must not block it.
        """
        config = self.config
        mic_setting = config.get("devices.mic")
        speaker_setting = config.get("devices.speaker")
        follow = bool(config.get("devices.follow_default"))

        watcher = self._watcher
        if watcher is not None:
            watcher.update_settings(mic_setting, speaker_setting)

        if not self.active:
            # A different device means the buffered audio came from the wrong
            # source, so the buffer is rebuilt rather than kept.
            self.apply_preroll_settings()
            return

        if follow and watcher is None:
            watcher = watcher_module.DeviceWatcher(
                mic_setting, speaker_setting, self._on_devices_changed
            )
            self._watcher = watcher
            watcher.start(current=self.resolution)
        elif not follow and watcher is not None:
            self._watcher = None
            threading.Thread(target=watcher.stop, name="echolot-watch-off", daemon=True).start()

        threading.Thread(
            target=self._retarget_to_settings,
            args=(mic_setting, speaker_setting),
            name="echolot-retarget",
            daemon=True,
        ).start()

    def _retarget_to_settings(self, mic_setting: str, speaker_setting: str) -> None:
        resolution = devices.resolve(mic_setting, speaker_setting)
        if not self.active:
            return
        self.resolution = resolution
        if self._speechlog is not None:
            self._speechlog.event(
                "device_setting_changed",
                self.elapsed_seconds,
                mic=mic_setting,
                speaker=speaker_setting,
            )
        for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
            process = self._captures.get(side)
            if process is not None and device and process.device != device:
                process.retarget(device)

    # -- callbacks ------------------------------------------------------

    def _on_capture_event(self, event: str, fields: dict) -> None:
        speech = self._speechlog
        mixer = self._mixer
        at = mixer.seconds_written if mixer is not None else None
        if speech is not None:
            speech.event(event, at, **fields)
        if event == "source_error":
            side = fields.get("side")
            self._notify(
                paths.APP_NAME,
                t(
                    "session.source_lost",
                    side=t("common.mic") if side == MIC else t("common.other_track"),
                ),
                "warning",
            )

    def _on_devices_changed(self, resolution: devices.Resolution) -> None:
        """Follow the default devices without interrupting the recording."""
        if not self.active:
            return
        self.resolution = resolution
        for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
            process = self._captures.get(side)
            if process is None or not device or process.device == device:
                continue
            process.retarget(device)

    def _on_mixer_error(self, message: str) -> None:
        # Called from the mixer thread: stopping from here would join ourselves.
        self._set_state(State.ERROR)
        self._notify(paths.APP_NAME, message, "error")
        threading.Thread(
            target=self.stop, kwargs={"reason": message}, name="echolot-fail-stop", daemon=True
        ).start()

    def _disk_guard(self) -> None:
        interval = float(self.config.get("disk.check_interval_s"))
        warn_mb = float(self.config.get("disk.warn_mb"))
        stop_mb = float(self.config.get("disk.stop_mb"))
        directory = self.config.recordings_dir
        while not self._guard_stop.wait(interval):
            free = free_megabytes(directory)
            if free is None:
                continue
            if free < stop_mb:
                message = t("session.disk_full", free=f"{free:.0f}")
                if self._speechlog is not None:
                    self._speechlog.event(
                        "disk_full", self.elapsed_seconds, free_mb=round(free, 1)
                    )
                self._notify(paths.APP_NAME, message, "error")
                self.stop(reason="disk_full")
                return
            if free < warn_mb and not self._warned_low_disk:
                self._warned_low_disk = True
                if self._speechlog is not None:
                    self._speechlog.event(
                        "disk_warning", self.elapsed_seconds, free_mb=round(free, 1)
                    )
                self._notify(
                    paths.APP_NAME,
                    t("session.disk_warning", free=f"{free:.0f}"),
                    "warning",
                )


def format_duration(seconds: float) -> str:
    """`01:23:45` for long sessions, `12:34` for short ones."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
