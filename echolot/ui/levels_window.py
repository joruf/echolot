"""Levels and devices - one row per device, with the tick that decides.

The window answers two questions at once, and they belong together: *what is
arriving on each device right now*, and *which of them go into the recording*.
Seeing the bar move is the only reliable way to tell which device carries the
other side, so the tick sits directly next to it instead of in a separate dialog.

Every listed device gets its own capture while the window is open, including
during a recording. Reading the recording's own per-side level instead would be
cheaper, but a side can be several devices summed - then the row would show the
sum rather than that device, which is exactly the confusion this window exists to
end. The processes stop when the window closes.
"""

from __future__ import annotations

from array import array
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..audio import capture, devices as devices_module
from ..audio.devices import ALL
from ..audio.mixer import ChannelMetrics, measure
from ..i18n import t
from ..speechlog import MIC, SPEAKER
from .menu import shorten

REFRESH_MS = 100
FLOOR_DB = -60.0
# Bars fall back slowly so a short word stays visible instead of flickering.
DECAY_DB_PER_TICK = 3.0
LABEL_CHARS = 46

#: Which config key and which kind of device each group covers.
GROUPS = (
    (MIC, "devices.mic", False, "levels.group_inputs", "levels.group_inputs_hint"),
    (SPEAKER, "devices.speaker", True, "levels.group_outputs", "levels.group_outputs_hint"),
)


def normalise(level_db: float) -> float:
    return max(0.0, min(1.0, (level_db - FLOOR_DB) / -FLOOR_DB))


class DeviceRow:
    """One device: the tick, the live bar, the number."""

    def __init__(self, device, on_toggled: Callable[[], None], foreign: bool = False) -> None:
        self.device = device
        self.shown_db = FLOOR_DB
        self.capture: capture.CaptureProcess | None = None
        #: True when this device is not the kind the group is about - an input
        #: offered for the other side, for instance. Listed anyway, because the
        #: other side's audio does not always arrive on a monitor.
        self.foreign = foreign

        caption = shorten(device.label(), LABEL_CHARS)
        if foreign:
            kind = t("levels.kind_input") if not device.is_monitor else t("levels.kind_output")
            caption = f"{caption}  [{kind}]"
        self.check = Gtk.CheckButton(label=caption)
        self.check.set_tooltip_text(device.name)
        self._handler = self.check.connect("toggled", lambda _button: on_toggled())

        self.bar = Gtk.LevelBar.new_for_interval(0.0, 1.0)
        self.bar.set_hexpand(True)
        self.bar.set_size_request(-1, 16)
        self.value = Gtk.Label(label=t("common.none"), xalign=1.0, width_chars=8)

    def set_active_quietly(self, active: bool) -> None:
        """Reflect the config without the UI writing it straight back."""
        with_handler = self._handler
        self.check.handler_block(with_handler)
        self.check.set_active(active)
        self.check.handler_unblock(with_handler)

    def attach(self, grid: Gtk.Grid, row: int) -> None:
        grid.attach(self.check, 0, row, 1, 1)
        grid.attach(self.bar, 1, row, 1, 1)
        grid.attach(self.value, 2, row, 1, 1)

    # -- level ----------------------------------------------------------

    def measure_now(self) -> float | None:
        """Loudest block since the last tick, or None if nothing arrived."""
        process = self.capture
        if process is None:
            return None
        peak, total, count = 0, 0.0, 0
        while True:
            block = process.read(timeout=0)
            if block is None:
                break
            samples = array("h")
            samples.frombytes(block)
            block_peak, block_mav = measure(samples)
            peak = max(peak, block_peak)
            total += block_mav
            count += 1
        if count == 0:
            return None
        return ChannelMetrics(peak, total / count, True).level_db

    def refresh(self) -> None:
        level_db = self.measure_now()
        if level_db is None:
            self.shown_db = max(FLOOR_DB, self.shown_db - DECAY_DB_PER_TICK)
        else:
            self.shown_db = max(level_db, self.shown_db - DECAY_DB_PER_TICK)
        self.bar.set_value(normalise(self.shown_db))
        self.value.set_text(
            t("common.none") if self.shown_db <= FLOOR_DB else f"{self.shown_db:5.0f} dB"
        )


