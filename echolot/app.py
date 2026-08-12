"""The application: tray icon, menu, windows and the recorder behind them.

Everything the recorder reports arrives from worker threads, so every callback is
handed to the GTK main loop with `GLib.idle_add` before it touches a widget.
"""

from __future__ import annotations

import signal
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import autostart as autostart_module
from . import paths
from .i18n import t
from .instance import PING, TOGGLE, InstanceLock
from .notify import Notifier
from .session import Recorder, State, format_duration, free_megabytes
from .ui.level_test import LevelTestWindow
from .ui.menu import AppMenu, shorten, state_label
from .ui.settings_window import SettingsWindow
from .ui.tray import TrayIcon

STATE_ICONS = {
    State.IDLE: "idle",
    State.RECORDING: "rec-on",
    State.PAUSED: "paused",
    State.ERROR: "error",
}


class EcholotApp:
    """Owns the GTK main loop."""

    def __init__(self, config, *, autostart: bool = False, record_now: bool = False) -> None:
        self.config = config
        self.quiet_start = autostart
        self.record_now = record_now
        self.notifier = Notifier()
        self.lock = InstanceLock()
        self.recorder = Recorder(
            config, on_state=self._on_state, on_notify=self._on_notify
        )
        self.menu = AppMenu(self)
        self.tray: TrayIcon | None = None
        self.settings_window: SettingsWindow | None = None
        self.level_window: LevelTestWindow | None = None
        self._tick_source: int | None = None

    # -- lifecycle ------------------------------------------------------

    def run(self) -> int:
        if not self.lock.acquire():
            if self.lock.signal_owner(PING):
                print(t("app.already_running"))
                return 0
            print(t("app.already_running_locked"), flush=True)
            return 1

        autostart_module.sync(bool(self.config.get("autostart")))
        if not self.config.load_error and (
            not self.config.path.exists() or self.config.needs_migration
        ):
            # Write the settings out once, so the documented file exists and stays
            # complete when new settings are added in a later version.
            try:
                self.config.save()
            except OSError:
                pass
        if self.config.load_error:
            self.notifier.send(
                paths.APP_NAME,
                t("app.settings_unreadable", error=self.config.load_error),
                "warning",
            )

        self.tray = TrayIcon(
            on_toggle=self.toggle_recording,
            populate_menu=self.menu.populate,
            on_no_tray=self._warn_no_tray,
        )
        self._install_signal_handlers()
        self._apply_state(self.recorder.state)
        self._tick_source = GLib.timeout_add_seconds(1, self._tick)
        # Start buffering right away if a pre-roll is configured: it is only
        # useful if it is already full when the conversation starts.
        self.recorder.apply_preroll_settings()

        problems = self.recorder.preflight()
        if problems:
            self.notifier.send(paths.APP_NAME, "\n".join(problems), "warning")
        elif not self.quiet_start:
            self.notifier.send(t("app.running_title"), t("app.running_body"))

        if self.record_now:
            GLib.idle_add(self.toggle_recording)

        Gtk.main()
        return 0

    def _install_signal_handlers(self) -> None:
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_ping)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, self._on_toggle_signal)
        for signum in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_quit_signal)

    def _on_ping(self) -> bool:
        """A second start pinged us instead of opening another icon."""
        if self.tray is not None:
            self.tray.refresh_icon()
        self.notifier.send(
            t("app.running_title"),
            t("app.ping_body", state=state_label(self.recorder.state)),
        )
        return True

    def _on_toggle_signal(self) -> bool:
        self.toggle_recording()
        return True

    def _on_quit_signal(self) -> bool:
        self.quit()
        return False

    def quit(self) -> None:
        # Closes a running recording and gives up the idle buffer.
        self.recorder.shutdown()
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        for window in (self.settings_window, self.level_window):
            if window is not None:
                window.destroy()
        if self.tray is not None:
            self.tray.destroy()
        Gtk.main_quit()
        self.lock.release()

    # -- recorder callbacks (worker threads) ----------------------------

    def _on_state(self, state: State) -> None:
        GLib.idle_add(self._apply_state, state)

    def _on_notify(self, title: str, text: str, kind: str) -> None:
        GLib.idle_add(self._show_notification, title, text, kind)

    def _show_notification(self, title: str, text: str, kind: str) -> bool:
        if kind in ("warning", "error") and not self.config.get("notifications.on_error"):
            return False
        self.notifier.send(title, text, kind)
        return False

    # -- appearance -----------------------------------------------------

    def _apply_state(self, state: State) -> bool:
        tray = self.tray
        if tray is None:
            return False
        if state is State.RECORDING and self.config.get("tray.blink"):
            tray.start_blinking(int(self.config.get("tray.blink_interval_ms")))
        else:
            tray.stop_blinking()
            tray.set_icon(STATE_ICONS.get(state, "idle"))
        tray.set_tooltip(self._tooltip_text())
        return False

    def _tick(self) -> bool:
        if self.tray is not None:
            self.tray.set_tooltip(self._tooltip_text())
        return True

    def _tooltip_text(self) -> str:
        recorder = self.recorder
        state = recorder.state
        lines = [f"{paths.APP_NAME} – {state_label(state)}"]

        if recorder.active:
            mic, speaker = recorder.levels()
            lines[0] = (
                f"{paths.APP_NAME} – {state_label(state)} "
                f"{format_duration(recorder.elapsed_seconds)}"
            )
            lines.append(
                t(
                    "tooltip.levels",
                    mic=f"{mic.level_db:.0f}",
                    speaker=f"{speaker.level_db:.0f}",
                )
            )
            mic_label, speaker_label = recorder.device_labels()
            lines.append(t("tooltip.mic", label=shorten(mic_label, 40)))
            lines.append(t("tooltip.output", label=shorten(speaker_label, 40)))
            if recorder.files is not None:
                lines.append(recorder.files.audio.name)
        else:
            lines.append(t("tooltip.hint"))
            preroll = recorder.preroll_status()
            if preroll:
                lines.append(preroll)
            if recorder.last_error:
                lines.append(t("tooltip.last_error", error=recorder.last_error))
            elif recorder.last_result:
                lines.append(t("tooltip.last_result", result=recorder.last_result))

        free = free_megabytes(self.config.recordings_dir)
        if free is not None:
            lines.append(t("tooltip.free", size=paths.human_size(int(free * 1024 * 1024))))
        return "\n".join(lines)

    def _warn_no_tray(self) -> None:
        self.notifier.send(paths.APP_NAME, t("app.no_tray"), "warning")

    # -- actions --------------------------------------------------------

    def toggle_recording(self) -> bool:
        self.recorder.toggle()
        return False

    def toggle_pause(self) -> None:
        self.recorder.toggle_pause()

    def open_settings(self) -> None:
        if self.settings_window is not None:
            self.settings_window.present()
            return
        self.settings_window = SettingsWindow(
            self.config,
            on_saved=self._on_settings_saved,
            on_closed=self._on_settings_closed,
        )
        self.settings_window.show_all()

    def _on_settings_closed(self) -> None:
        self.settings_window = None

    def _on_settings_saved(self) -> None:
        # The menu and the tooltip are rebuilt anyway, so a language change needs
        # nothing more than this.
        self.config.apply_language()
        self._apply_state(self.recorder.state)
        self.recorder.apply_device_settings()
        self.recorder.apply_preroll_settings()

    def open_level_test(self) -> None:
        if self.level_window is not None:
            self.level_window.present()
            return
        self.level_window = LevelTestWindow(
            self.config, self.recorder, on_closed=self._on_level_closed
        )
        self.level_window.show_all()

    def _on_level_closed(self) -> None:
        self.level_window = None

    def open_recordings_folder(self) -> None:
        directory = self.config.recordings_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.notifier.send(paths.APP_NAME, t("app.folder_failed", error=exc), "error")
            return
        self.open_path(directory)

    def open_path(self, path: Path) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
        except GLib.Error as exc:
            self.notifier.send(
                paths.APP_NAME, t("app.open_failed", error=exc.message), "error"
            )

    def set_device(self, side: str, name: str) -> None:
        if self.config.get(f"devices.{side}") == name:
            return
        self.config.set(f"devices.{side}", name)
        self._save_config()
        self.recorder.apply_device_settings()

    def set_follow_default(self, enabled: bool) -> None:
        if bool(self.config.get("devices.follow_default")) == enabled:
            return
        self.config.set("devices.follow_default", enabled)
        self._save_config()
        self.recorder.apply_device_settings()

    def _save_config(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            self.notifier.send(
                paths.APP_NAME, t("app.settings_not_saved", error=exc), "error"
            )
