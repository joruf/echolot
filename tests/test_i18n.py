"""Translations: completeness, placeholders and the fallback behaviour.

The tests that matter here are the mechanical ones. A missing key or a mistyped
placeholder is invisible until the moment the message is needed - which is
usually the moment something has already gone wrong.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from echolot import i18n, paths

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCALES_DIR = PROJECT_DIR / "echolot" / "locales"
EXPECTED_LANGUAGES = {"en", "de", "es", "fr"}

#: Every key the code asks for directly, collected from `t("...")` calls. The
#: namespaces overlap with config paths (`devices.mic`), so nothing wider than
#: this would be reliable.
SOURCES = " ".join(
    path.read_text(encoding="utf-8")
    for path in list((PROJECT_DIR / "echolot").rglob("*.py")) + [PROJECT_DIR / "run.py"]
)
USED_KEYS = set(re.findall(r"""\bt\(\s*["']([a-z0-9_.]+)["']""", SOURCES))

#: Keys the code does not name literally in a `t(...)` call. Listed here on
#: purpose: it keeps the unused-key check meaningful instead of toothless.
INDIRECT_KEYS = {
    # held in tables in ui/settings_window.py and translated when the combo is built
    "settings.format_opus",
    "settings.format_flac",
    "settings.format_wav",
    "settings.layout_mix",
    "settings.layout_split",
    # built from the recorder state: t(f"state.{state.value}")
    "state.idle",
    "state.recording",
    "state.paused",
    "state.error",
    # read per language for the .desktop entry, never through t()
    "desktop.comment",
    "_label",
}
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def catalog(code: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def restore_language():
    """Every test leaves the translator on the default language."""
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


# -- the catalogues ------------------------------------------------------


def test_the_four_promised_languages_exist():
    assert EXPECTED_LANGUAGES <= set(i18n.language_codes())


def test_english_is_the_default():
    assert i18n.DEFAULT_LANGUAGE == "en"
    assert i18n.current_language() == "en"
    assert i18n.t("menu.start") == catalog("en")["menu.start"]


def test_every_language_has_a_display_label():
    labels = dict(i18n.available_languages())
    assert labels["en"] == "English"
    assert labels["de"] == "Deutsch"
    assert labels["es"] == "Español"
    assert labels["fr"] == "Français"


@pytest.mark.parametrize("code", sorted(EXPECTED_LANGUAGES))
def test_no_language_is_missing_a_key(code):
    """A gap would silently fall back to English mid-sentence."""
    missing = set(catalog("en")) - set(catalog(code))
    assert missing == set(), f"{code} is missing: {sorted(missing)}"


@pytest.mark.parametrize("code", sorted(EXPECTED_LANGUAGES - {"en"}))
def test_no_language_has_keys_english_does_not(code):
    extra = set(catalog(code)) - set(catalog("en"))
    assert extra == set(), f"{code} has unknown keys: {sorted(extra)}"


@pytest.mark.parametrize("code", sorted(EXPECTED_LANGUAGES - {"en"}))
def test_placeholders_match_english(code):
    """A renamed placeholder would render as literal text in that language."""
    english, other = catalog("en"), catalog(code)
    for key, template in english.items():
        expected = set(PLACEHOLDER_RE.findall(template))
        actual = set(PLACEHOLDER_RE.findall(other[key]))
        assert actual == expected, f"{code}/{key}: {actual} instead of {expected}"


@pytest.mark.parametrize("code", sorted(EXPECTED_LANGUAGES))
def test_markup_is_balanced(code):
    """Pango markup in a label must not break the window it is shown in."""
    for key, template in catalog(code).items():
        for tag in ("b", "small", "tt"):
            assert template.count(f"<{tag}>") == template.count(f"</{tag}>"), f"{code}/{key}"


# -- code and catalogue agree -------------------------------------------


def test_every_key_used_in_the_code_exists():
    assert USED_KEYS, "no t(...) calls found - the collecting regex is broken"
    missing = sorted(key for key in USED_KEYS if key not in catalog("en"))
    assert missing == [], f"used but not translated: {missing}"


def test_no_unused_keys_are_carried_around():
    known = USED_KEYS | INDIRECT_KEYS
    unused = sorted(key for key in catalog("en") if key not in known)
    assert unused == [], f"translated but never used: {unused}"


def test_indirect_keys_are_really_indirect():
    """Guards the list above: a key that became a direct call belongs out of it."""
    overlap = sorted(INDIRECT_KEYS & USED_KEYS)
    assert overlap == [], f"listed as indirect but called directly: {overlap}"


# -- behaviour ----------------------------------------------------------


def test_switching_language_changes_the_output():
    i18n.set_language("de")
    assert i18n.t("menu.quit") == "Beenden"
    i18n.set_language("fr")
    assert i18n.t("menu.quit") == "Quitter"
    i18n.set_language("es")
    assert i18n.t("menu.quit") == "Salir"


def test_unknown_language_is_ignored():
    i18n.set_language("de")
    i18n.set_language("klingon")
    assert i18n.current_language() == "de"


def test_missing_key_returns_the_key_itself():
    assert i18n.t("gibt.es.nicht") == "gibt.es.nicht"


def test_placeholders_are_filled_in():
    i18n.set_language("de")
    assert i18n.t("tooltip.free", size="1,5 GB") == "Frei: 1,5 GB"


def test_a_wrong_placeholder_does_not_raise():
    """A broken call must not take a window down; the raw template is fine."""
    assert "{size}" in i18n.t("tooltip.free", unerwartet=1)


def test_incomplete_language_falls_back_to_english(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text(
        (LOCALES_DIR / "en.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "xx.json").write_text(
        json.dumps({"_label": "Teilweise", "menu.quit": "Weg"}), encoding="utf-8"
    )
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)

    translator = i18n.Translator()
    translator.set_language("xx")
    assert translator.get("menu.quit") == "Weg"
    # Not in the partial file: English still answers.
    assert translator.get("menu.pause") == "Pause"


# -- values that depend on the language --------------------------------


def test_size_uses_the_decimal_separator_of_the_language():
    assert paths.human_size(2048) == "2.0 KB"
    i18n.set_language("de")
    assert paths.human_size(2048) == "2,0 KB"
    i18n.set_language("fr")
    assert paths.human_size(2048) == "2,0 KB"


def test_translations_of_collects_every_language():
    comments = i18n.translations_of("desktop.comment")
    assert EXPECTED_LANGUAGES <= set(comments)
    assert comments["de"] != comments["en"]
