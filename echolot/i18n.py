"""Translation layer.

Language files are plain JSON in ``echolot/locales``. Adding a language means
dropping another file in there - no code change. English is the default and the
fallback for any key a translation happens to be missing, so a partial file is
still usable.

Nothing needs to be relabelled when the language changes: the menu is rebuilt on
every right click, the tooltip on every tick, and the windows are built when they
open. Windows that are already open keep the language they were built with.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

DEFAULT_LANGUAGE = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"


class Translator:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._language = DEFAULT_LANGUAGE
        self._fallback: dict[str, str] = self._read(DEFAULT_LANGUAGE)
        self._catalog: dict[str, str] = dict(self._fallback)

    # -- loading ---------------------------------------------------------

    @staticmethod
    def _read(code: str) -> dict[str, str]:
        path = LOCALES_DIR / f"{code}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {key: value for key, value in data.items() if isinstance(value, str)}

    def available(self) -> list[tuple[str, str]]:
        """(code, display label) for every language file that parses."""
        found: list[tuple[str, str]] = []
        for path in sorted(LOCALES_DIR.glob("*.json")):
            data = self._read(path.stem)
            if not data:
                continue
            found.append((path.stem, data.get("_label", path.stem.upper())))
        return found or [(DEFAULT_LANGUAGE, "English")]

    @property
    def language(self) -> str:
        with self._lock:
            return self._language

    def set_language(self, code: str) -> None:
        """Switch language; an unknown or unreadable code is ignored."""
        catalog = self._read(code)
        with self._lock:
            if not catalog and code != DEFAULT_LANGUAGE:
                return
            self._language = code
            self._catalog = catalog

    # -- translation -----------------------------------------------------

    def get(self, key: str, **params) -> str:
        with self._lock:
            template = self._catalog.get(key) or self._fallback.get(key)
        if template is None:
            # Showing the key is ugly but tells you exactly what is missing,
            # which beats an empty label.
            return key
        if not params:
            return template
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            # A malformed placeholder must not take the window down.
            return template


TRANSLATOR = Translator()


def t(key: str, **params) -> str:
    return TRANSLATOR.get(key, **params)


def set_language(code: str) -> None:
    TRANSLATOR.set_language(code)


def current_language() -> str:
    return TRANSLATOR.language


def available_languages() -> list[tuple[str, str]]:
    return TRANSLATOR.available()


def language_codes() -> list[str]:
    return [code for code, _label in available_languages()]


def translations_of(key: str) -> dict[str, str]:
    """The value of one key in every language.

    Used for files that carry all languages at once instead of one at a time,
    such as the localised `Comment[xx]=` lines in a .desktop entry.
    """
    found: dict[str, str] = {}
    for code, _label in available_languages():
        value = Translator._read(code).get(key)
        if value:
            found[code] = value
    return found
