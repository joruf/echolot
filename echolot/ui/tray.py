"""The tray icon: double click records, right click opens the menu, and while
recording the icon blinks.

Backend choice is dictated by the double click requirement. AppIndicator - the
usual modern choice - delivers no click events at all, only a menu, so it cannot
be used here. `Gtk.StatusIcon` does deliver real button events and is what the
Cinnamon systray applet carries, so it is the primary backend, with
`XApp.StatusIcon` as fallback if the icon never gets embedded.

Double clicks are detected from single click events on purpose, identically in
both backends: GTK also emits `_2BUTTON_PRESS`, but acting on both that and the
plain presses would fire twice.
"""

from __future__ import annotations

import warnings
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp
except (ImportError, ValueError):  # pragma: no cover - depends on the desktop
    XApp = None

from .. import paths

# Gtk.StatusIcon is deprecated, and we use it deliberately - it is the only tray
# API that reports clicks. Silencing just these warnings keeps the session log of
# an autostarted program readable.
warnings.filterwarnings(
    "ignore", message=r"Gtk\.StatusIcon.*deprecated", category=DeprecationWarning
)

DEFAULT_DOUBLE_CLICK_MS = 400
EMBED_CHECK_DELAY_MS = 3000
FALLBACK_ICON = "media-record"


def double_click_interval_ms() -> int:
    """The desktop's own double click time, so the tray feels like everything else."""
    try:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            value = settings.get_property("gtk-double-click-time")
            if value:
                return int(value)
    except (TypeError, AttributeError):
        pass
    return DEFAULT_DOUBLE_CLICK_MS


