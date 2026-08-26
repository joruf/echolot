"""The right click menu, rebuilt every time it opens.

Rebuilding instead of updating means the menu always shows the true current
state - which devices are actually in use, which recordings exist - without any
invalidation logic. While the menu is open, the two level rows keep updating, so
it doubles as a live check that both sides are being heard.
"""

from __future__ import annotations

from html import escape

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import paths
from ..audio import devices as devices_module
from ..audio.devices import ALL
from ..config import AUTO
from ..i18n import t
from ..session import State, format_duration

LEVEL_REFRESH_MS = 200
BAR_WIDTH = 12
BAR_FLOOR_DB = -60.0


def state_label(state: State) -> str:
    """Translated name of a recorder state."""
    return t(f"state.{state.value}")


def level_bar(level_db: float, width: int = BAR_WIDTH) -> str:
    """Text meter, so the level fits into a menu row."""
    if level_db <= BAR_FLOOR_DB:
        filled = 0
    else:
        filled = min(width, max(0, round((level_db - BAR_FLOOR_DB) / -BAR_FLOOR_DB * width)))
    return "▮" * filled + "▯" * (width - filled)


def monospace_item(text: str) -> Gtk.MenuItem:
    """A non-clickable row in a fixed width font."""
    item = Gtk.MenuItem()
    label = Gtk.Label(xalign=0.0)
    label.set_markup(f"<tt>{escape(text)}</tt>")
    item.add(label)
    item.set_sensitive(False)
    return item


