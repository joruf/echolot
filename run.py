#!/usr/bin/env python3
"""Echolot - conversation recorder in the system tray.

Usage:
    python3 run.py                  start the tray icon
    python3 run.py --autostart      same, started by the login session
    python3 run.py --toggle         start/stop recording (bind this to a key)
    python3 run.py --record 20      record 20 seconds without any GUI
    python3 run.py --devices        show which devices would be recorded
    python3 run.py --check          check that everything needed is in place
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# --- what this application needs ----------------------------------------------------------------
# Checked before anything below is imported. Whatever is missing is installed in a window that
# shows the work as it happens; see bootstrap_ui.py. `--setup` opens that window even when nothing
# is missing, which is how to see what is installed.
from bootstrap_ui import Need, ensure  # noqa: E402

NEEDS = (
    Need(label="GTK 3 bindings for Python", module="gi",
         packages=("python3-gi", "gir1.2-gtk-3.0")),
    Need(label="Notification bindings", module="gi", packages=("gir1.2-notify-0.7",),
         optional=True, note="there are no desktop notifications"),
    Need(label="Mint desktop bindings", module="gi", packages=("gir1.2-xapp-1.0",),
         optional=True, note="Mint's own status icon is unavailable"),
    Need(label="FFmpeg", command="ffmpeg", packages=("ffmpeg",)),
    Need(label="PulseAudio tools", command="parec", packages=("pulseaudio-utils",)),
    Need(label="Desktop notifications", command="notify-send", packages=("libnotify-bin",),
         optional=True, note="there are no desktop notifications"),
)

# Only when the application is actually being started. Importing this module — which the test
# suite does — should not check anything, let alone put an installer window on screen.
if __name__ == "__main__":
    # Taken out of the arguments once it has been read, so the application's own parser does
    # not trip over a flag that was never meant for it.
    _SETUP = "--setup" in sys.argv

    if _SETUP:
        sys.argv.remove("--setup")

    if not ensure("Echolot", NEEDS, force=_SETUP):
        raise SystemExit(1)


from echolot import VERSION_LABEL, paths  # noqa: E402
from echolot.config import Config  # noqa: E402
from echolot.i18n import t  # noqa: E402


def load_config() -> Config:
    """Load the settings and switch the interface language before anything else."""
    config = Config().load()
    config.apply_language()
    if config.load_error:
        print(t("cli.settings_unreadable", error=config.load_error), file=sys.stderr)
    return config


def cmd_devices(config: Config) -> int:
    from echolot.audio import devices

    resolution = devices.resolve(config.get("devices.mic"), config.get("devices.speaker"))
    print(t("cli.devices_header"))
    print(t("cli.devices_mic", label=resolution.mic_label))
    for name in resolution.mics:
        print(f"      {name}")
    print(t("cli.devices_speaker", label=resolution.speaker_label))
    for name in resolution.speakers:
        print(f"      {name}")
    for problem in resolution.problems:
        print(f"  ! {problem}")
    print()
    print(t("cli.devices_all"))
    for device in devices.list_sources():
        kind = t("cli.device_kind_monitor") if device.is_monitor else t("cli.device_kind_input")
        print(f"  [{kind}] {device.label()}\n      {device.name}")
    return 0 if resolution.complete else 1


def cmd_check(config: Config) -> int:
    from echolot.audio import capture
    from echolot.audio import encoder as encoder_module
    from echolot.session import Recorder, free_megabytes

    print(f"{paths.APP_NAME} {VERSION_LABEL}")
    print(t("cli.check_ffmpeg", version=encoder_module.probe_available() or t("cli.check_missing")))
    print(
        t(
            "cli.check_parec",
            status=t("cli.check_present") if capture.probe_available() else t("cli.check_missing"),
        )
    )
    directory = config.recordings_dir
    free = free_megabytes(directory)
    print(t("cli.check_storage", directory=directory))
    print(
        t("cli.check_free", free=f"{free:.0f}") if free is not None else t("cli.check_free_unknown")
    )
    print(
        t(
            "cli.check_tracks",
            tracks=t("cli.check_tracks_split")
            if config.audio_channels == 2
            else t("cli.check_tracks_mix"),
        )
    )
    minutes = int(config.get("audio.preroll_minutes", 0))
    if minutes:
        per_minute = 60 * int(config.get("audio.sample_rate")) * 2 * config.audio_channels
        print(
            t(
                "cli.check_preroll",
                minutes=minutes,
                memory=paths.human_size(minutes * per_minute),
            )
        )
    else:
        print(t("cli.check_preroll_off"))
    problems = Recorder(config).preflight()
    for problem in problems:
        print(f"  ! {problem}")
    print(t("cli.check_not_ready") if problems else t("cli.check_ready"))
    return 1 if problems else 0


def cmd_record(config: Config, seconds: float) -> int:
    """Headless recording, used for verification and scripting."""
    from echolot.session import Recorder, State, format_duration

    def on_notify(title: str, text: str, kind: str) -> None:
        print(f"[{kind}] {title}: {text}".replace("\n", " | "))

    recorder = Recorder(config, on_notify=on_notify)
    if not recorder.start():
        return 1

    stop_requested = {"value": False}

    def handle_signal(signum, frame) -> None:  # noqa: ANN001, ARG001
        stop_requested["value"] = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    deadline = time.monotonic() + seconds if seconds > 0 else None
    try:
        while recorder.state is State.RECORDING and not stop_requested["value"]:
            if deadline is not None and time.monotonic() >= deadline:
                break
            mic, speaker = recorder.levels()
            print(
                "\r"
                + t(
                    "cli.record_levels",
                    elapsed=format_duration(recorder.elapsed_seconds),
                    mic=f"{mic.level_db:6.1f}",
                    speaker=f"{speaker.level_db:6.1f}",
                ),
                end="",
                flush=True,
            )
            time.sleep(0.2)
    finally:
        print()
        recorder.stop()

    files = recorder.files
    if files is not None:
        print(t("cli.record_audio", path=files.audio))
        print(t("cli.record_log", path=files.log))
    return 0


def cmd_toggle(config: Config) -> int:
    from echolot.instance import InstanceLock, TOGGLE

    lock = InstanceLock()
    if lock.signal_owner(TOGGLE):
        return 0
    # Nothing running: bring up the tray and start recording right away.
    return run_tray(config, autostart=False, record_now=True)


def cmd_quit() -> int:
    from echolot.instance import InstanceLock

    return 0 if InstanceLock().signal_owner(signal.SIGTERM) else 1


def run_tray(config: Config, *, autostart: bool, record_now: bool = False) -> int:
    from echolot.app import EcholotApp

    return EcholotApp(config, autostart=autostart, record_now=record_now).run()


def main(argv: list[str] | None = None) -> int:
    # The language is needed for the help texts, so the settings are read first.
    config = load_config()

    parser = argparse.ArgumentParser(prog=paths.APP_ID, description=t("cli.description"))
    parser.add_argument(
        "--version", action="version", version=f"{paths.APP_NAME} {VERSION_LABEL}"
    )
    parser.add_argument("--autostart", action="store_true", help=t("cli.help_autostart"))
    parser.add_argument("--toggle", action="store_true", help=t("cli.help_toggle"))
    parser.add_argument("--quit", action="store_true", help=t("cli.help_quit"))
    parser.add_argument("--devices", action="store_true", help=t("cli.help_devices"))
    parser.add_argument("--check", action="store_true", help=t("cli.help_check"))
    parser.add_argument("--record", type=float, metavar="SECONDS", help=t("cli.help_record"))
    args = parser.parse_args(argv)

    if args.devices:
        return cmd_devices(config)
    if args.check:
        return cmd_check(config)
    if args.record is not None:
        return cmd_record(config, args.record)
    if args.quit:
        return cmd_quit()
    if args.toggle:
        return cmd_toggle(config)
    return run_tray(config, autostart=args.autostart)


if __name__ == "__main__":
    sys.exit(main())