class DoubleClickDetector:
    """Turns a stream of single clicks into double click callbacks."""

    def __init__(self, on_double: Callable[[], None], interval_ms: int | None = None) -> None:
        self.on_double = on_double
        self.interval_ms = interval_ms or double_click_interval_ms()
        self._source: int | None = None

    def click(self) -> None:
        if self._source is not None:
            self._cancel()
            self.on_double()
            return
        self._source = GLib.timeout_add(self.interval_ms, self._expire)

    def _expire(self) -> bool:
        self._source = None
        return False

    def _cancel(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None


def load_pixbuf(name: str, size: int) -> GdkPixbuf.Pixbuf | None:
    path = paths.icon_file(name)
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
    except GLib.Error:
        return None


class _GtkStatusIconBackend:
    """Legacy tray icon - the only widely carried one with click events."""

    kind = "gtk-status-icon"

    def __init__(self, owner: "TrayIcon") -> None:
        self.owner = owner
        self.size = 22
        self.icon = Gtk.StatusIcon()
        self.icon.set_name(paths.APP_ID)
        self.icon.set_title(paths.APP_NAME)
        self.icon.connect("button-press-event", self._on_button_press)
        self.icon.connect("popup-menu", self._on_popup_menu)
        self.icon.connect("size-changed", self._on_size_changed)
        # Give it a picture before showing it, so no empty icon flashes in the panel.
        self.apply_icon("idle")
        self.icon.set_visible(True)

    def _on_button_press(self, _icon, event) -> bool:
        # Only plain presses; the synthetic _2BUTTON_PRESS would double count.
        if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.owner.handle_click()
            return True
        return False

    def _on_popup_menu(self, icon, button: int, activate_time: int) -> None:
        menu = self.owner.menu
        menu.show_all()
        try:
            menu.popup_at_pointer(None)
        except (AttributeError, TypeError):  # pragma: no cover - very old GTK
            menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def _on_size_changed(self, _icon, size: int) -> bool:
        self.size = max(16, int(size))
        self.owner.refresh_icon()
        return True

    def apply_icon(self, name: str) -> None:
        pixbuf = load_pixbuf(name, self.size)
        if pixbuf is not None:
            self.icon.set_from_pixbuf(pixbuf)
        else:
            self.icon.set_from_icon_name(FALLBACK_ICON)

    def set_tooltip(self, text: str) -> None:
        self.icon.set_tooltip_text(text)

    def attach_menu(self, menu: Gtk.Menu) -> None:
        """Nothing to do: the menu is popped up from the signal handler."""

    def is_embedded(self) -> bool:
        return bool(self.icon.is_embedded())

    def destroy(self) -> None:
        self.icon.set_visible(False)


class _XAppBackend:  # pragma: no cover - only used when the systray is missing
    """Cinnamon's own status icon, used when nothing embeds the legacy icon."""

    kind = "xapp"

    def __init__(self, owner: "TrayIcon") -> None:
        self.owner = owner
        self.icon = XApp.StatusIcon()
        self.icon.set_name(paths.APP_NAME)
        self.icon.connect("button-press-event", self._on_button_press)

    def _on_button_press(self, _icon, _x, _y, button: int, _time, _position) -> None:
        if button == 1:
            self.owner.handle_click()

    def apply_icon(self, name: str) -> None:
        path = paths.icon_file(name)
        self.icon.set_icon_name(str(path) if path.exists() else FALLBACK_ICON)

    def set_tooltip(self, text: str) -> None:
        self.icon.set_tooltip_text(text)

    def attach_menu(self, menu: Gtk.Menu) -> None:
        # XApp shows this itself on right click and repopulates it through the
        # menu's "show" signal.
        self.icon.set_secondary_menu(menu)

    def is_embedded(self) -> bool:
        return True

    def destroy(self) -> None:
        self.icon.set_visible(False)


class TrayIcon:
    """Icon, blinking, tooltip and the menu that belongs to it."""

    def __init__(
        self,
        *,
        on_toggle: Callable[[], None],
        populate_menu: Callable[[Gtk.Menu], None],
        on_no_tray: Callable[[], None] | None = None,
    ) -> None:
        self.populate_menu = populate_menu
        self.on_no_tray = on_no_tray
        self._detector = DoubleClickDetector(on_toggle)
        self._icon_name = "idle"
        self._blink_names: tuple[str, str] | None = None
        self._blink_source: int | None = None
        self._blink_phase = False

        self.menu = Gtk.Menu()
        self.menu.connect("show", self._on_menu_show)

        self.backend = _GtkStatusIconBackend(self)
        self.backend.attach_menu(self.menu)
        self.refresh_icon()
        GLib.timeout_add(EMBED_CHECK_DELAY_MS, self._check_embedded)

    # -- events ---------------------------------------------------------

    def handle_click(self) -> None:
        self._detector.click()

    def _on_menu_show(self, menu: Gtk.Menu) -> None:
        for child in menu.get_children():
            menu.remove(child)
        self.populate_menu(menu)
        menu.show_all()

    def _check_embedded(self) -> bool:
        """Switch backends if nothing picked the icon up."""
        if self.backend.is_embedded():
            return False
        if XApp is not None and self.backend.kind != "xapp":
            self.backend.destroy()
            self.backend = _XAppBackend(self)
            self.backend.attach_menu(self.menu)
            self.refresh_icon()
            return False
        if self.on_no_tray is not None:
            self.on_no_tray()
        return False

    # -- appearance -----------------------------------------------------

    def refresh_icon(self) -> None:
        name = self._icon_name
        if self._blink_names is not None:
            name = self._blink_names[1 if self._blink_phase else 0]
        self.backend.apply_icon(name)

    def set_icon(self, name: str) -> None:
        self._icon_name = name
        self.refresh_icon()

    def set_tooltip(self, text: str) -> None:
        self.backend.set_tooltip(text)

    def start_blinking(self, interval_ms: int, names: tuple[str, str] = ("rec-on", "rec-off")) -> None:
        self.stop_blinking()
        self._blink_names = names
        self._blink_phase = False
        self.refresh_icon()
        self._blink_source = GLib.timeout_add(max(150, int(interval_ms)), self._blink_tick)

    def _blink_tick(self) -> bool:
        self._blink_phase = not self._blink_phase
        self.refresh_icon()
        return True

    def stop_blinking(self) -> None:
        if self._blink_source is not None:
            GLib.source_remove(self._blink_source)
            self._blink_source = None
        self._blink_names = None
        self._blink_phase = False

    def destroy(self) -> None:
        self.stop_blinking()
        self.backend.destroy()
