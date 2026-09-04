"""GUI smoke test: build every window and the menu for real.

Widget mistakes - a wrong constructor keyword, a renamed method - do not show up
in unit tests, they show up when the user clicks. This builds the actual widgets
against the actual GTK on this machine, so those mistakes surface here instead.

Skipped without a display.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("gi")
pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="kein X-Display")

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from echolot.app import EcholotApp  # noqa: E402
from echolot.i18n import t  # noqa: E402
from echolot.session import State  # noqa: E402
from echolot.audio.devices import ALL  # noqa: E402
from echolot.speechlog import MIC, SPEAKER  # noqa: E402
from echolot.ui.levels_window import LevelsWindow  # noqa: E402
from echolot.ui.settings_window import SettingsWindow  # noqa: E402


def strip_markup(text: str) -> str:
    """The plain text GTK shows for a markup string."""
    import re

    return re.sub(r"<[^>]+>", "", text)


def pump(iterations: int = 40) -> None:
    """Let GTK process pending work without entering a main loop."""
    for _ in range(iterations):
        if not Gtk.events_pending():
            break
        Gtk.main_iteration_do(False)


@pytest.fixture
def app(config) -> EcholotApp:
    """An application object without the tray icon and without a main loop."""
    return EcholotApp(config)


def test_menu_builds_in_every_state(app):
    for state in (State.IDLE, State.RECORDING, State.PAUSED, State.ERROR):
        app.recorder._state = state
        menu = Gtk.Menu()
        app.menu.populate(menu)
        labels = [
            child.get_label() for child in menu.get_children() if isinstance(child, Gtk.MenuItem)
        ]
        assert t("menu.stop") in labels or t("menu.start") in labels
        assert t("menu.quit") in labels
        assert t("menu.settings") in labels
        menu.destroy()
    app.recorder._state = State.IDLE


def test_menu_offers_devices_and_recent_recordings(app, tmp_path):
    recordings = app.config.recordings_dir
    recordings.mkdir(parents=True, exist_ok=True)
    (recordings / "Echolot_2026-08-12_10-15-03.opus").write_bytes(b"x" * 2048)
    (recordings / "Echolot_2026-08-12_10-15-03.log").write_text("{}\n", encoding="utf-8")

    menu = Gtk.Menu()
    app.menu.populate(menu)
    submenus = {
        child.get_label(): child.get_submenu()
        for child in menu.get_children()
        if isinstance(child, Gtk.MenuItem) and child.get_submenu() is not None
    }
    assert t("menu.devices") in submenus
    assert t("menu.recent") in submenus
    entries = [item.get_label() for item in submenus[t("menu.recent")].get_children()]
    assert any("12.08.2026 10:15" in (label or "") for label in entries)
    menu.destroy()


def test_settings_window_builds_and_saves(app, config):
    saved = []
    window = SettingsWindow(config, on_saved=lambda: saved.append(1))
    window.show_all()
    pump()

    window.blink_spin.set_value(850)
    window.format_combo.set_active_id("flac")
    window.layout_combo.set_active_id("split")
    window.preroll_combo.set_active_id("3")
    window.silent_spin.set_value(45)
    window._on_save(None)
    pump()

    assert saved == [1]
    assert config.get("tray.blink_interval_ms") == 850
    assert config.get("audio.format") == "flac"
    assert config.audio_layout == "split"
    assert config.audio_channels == 2
    assert config.get("audio.preroll_minutes") == 3
    assert config.get("warnings.silent_side_seconds") == 45
    assert config.path.exists()
    window.destroy()
    pump()


def test_preroll_offers_off_plus_one_to_five_minutes(app, config):
    window = SettingsWindow(config)
    model = window.preroll_combo.get_model()
    ids = []
    for row in model:
        window.preroll_combo.set_active_iter(row.iter)
        ids.append(window.preroll_combo.get_active_id())
    assert ids == ["0", "1", "2", "3", "4", "5"]
    window.destroy()


def test_preroll_hint_names_the_memory_cost(app, config):
    """The cost has to be visible where the choice is made."""
    window = SettingsWindow(config)
    window.preroll_combo.set_active_id("0")
    assert window.preroll_hint.get_text() == strip_markup(t("settings.preroll_hint_off"))

    window.preroll_combo.set_active_id("2")
    hint = window.preroll_hint.get_text()
    assert "11.0 MB" in hint  # 2 minutes, mixed
    assert "microphone" in hint.lower()  # and the honest downside
    window.destroy()


def test_preroll_memory_estimate_follows_the_track_layout(app, config):
    """Two channels cost twice as much, and the hint has to say so."""
    window = SettingsWindow(config)
    window.preroll_combo.set_active_id("2")
    window.layout_combo.set_active_id("mix")
    assert "11.0 MB" in window.preroll_hint.get_text()
    window.layout_combo.set_active_id("split")
    assert "22.0 MB" in window.preroll_hint.get_text()
    window.destroy()


def test_windows_follow_the_configured_language(app, config):
    from echolot import i18n

    try:
        config.set("language", "fr")
        config.apply_language()
        window = SettingsWindow(config)
        assert window.get_title() == t("settings.title")
        assert "paramètres" in window.get_title().lower()
        window.destroy()
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_bitrate_is_only_editable_for_opus(app, config):
    window = SettingsWindow(config)
    window.format_combo.set_active_id("wav")
    assert window.bitrate_spin.get_sensitive() is False
    window.format_combo.set_active_id("opus")
    assert window.bitrate_spin.get_sensitive() is True
    window.destroy()


def test_levels_window_runs_and_closes(app, config):
    closed = []
    window = LevelsWindow(config, app.recorder, on_closed=lambda: closed.append(1))
    window.show_all()
    pump()
    window._tick()  # would raise if the widgets were wired up wrongly
    window.destroy()
    pump()
    assert closed == [1]


def test_levels_window_lists_every_device_with_a_tick(app, config):
    window = LevelsWindow(config, app.recorder)
    pump()
    rows = window._rows[MIC] + window._rows[SPEAKER]
    assert rows, "no device rows at all"
    # Every row carries the three things the dialog is for.
    for row in rows:
        assert row.check is not None and row.bar is not None and row.value is not None
    window.destroy()
    pump()


def test_use_all_is_reflected_and_writable(app, config):
    config.set("devices.mic", ALL)
    window = LevelsWindow(config, app.recorder)
    pump()

    assert window._all_checks[MIC].get_active() is True
    # While "all" is on, single rows must not be editable behind its back.
    assert all(not row.check.get_sensitive() for row in window._rows[MIC])

    window._all_checks[MIC].set_active(False)
    pump()
    value = config.get("devices.mic")
    assert isinstance(value, list)  # turned into the explicit selection
    assert all(row.check.get_sensitive() for row in window._rows[MIC])
    window.destroy()
    pump()


def test_unticking_a_device_takes_it_out_of_the_recording(app, config):
    config.set("devices.mic", ALL)
    window = LevelsWindow(config, app.recorder)
    pump()
    window._all_checks[MIC].set_active(False)
    pump()

    row = window._rows[MIC][0]
    row.check.set_active(False)
    pump()

    assert row.device.name not in config.get("devices.mic")
    window.destroy()
    pump()


def test_a_side_with_nothing_ticked_says_so(app, config):
    window = LevelsWindow(config, app.recorder)
    pump()
    window._all_checks[MIC].set_active(False)
    for row in window._rows[MIC]:
        row.check.set_active(False)
    pump()

    assert config.get("devices.mic") == []
    assert "<b>" in window._warnings[MIC].get_label()  # visibly warned, not silent
    window.destroy()
    pump()


def test_tooltip_is_built_in_every_state(app):
    for state in (State.IDLE, State.RECORDING, State.ERROR):
        app.recorder._state = state
        text = app._tooltip_text()
        assert "Echolot" in text
        assert len(text.splitlines()) >= 2
    app.recorder._state = State.IDLE


def test_every_source_is_offered_on_both_sides(app, config):
    """The other side's audio can arrive on an input - a cable, a line in."""
    window = LevelsWindow(config, app.recorder)
    pump()

    mic_names = [row.device.name for row in window._rows[MIC]]
    speaker_names = [row.device.name for row in window._rows[SPEAKER]]
    assert set(mic_names) == set(speaker_names), "a side is hiding devices"
    # The expected kind comes first, the rest are marked as foreign.
    assert window._rows[MIC][0].foreign is False
    assert any(row.foreign for row in window._rows[SPEAKER]) or len(speaker_names) == 1
    window.destroy()
    pump()


def test_an_input_can_be_ticked_for_the_other_side(app, config):
    """The setting the host-audio route needs: an input on the far-side channel."""
    window = LevelsWindow(config, app.recorder)
    pump()
    window._all_checks[SPEAKER].set_active(False)
    pump()

    foreign = [row for row in window._rows[SPEAKER] if row.foreign]
    if not foreign:
        window.destroy(); pump(); pytest.skip("this machine has only one kind of source")
    row = foreign[0]
    row.check.set_active(True)
    pump()

    assert row.device.name in config.get("devices.speaker")
    window.destroy()
    pump()


def test_all_available_does_not_imply_the_foreign_devices(app, config):
    """"All available" stays "every monitor", not "every source there is"."""
    config.set("devices.speaker", ALL)
    window = LevelsWindow(config, app.recorder)
    pump()

    for row in window._rows[SPEAKER]:
        assert row.check.get_active() is (not row.foreign)
    window.destroy()
    pump()
