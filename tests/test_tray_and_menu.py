"""Double click detection and the menu's text level meter."""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from echolot import i18n  # noqa: E402
from echolot.session import State  # noqa: E402
from echolot.ui.menu import BAR_WIDTH, level_bar, shorten, state_label  # noqa: E402
from echolot.ui.tray import DoubleClickDetector, double_click_interval_ms  # noqa: E402


def test_two_quick_clicks_toggle_once():
    """A double click must switch recording exactly once, not twice."""
    fired = []
    detector = DoubleClickDetector(lambda: fired.append(1), interval_ms=400)
    detector.click()
    assert fired == []  # a single click does nothing on purpose
    detector.click()
    assert fired == [1]


def test_four_clicks_toggle_twice():
    fired = []
    detector = DoubleClickDetector(lambda: fired.append(1), interval_ms=400)
    for _ in range(4):
        detector.click()
    assert fired == [1, 1]


def test_interval_falls_back_to_a_sane_default():
    assert 100 <= double_click_interval_ms() <= 2000


def test_level_bar_spans_from_silence_to_full_scale():
    assert level_bar(-120) == "▯" * BAR_WIDTH
    assert level_bar(-60) == "▯" * BAR_WIDTH
    assert level_bar(0) == "▮" * BAR_WIDTH
    half = level_bar(-30)
    assert half.count("▮") == BAR_WIDTH // 2
    assert len(half) == BAR_WIDTH


def test_shorten_keeps_menus_narrow():
    assert shorten("kurz") == "kurz"
    long_name = "ES1371/ES1373 Creative Labs CT2518 Audio PCI 64V/128/5200 Analoges Stereo"
    assert len(shorten(long_name)) <= 48
    assert shorten(long_name).endswith("…")


def test_every_state_has_a_label_in_every_language():
    try:
        for code in i18n.language_codes():
            i18n.set_language(code)
            for state in State:
                label = state_label(state)
                assert label and label != f"state.{state.value}", (code, state)
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_state_labels_follow_the_language():
    try:
        i18n.set_language("de")
        assert state_label(State.RECORDING) == "Aufnahme läuft"
        i18n.set_language("es")
        assert state_label(State.RECORDING) == "Grabando"
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