class AppMenu:
    """Builds the tray menu for an `EcholotApp`."""

    def __init__(self, app) -> None:
        self.app = app
        self._level_items: tuple[Gtk.MenuItem, Gtk.MenuItem] | None = None
        self._level_source: int | None = None

    # -- assembly -------------------------------------------------------

    def populate(self, menu: Gtk.Menu) -> None:
        app = self.app
        recorder = app.recorder
        state = recorder.state

        header = state_label(state)
        if recorder.active:
            header = f"{header} · {format_duration(recorder.elapsed_seconds)}"
        menu.append(monospace_item(t("menu.header", state=header)))
        preroll = recorder.preroll_status()
        if preroll and not recorder.active:
            menu.append(monospace_item(preroll))
        menu.append(Gtk.SeparatorMenuItem())

        toggle = Gtk.MenuItem(label=t("menu.stop") if recorder.active else t("menu.start"))
        toggle.connect("activate", lambda _item: app.toggle_recording())
        menu.append(toggle)

        pause = Gtk.MenuItem(
            label=t("menu.resume") if state is State.PAUSED else t("menu.pause")
        )
        pause.set_sensitive(recorder.active)
        pause.connect("activate", lambda _item: app.toggle_pause())
        menu.append(pause)

        menu.append(Gtk.SeparatorMenuItem())
        self._append_levels(menu)

        levels = Gtk.MenuItem(label=t("menu.levels"))
        levels.connect("activate", lambda _item: app.open_levels())
        menu.append(levels)

        menu.append(Gtk.SeparatorMenuItem())
        menu.append(self._devices_item())
        menu.append(self._recent_item())

        folder = Gtk.MenuItem(label=t("menu.open_folder"))
        folder.connect("activate", lambda _item: app.open_recordings_folder())
        menu.append(folder)

        menu.append(Gtk.SeparatorMenuItem())
        settings = Gtk.MenuItem(label=t("menu.settings"))
        settings.connect("activate", lambda _item: app.open_settings())
        menu.append(settings)

        quit_item = Gtk.MenuItem(label=t("menu.quit"))
        quit_item.connect("activate", lambda _item: app.quit())
        menu.append(quit_item)

        menu.connect("hide", self._on_hide)
        self._start_level_refresh()

    # -- levels ---------------------------------------------------------

    def _append_levels(self, menu: Gtk.Menu) -> None:
        mic_item = monospace_item("")
        speaker_item = monospace_item("")
        self._level_items = (mic_item, speaker_item)
        self._update_levels()
        menu.append(mic_item)
        menu.append(speaker_item)

    def _level_text(self, caption: str, metrics) -> str:
        if not self.app.recorder.active:
            return f"{caption:10} {'▯' * BAR_WIDTH}   {t('common.none')}"
        return f"{caption:10} {level_bar(metrics.level_db)} {metrics.level_db:6.0f} dB"

    def _update_levels(self) -> bool:
        if self._level_items is None:
            return False
        mic_metrics, speaker_metrics = self.app.recorder.levels()
        for item, text in zip(
            self._level_items,
            (
                self._level_text(t("common.you"), mic_metrics),
                self._level_text(t("common.other"), speaker_metrics),
            ),
        ):
            child = item.get_child()
            if child is not None:
                child.set_markup(f"<tt>{escape(text)}</tt>")
        return True

    def _start_level_refresh(self) -> None:
        self._stop_level_refresh()
        self._level_source = GLib.timeout_add(LEVEL_REFRESH_MS, self._update_levels)

    def _stop_level_refresh(self) -> None:
        if self._level_source is not None:
            GLib.source_remove(self._level_source)
            self._level_source = None

    def _on_hide(self, _menu: Gtk.Menu) -> None:
        self._stop_level_refresh()
        self._level_items = None

    # -- submenus -------------------------------------------------------

    def _devices_item(self) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=t("menu.devices"))
        submenu = Gtk.Menu()
        sources = devices_module.list_sources()

        submenu.append(self._device_group(t("menu.device_mic"), "mic", sources, monitors=False))
        submenu.append(
            self._device_group(t("menu.device_output"), "speaker", sources, monitors=True)
        )
        submenu.append(Gtk.SeparatorMenuItem())

        follow = Gtk.CheckMenuItem(label=t("menu.follow_default"))
        follow.set_active(bool(self.app.config.get("devices.follow_default")))
        follow.connect("toggled", lambda widget: self.app.set_follow_default(widget.get_active()))
        submenu.append(follow)

        if self.app.recorder.active:
            mic_label, speaker_label = self.app.recorder.device_labels()
            submenu.append(Gtk.SeparatorMenuItem())
            submenu.append(monospace_item(t("menu.device_in_use", label=shorten(mic_label))))
            submenu.append(
                monospace_item(t("menu.device_in_use", label=shorten(speaker_label)))
            )

        item.set_submenu(submenu)
        return item

    def _device_group(
        self, caption: str, side: str, sources: list, *, monitors: bool
    ) -> Gtk.MenuItem:
        """Everything, the system default, or one device by name.

        Every source is offered for either side, not only the expected kind: when
        the other side's audio arrives on a real input - a virtual cable, a line
        in - that has to be selectable.
        """
        item = Gtk.MenuItem(label=caption)
        submenu = Gtk.Menu()
        current = self.app.config.get(f"devices.{side}", ALL)

        everything = Gtk.RadioMenuItem(label=t("devices.all"))
        everything.set_active(current == ALL)
        everything.connect("toggled", self._on_device_chosen, side, ALL)
        submenu.append(everything)
        group = everything

        auto = Gtk.RadioMenuItem(
            label=t("menu.device_auto") if side == "mic" else t("menu.device_auto_output"),
            group=group,
        )
        auto.set_active(current == AUTO)
        auto.connect("toggled", self._on_device_chosen, side, AUTO)
        submenu.append(auto)

        preferred = [d for d in sources if d.is_monitor is monitors]
        others = [d for d in sources if d.is_monitor is not monitors]
        for index, block in enumerate((preferred, others)):
            if not block:
                continue
            submenu.append(Gtk.SeparatorMenuItem())
            for device in block:
                entry = Gtk.RadioMenuItem(label=shorten(device.label()), group=group)
                entry.set_active(current == device.name)
                entry.connect("toggled", self._on_device_chosen, side, device.name)
                submenu.append(entry)

        item.set_submenu(submenu)
        return item

    def _on_device_chosen(self, widget: Gtk.RadioMenuItem, side: str, name: str) -> None:
        if widget.get_active():
            self.app.set_device(side, name)

    def _recent_item(self) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=t("menu.recent"))
        submenu = Gtk.Menu()
        limit = int(self.app.config.get("recent_limit", 5))
        recordings = paths.list_recordings(self.app.config.recordings_dir, limit=limit)

        if not recordings:
            submenu.append(monospace_item(t("menu.recent_empty")))
        for recording in recordings:
            entry = Gtk.MenuItem(label=recording.label())
            actions = Gtk.Menu()

            play = Gtk.MenuItem(label=t("menu.play"))
            play.connect("activate", lambda _i, r=recording: self.app.open_path(r.audio))
            actions.append(play)

            folder = Gtk.MenuItem(label=t("menu.open_folder"))
            folder.connect("activate", lambda _i, r=recording: self.app.open_path(r.audio.parent))
            actions.append(folder)

            log = Gtk.MenuItem(label=t("menu.open_log"))
            log.set_sensitive(recording.has_log)
            if recording.has_log:
                log.connect("activate", lambda _i, r=recording: self.app.open_path(r.log))
            actions.append(log)

            entry.set_submenu(actions)
            submenu.append(entry)

        item.set_submenu(submenu)
        return item


def shorten(text: str, limit: int = 48) -> str:
    """Device descriptions are long enough to blow up a menu."""
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"
