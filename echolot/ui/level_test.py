"""Level test window - the answer to "does it actually hear us both?".

Run before an important conversation: both bars have to move, one when you
speak, one when the other side does. Catching a dead channel here costs a few
seconds; catching it afterwards costs the conversation.

While a recording is running the window shows that recording's levels instead of
opening capture processes of its own.
"""

from __future__ import annotations

from array import array
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ..audio import capture, devices
from ..audio.mixer import SILENT_METRICS, ChannelMetrics, measure
from ..i18n import t
from ..speechlog import MIC, SPEAKER
from .menu import shorten

REFRESH_MS = 100
FLOOR_DB = -60.0
# Bars fall back slowly so a short word stays visible instead of flickering.
DECAY_DB_PER_TICK = 3.0


def normalise(level_db: float) -> float:
    return max(0.0, min(1.0, (level_db - FLOOR_DB) / -FLOOR_DB))


class LevelTestWindow(Gtk.Window):
    def __init__(self, config, recorder, on_closed: Callable[[], None] | None = None) -> None:
        super().__init__(title=t("level.title"))
        self.config = config
        self.recorder = recorder
        self.on_closed = on_closed
        self.set_default_size(460, -1)
        self.set_border_width(14)
        self.set_position(Gtk.WindowPosition.CENTER)

        self._captures: dict[str, capture.CaptureProcess] = {}
        self._shown_db = {MIC: FLOOR_DB, SPEAKER: FLOOR_DB}
        self._source: int | None = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(box)

        heading = Gtk.Label(xalign=0.0)
        heading.set_markup(t("level.heading"))
        box.pack_start(heading, False, False, 0)

        self._bars: dict[str, Gtk.LevelBar] = {}
        self._values: dict[str, Gtk.Label] = {}
        self._names: dict[str, Gtk.Label] = {}

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        box.pack_start(grid, False, False, 0)
        for row, (side, caption) in enumerate(
            ((MIC, t("common.you")), (SPEAKER, t("common.other")))
        ):
            caption_label = Gtk.Label(label=caption, xalign=0.0)
            bar = Gtk.LevelBar.new_for_interval(0.0, 1.0)
            bar.set_hexpand(True)
            bar.set_size_request(-1, 18)
            value = Gtk.Label(label=t("common.none"), xalign=1.0, width_chars=8)
            name = Gtk.Label(xalign=0.0)
            name.set_markup("<small> </small>")

            grid.attach(caption_label, 0, row * 2, 1, 1)
            grid.attach(bar, 1, row * 2, 1, 1)
            grid.attach(value, 2, row * 2, 1, 1)
            grid.attach(name, 1, row * 2 + 1, 2, 1)

            self._bars[side] = bar
            self._values[side] = value
            self._names[side] = name

        self._hint = Gtk.Label(xalign=0.0)
        box.pack_start(self._hint, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        close = Gtk.Button(label=t("common.close"))
        close.connect("clicked", lambda _button: self.close())
        buttons.pack_start(close, False, False, 0)
        box.pack_start(buttons, False, False, 0)

        self.connect("destroy", self._on_destroy)
        self._prepare_sources()
        self._source = GLib.timeout_add(REFRESH_MS, self._tick)

    # -- sources --------------------------------------------------------

    def _prepare_sources(self) -> None:
        if self.recorder.active:
            mic_label, speaker_label = self.recorder.device_labels()
            self._names[MIC].set_markup(f"<small>{shorten(mic_label, 60)}</small>")
            self._names[SPEAKER].set_markup(f"<small>{shorten(speaker_label, 60)}</small>")
            self._hint.set_markup(f"<small>{t('level.running')}</small>")
            return

        resolution = devices.resolve(
            self.config.get("devices.mic"), self.config.get("devices.speaker")
        )
        self._names[MIC].set_markup(f"<small>{shorten(resolution.mic_label, 60)}</small>")
        self._names[SPEAKER].set_markup(f"<small>{shorten(resolution.speaker_label, 60)}</small>")

        missing: list[str] = []
        for side, device in ((MIC, resolution.mic), (SPEAKER, resolution.speaker)):
            if not device:
                missing.append(t("common.mic") if side == MIC else t("common.output"))
                continue
            process = capture.CaptureProcess(
                device,
                side=side,
                sample_rate=int(self.config.get("audio.sample_rate")),
                block_frames=self.config.block_frames,
                buffer_seconds=1.0,
            )
            try:
                process.start()
            except OSError as exc:
                missing.append(f"{side}: {exc}")
                continue
            self._captures[side] = process

        self._hint.set_markup(
            f"<small>{t('level.no_device', sides=', '.join(missing))}</small>"
            if missing
            else f"<small>{t('level.local_only')}</small>"
        )

    def _metrics_from_capture(self, side: str) -> ChannelMetrics | None:
        process = self._captures.get(side)
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
        return ChannelMetrics(peak, total / count, True)

    # -- refresh --------------------------------------------------------

    def _tick(self) -> bool:
        if self.recorder.active:
            mic_metrics, speaker_metrics = self.recorder.levels()
            samples = {MIC: mic_metrics, SPEAKER: speaker_metrics}
        else:
            samples = {side: self._metrics_from_capture(side) for side in (MIC, SPEAKER)}

        for side in (MIC, SPEAKER):
            metrics = samples.get(side)
            if metrics is None or not metrics.present:
                self._shown_db[side] = max(FLOOR_DB, self._shown_db[side] - DECAY_DB_PER_TICK)
            else:
                self._shown_db[side] = max(
                    metrics.level_db, self._shown_db[side] - DECAY_DB_PER_TICK
                )
            level_db = self._shown_db[side]
            self._bars[side].set_value(normalise(level_db))
            self._values[side].set_text(
                t("common.none") if level_db <= FLOOR_DB else f"{level_db:5.0f} dB"
            )
        return True

    # -- teardown -------------------------------------------------------

    def _on_destroy(self, _widget) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None
        for process in self._captures.values():
            process.stop()
        self._captures.clear()
        if self.on_closed is not None:
            self.on_closed()
