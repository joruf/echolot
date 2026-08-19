"""Settings dialog for everything that is otherwise only in settings.json."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from .. import autostart, i18n, paths
from ..audio import devices as devices_module
from ..audio.devices import ALL
from ..config import AUTO, MAX_PREROLL_MINUTES
from ..i18n import t
from .menu import shorten

FORMAT_KEYS = (("opus", "settings.format_opus"), ("flac", "settings.format_flac"), ("wav", "settings.format_wav"))
LAYOUT_KEYS = (("mix", "settings.layout_mix"), ("split", "settings.layout_split"))


def frame(title: str, grid: Gtk.Grid) -> Gtk.Frame:
    box = Gtk.Frame(label=f" {title} ")
    grid.set_border_width(10)
    grid.set_column_spacing(10)
    grid.set_row_spacing(6)
    box.add(grid)
    return box


def spin(low: float, high: float, step: float, value: float, digits: int = 0) -> Gtk.SpinButton:
    button = Gtk.SpinButton.new_with_range(low, high, step)
    button.set_digits(digits)
    button.set_value(value)
    button.set_halign(Gtk.Align.START)
    return button


def label(text: str) -> Gtk.Label:
    return Gtk.Label(label=text, xalign=0.0)


class SettingsWindow(Gtk.Window):
    def __init__(
        self,
        config,
        on_saved: Callable[[], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(title=t("settings.title"))
        self.config = config
        self.on_saved = on_saved
        self.on_closed = on_closed
        self.set_default_size(560, -1)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(outer)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(520)
        scroller.set_propagate_natural_height(True)
        outer.pack_start(scroller, True, True, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        scroller.add(content)

        content.pack_start(self._language_frame(), False, False, 0)
        content.pack_start(self._storage_frame(), False, False, 0)
        content.pack_start(self._audio_frame(), False, False, 0)
        content.pack_start(self._preroll_frame(), False, False, 0)
        content.pack_start(self._devices_frame(), False, False, 0)
        content.pack_start(self._tray_frame(), False, False, 0)
        content.pack_start(self._speech_frame(), False, False, 0)
        content.pack_start(self._disk_frame(), False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label=t("common.cancel"))
        cancel.connect("clicked", lambda _button: self.close())
        save = Gtk.Button(label=t("common.save"))
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._on_save)
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(save, False, False, 0)
        outer.pack_start(buttons, False, False, 0)

        self.connect("destroy", lambda _widget: self.on_closed and self.on_closed())

    # -- sections -------------------------------------------------------

    def _language_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.language_combo = Gtk.ComboBoxText()
        for code, name in i18n.available_languages():
            self.language_combo.append(code, name)
        self.language_combo.set_active_id(self.config.language)
        grid.attach(label(t("settings.language")), 0, 0, 1, 1)
        grid.attach(self.language_combo, 1, 0, 2, 1)

        hint = Gtk.Label(xalign=0.0)
        hint.set_markup(t("settings.language_hint"))
        grid.attach(hint, 0, 1, 3, 1)
        return frame(t("settings.language_frame"), grid)

    def _storage_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.dir_entry = Gtk.Entry(text=str(self.config.recordings_dir), hexpand=True)
        choose = Gtk.Button(label=t("common.choose"))
        choose.connect("clicked", self._on_choose_dir)
        grid.attach(label(t("settings.folder")), 0, 0, 1, 1)
        grid.attach(self.dir_entry, 1, 0, 1, 1)
        grid.attach(choose, 2, 0, 1, 1)

        hint = Gtk.Label(xalign=0.0)
        hint.set_markup(t("settings.naming_hint"))
        grid.attach(hint, 0, 1, 3, 1)

        self.autostart_check = Gtk.CheckButton(label=t("settings.autostart"))
        self.autostart_check.set_active(bool(self.config.get("autostart")))
        grid.attach(self.autostart_check, 0, 2, 3, 1)

        self.recent_spin = spin(1, 25, 1, int(self.config.get("recent_limit", 5)))
        grid.attach(label(t("settings.recent_limit")), 0, 3, 2, 1)
        grid.attach(self.recent_spin, 2, 3, 1, 1)
        return frame(t("settings.storage"), grid)

    def _audio_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.layout_combo = Gtk.ComboBoxText()
        for key, text_key in LAYOUT_KEYS:
            self.layout_combo.append(key, t(text_key))
        self.layout_combo.set_active_id(self.config.audio_layout)
        self.layout_combo.connect("changed", self._on_layout_changed)
        grid.attach(label(t("settings.tracks")), 0, 0, 1, 1)
        grid.attach(self.layout_combo, 1, 0, 2, 1)

        self.format_combo = Gtk.ComboBoxText()
        for key, text_key in FORMAT_KEYS:
            self.format_combo.append(key, t(text_key))
        self.format_combo.set_active_id(self.config.audio_format)
        self.format_combo.connect("changed", self._on_format_changed)
        grid.attach(label(t("settings.format")), 0, 1, 1, 1)
        grid.attach(self.format_combo, 1, 1, 2, 1)

        self.bitrate_spin = spin(16, 512, 8, int(self.config.get("audio.bitrate_kbps")))
        grid.attach(label(t("settings.bitrate")), 0, 2, 2, 1)
        grid.attach(self.bitrate_spin, 2, 2, 1, 1)
        self._on_format_changed(self.format_combo)

        hint = Gtk.Label(xalign=0.0)
        hint.set_markup(t("settings.tracks_hint"))
        grid.attach(hint, 0, 3, 3, 1)
        return frame(t("settings.recording"), grid)

    def _preroll_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.preroll_combo = Gtk.ComboBoxText()
        self.preroll_combo.append("0", t("settings.preroll_off"))
        for minutes in range(1, MAX_PREROLL_MINUTES + 1):
            self.preroll_combo.append(
                str(minutes),
                t("settings.preroll_minutes_one")
                if minutes == 1
                else t("settings.preroll_minutes_many", minutes=minutes),
            )
        self.preroll_combo.set_active_id(str(int(self.config.get("audio.preroll_minutes", 0))))
        self.preroll_combo.connect("changed", self._on_preroll_changed)
        grid.attach(label(t("settings.preroll")), 0, 0, 1, 1)
        grid.attach(self.preroll_combo, 1, 0, 2, 1)

        self.preroll_hint = Gtk.Label(xalign=0.0)
        grid.attach(self.preroll_hint, 0, 1, 3, 1)
        self._on_preroll_changed(self.preroll_combo)
        return frame(t("settings.preroll_frame"), grid)

    def _devices_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        sources = devices_module.list_sources()

        # Every source is offered for either side: the other side's audio can
        # arrive on a real input, for instance through a virtual cable.
        self.mic_combo = self._device_combo("devices.mic", sources, monitors=False)
        self.speaker_combo = self._device_combo("devices.speaker", sources, monitors=True)

        grid.attach(label(t("settings.device_mic")), 0, 0, 1, 1)
        grid.attach(self.mic_combo, 1, 0, 2, 1)
        grid.attach(label(t("settings.device_output")), 0, 1, 1, 1)
        grid.attach(self.speaker_combo, 1, 1, 2, 1)

        self.follow_check = Gtk.CheckButton(label=t("settings.follow_default"))
        self.follow_check.set_active(bool(self.config.get("devices.follow_default")))
        grid.attach(self.follow_check, 0, 2, 3, 1)
        return frame(t("settings.devices"), grid)

    def _device_combo(self, key: str, sources: list, *, monitors: bool) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        combo.append(ALL, t("devices.all"))
        combo.append(
            AUTO, t("settings.device_auto_output") if monitors else t("settings.device_auto")
        )
        for device in sorted(sources, key=lambda d: d.is_monitor is not monitors):
            combo.append(device.name, shorten(device.label(), 60))
        combo.set_active_id(self._active_id(self.config.get(key), sources))
        return combo

    def _active_id(self, value, sources) -> str:
        if value in (ALL, AUTO):
            return str(value)
        if not any(device.name == value for device in sources):
            return ALL
        return str(value)

    def _tray_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.blink_check = Gtk.CheckButton(label=t("settings.blink"))
        self.blink_check.set_active(bool(self.config.get("tray.blink")))
        grid.attach(self.blink_check, 0, 0, 3, 1)

        self.blink_spin = spin(200, 5000, 100, int(self.config.get("tray.blink_interval_ms")))
        grid.attach(label(t("settings.blink_interval")), 0, 1, 2, 1)
        grid.attach(self.blink_spin, 2, 1, 1, 1)

        self.notify_start = Gtk.CheckButton(label=t("settings.notify_start"))
        self.notify_start.set_active(bool(self.config.get("notifications.on_start")))
        self.notify_stop = Gtk.CheckButton(label=t("settings.notify_stop"))
        self.notify_stop.set_active(bool(self.config.get("notifications.on_stop")))
        self.notify_error = Gtk.CheckButton(label=t("settings.notify_error"))
        self.notify_error.set_active(bool(self.config.get("notifications.on_error")))
        grid.attach(self.notify_start, 0, 2, 3, 1)
        grid.attach(self.notify_stop, 0, 3, 3, 1)
        grid.attach(self.notify_error, 0, 4, 3, 1)

        self.silent_spin = spin(
            0, 600, 5, int(self.config.get("warnings.silent_side_seconds"))
        )
        grid.attach(label(t("settings.silent_warning")), 0, 5, 2, 1)
        grid.attach(self.silent_spin, 2, 5, 1, 1)

        hint = Gtk.Label(xalign=0.0)
        hint.set_markup(t("settings.silent_warning_hint"))
        grid.attach(hint, 0, 6, 3, 1)
        return frame(t("settings.tray"), grid)

    def _speech_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.threshold_spin = spin(-90, -5, 1, float(self.config.get("vad.threshold_db")))
        self.min_segment_spin = spin(0, 5000, 50, int(self.config.get("vad.min_segment_ms")))
        self.hangover_spin = spin(0, 5000, 50, int(self.config.get("vad.hangover_ms")))
        self.adaptive_check = Gtk.CheckButton(label=t("settings.adaptive"))
        self.adaptive_check.set_active(bool(self.config.get("vad.adaptive_noise_floor")))

        grid.attach(label(t("settings.threshold")), 0, 0, 2, 1)
        grid.attach(self.threshold_spin, 2, 0, 1, 1)
        grid.attach(label(t("settings.min_segment")), 0, 1, 2, 1)
        grid.attach(self.min_segment_spin, 2, 1, 1, 1)
        grid.attach(label(t("settings.hangover")), 0, 2, 2, 1)
        grid.attach(self.hangover_spin, 2, 2, 1, 1)
        grid.attach(self.adaptive_check, 0, 3, 3, 1)

        hint = Gtk.Label(xalign=0.0)
        hint.set_markup(t("settings.speech_hint"))
        grid.attach(hint, 0, 4, 3, 1)
        return frame(t("settings.speech"), grid)

    def _disk_frame(self) -> Gtk.Frame:
        grid = Gtk.Grid()
        self.warn_spin = spin(0, 1_000_000, 100, int(self.config.get("disk.warn_mb")))
        self.stop_spin = spin(0, 1_000_000, 100, int(self.config.get("disk.stop_mb")))
        self.interval_spin = spin(1, 3600, 5, int(self.config.get("disk.check_interval_s")))
        grid.attach(label(t("settings.warn_below")), 0, 0, 2, 1)
        grid.attach(self.warn_spin, 2, 0, 1, 1)
        grid.attach(label(t("settings.stop_below")), 0, 1, 2, 1)
        grid.attach(self.stop_spin, 2, 1, 1, 1)
        grid.attach(label(t("settings.check_interval")), 0, 2, 2, 1)
        grid.attach(self.interval_spin, 2, 2, 1, 1)
        return frame(t("settings.disk"), grid)

    # -- actions --------------------------------------------------------

    def _on_format_changed(self, combo: Gtk.ComboBoxText) -> None:
        self.bitrate_spin.set_sensitive(combo.get_active_id() == "opus")

    def _on_layout_changed(self, _combo: Gtk.ComboBoxText) -> None:
        # Two channels double the pre-roll's memory, so the estimate has to follow.
        self._on_preroll_changed(self.preroll_combo)

    def _selected_channels(self) -> int:
        return 2 if self.layout_combo.get_active_id() == "split" else 1

    def _on_preroll_changed(self, combo: Gtk.ComboBoxText) -> None:
        if not hasattr(self, "preroll_hint"):
            return
        try:
            minutes = int(combo.get_active_id() or 0)
        except ValueError:
            minutes = 0
        if minutes <= 0:
            self.preroll_hint.set_markup(t("settings.preroll_hint_off"))
            return
        per_minute = 60 * int(self.config.get("audio.sample_rate")) * 2 * self._selected_channels()
        self.preroll_hint.set_markup(
            t(
                "settings.preroll_hint_on",
                minutes=minutes,
                memory=paths.human_size(minutes * per_minute),
            )
        )

    def _on_choose_dir(self, _button) -> None:
        dialog = Gtk.FileChooserDialog(
            title=t("settings.folder_dialog"),
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            t("common.cancel"),
            Gtk.ResponseType.CANCEL,
            t("settings.folder_choose"),
            Gtk.ResponseType.ACCEPT,
        )
        dialog.set_filename(self.dir_entry.get_text())
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.dir_entry.set_text(dialog.get_filename() or self.dir_entry.get_text())
        dialog.destroy()

    def _on_save(self, _button) -> None:
        directory = self.dir_entry.get_text().strip()
        default_dir = str(paths.default_recordings_dir())
        self.config.update(
            {
                "language": self.language_combo.get_active_id() or i18n.DEFAULT_LANGUAGE,
                "recordings_dir": None if not directory or directory == default_dir else directory,
                "recent_limit": int(self.recent_spin.get_value()),
                "audio.format": self.format_combo.get_active_id() or "opus",
                "audio.layout": self.layout_combo.get_active_id() or "mix",
                "audio.preroll_minutes": int(self.preroll_combo.get_active_id() or 0),
                "audio.bitrate_kbps": int(self.bitrate_spin.get_value()),
                "devices.mic": self.mic_combo.get_active_id() or AUTO,
                "devices.speaker": self.speaker_combo.get_active_id() or AUTO,
                "devices.follow_default": self.follow_check.get_active(),
                "tray.blink": self.blink_check.get_active(),
                "tray.blink_interval_ms": int(self.blink_spin.get_value()),
                "notifications.on_start": self.notify_start.get_active(),
                "notifications.on_stop": self.notify_stop.get_active(),
                "notifications.on_error": self.notify_error.get_active(),
                "warnings.silent_side_seconds": int(self.silent_spin.get_value()),
                "vad.threshold_db": float(self.threshold_spin.get_value()),
                "vad.min_segment_ms": int(self.min_segment_spin.get_value()),
                "vad.hangover_ms": int(self.hangover_spin.get_value()),
                "vad.adaptive_noise_floor": self.adaptive_check.get_active(),
                "disk.warn_mb": int(self.warn_spin.get_value()),
                "disk.stop_mb": int(self.stop_spin.get_value()),
                "disk.check_interval_s": int(self.interval_spin.get_value()),
                "autostart": self.autostart_check.get_active(),
            }
        )
        try:
            self.config.save()
        except OSError as exc:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=t("settings.save_failed"),
                secondary_text=str(exc),
            )
            dialog.run()
            dialog.destroy()
            return
        autostart.sync(bool(self.config.get("autostart")))
        if self.on_saved is not None:
            self.on_saved()
        self.close()