class LevelsWindow(Gtk.Window):
    def __init__(self, config, recorder, on_closed: Callable[[], None] | None = None) -> None:
        super().__init__(title=t("levels.title"))
        self.config = config
        self.recorder = recorder
        self.on_closed = on_closed
        self.set_default_size(600, -1)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)

        self._rows: dict[str, list[DeviceRow]] = {MIC: [], SPEAKER: []}
        self._all_checks: dict[str, Gtk.CheckButton] = {}
        self._all_handlers: dict[str, int] = {}
        self._warnings: dict[str, Gtk.Label] = {}
        self._grids: dict[str, Gtk.Grid] = {}
        self._source: int | None = None
        self._loading = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        heading = Gtk.Label(xalign=0.0)
        heading.set_markup(t("levels.heading"))
        heading.set_line_wrap(True)
        box.pack_start(heading, False, False, 0)

        for side, key, monitors, caption, hint in GROUPS:
            box.pack_start(self._group(side, key, caption, hint), False, False, 0)

        self._hint = Gtk.Label(xalign=0.0)
        self._hint.set_line_wrap(True)
        box.pack_start(self._hint, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rescan = Gtk.Button(label=t("levels.rescan"))
        rescan.connect("clicked", lambda _button: self.reload())
        buttons.pack_start(rescan, False, False, 0)
        close = Gtk.Button(label=t("common.close"))
        close.connect("clicked", lambda _button: self.close())
        buttons.pack_end(close, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self.connect("destroy", self._on_destroy)
        self.reload()
        self._source = GLib.timeout_add(REFRESH_MS, self._tick)

    # -- layout ---------------------------------------------------------

    def _group(self, side: str, key: str, caption: str, hint: str) -> Gtk.Frame:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(10)

        explanation = Gtk.Label(xalign=0.0)
        explanation.set_markup(f"<small>{t(hint)}</small>")
        outer.pack_start(explanation, False, False, 0)

        every = Gtk.CheckButton(label=t("levels.use_all"))
        self._all_handlers[side] = every.connect(
            "toggled", lambda button, s=side: self._on_all_toggled(s, button)
        )
        self._all_checks[side] = every
        outer.pack_start(every, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=4)
        self._grids[side] = grid
        outer.pack_start(grid, False, False, 0)

        warning = Gtk.Label(xalign=0.0)
        self._warnings[side] = warning
        outer.pack_start(warning, False, False, 0)

        frame = Gtk.Frame(label=f" {t(caption)} ")
        frame.add(outer)
        return frame

    # -- building the rows ----------------------------------------------

    def reload(self) -> None:
        """Re-read the device list and start a fresh capture per device."""
        self._loading = True
        self._stop_captures()
        sources = devices_module.list_sources()
        failures: list[str] = []

        for side, key, monitors, _caption, _hint in GROUPS:
            grid = self._grids[side]
            for child in grid.get_children():
                grid.remove(child)
            self._rows[side] = []

            # Every source in both groups: the expected kind first, then the rest.
            # A virtual cable or a line input carrying the far end is a real case,
            # and it must be tickable where its level is visible.
            expected = [device for device in sources if device.is_monitor is monitors]
            others = [device for device in sources if device.is_monitor is not monitors]
            wanted = expected + others
            for index, device in enumerate(wanted):
                row = DeviceRow(
                    device,
                    on_toggled=self._on_row_toggled,
                    foreign=device.is_monitor is not monitors,
                )
                row.attach(grid, index)
                self._rows[side].append(row)
                if not self._start_capture(row, side):
                    failures.append(device.label())

            self._apply_config_to_rows(side, key)

        self._hint.set_markup(
            f"<small>{t('levels.probe_failed', names=', '.join(failures))}</small>"
            if failures
            else f"<small>{t('levels.footer')}</small>"
        )
        self._loading = False
        self.show_all()

    def _start_capture(self, row: DeviceRow, side: str) -> bool:
        process = capture.CaptureProcess(
            row.device.name,
            side=side,
            sample_rate=int(self.config.get("audio.sample_rate")),
            block_frames=self.config.block_frames,
            buffer_seconds=1.0,
        )
        try:
            process.start()
        except OSError:
            return False
        row.capture = process
        return True

    def _apply_config_to_rows(self, side: str, key: str) -> None:
        """Show what the settings currently say, without writing anything back."""
        value = self.config.get(key)
        every = value == ALL
        check = self._all_checks[side]
        check.handler_block(self._all_handlers[side])
        check.set_active(every)
        check.handler_unblock(self._all_handlers[side])

        if isinstance(value, (list, tuple)):
            selected = set(value)
        elif every:
            # "All available" means every device of this side's own kind - the
            # foreign ones are on offer, not implied.
            selected = {row.device.name for row in self._rows[side] if not row.foreign}
        else:
            # AUTO or a single name: show what would actually be recorded.
            resolution = devices_module.resolve(
                self.config.get("devices.mic"), self.config.get("devices.speaker")
            )
            selected = set(resolution.mics if side == MIC else resolution.speakers)

        for row in self._rows[side]:
            row.set_active_quietly(row.device.name in selected)
            row.check.set_sensitive(not every)
        self._update_warning(side)

    # -- reacting to the user -------------------------------------------

    def _on_all_toggled(self, side: str, button: Gtk.CheckButton) -> None:
        if self._loading:
            return
        key = "devices.mic" if side == MIC else "devices.speaker"
        if button.get_active():
            self.config.set(key, ALL)
        else:
            # Turning it off keeps exactly what is on screen, so nothing changes
            # under the user at the moment they take manual control.
            self.config.set(key, [row.device.name for row in self._rows[side] if row.check.get_active()])
        for row in self._rows[side]:
            row.check.set_sensitive(not button.get_active())
        self._save_and_apply()
        self._update_warning(side)

    def _on_row_toggled(self) -> None:
        if self._loading:
            return
        for side, key, _monitors, _caption, _hint in GROUPS:
            if self._all_checks[side].get_active():
                continue
            self.config.set(
                key, [row.device.name for row in self._rows[side] if row.check.get_active()]
            )
            self._update_warning(side)
        self._save_and_apply()

    def _update_warning(self, side: str) -> None:
        empty = not any(row.check.get_active() for row in self._rows[side])
        self._warnings[side].set_markup(
            f"<small>{t('levels.nothing_selected')}</small>" if empty else ""
        )

    def _save_and_apply(self) -> None:
        self.config.save()
        # A running recording follows immediately - that is the whole point of
        # being able to tick a device while you can hear it is the right one.
        self.recorder.apply_device_settings()

    # -- refresh --------------------------------------------------------

    def _tick(self) -> bool:
        for rows in self._rows.values():
            for row in rows:
                row.refresh()
        return True

    # -- teardown -------------------------------------------------------

    def _stop_captures(self) -> None:
        for rows in self._rows.values():
            for row in rows:
                if row.capture is not None:
                    row.capture.stop()
                    row.capture = None

    def _on_destroy(self, _widget) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        self._stop_captures()
        if self.on_closed is not None:
            self.on_closed()
