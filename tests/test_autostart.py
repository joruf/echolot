"""Autostart entry - without it the tray is missing after the next login."""

from __future__ import annotations

from echolot import autostart, paths


def test_entry_starts_run_py_in_autostart_mode():
    text = autostart.desktop_entry_text()
    assert "[Desktop Entry]" in text
    assert "run.py" in text
    assert "--autostart" in text
    assert f"Name={paths.APP_NAME}" in text
    assert "Terminal=false" in text


def test_enable_creates_the_file_and_disable_removes_it():
    assert autostart.is_enabled() is False
    path = autostart.enable()
    assert path == paths.autostart_file()
    assert path.exists()
    assert autostart.is_enabled() is True

    autostart.disable()
    assert path.exists() is False
    assert autostart.is_enabled() is False


def test_sync_follows_the_setting():
    autostart.sync(True)
    assert autostart.is_enabled() is True
    autostart.sync(False)
    assert paths.autostart_file().exists() is False


def test_disabled_marker_counts_as_off():
    """Cinnamon's startup list switches entries off with this line."""
    path = autostart.enable()
    path.write_text(
        autostart.desktop_entry_text().replace(
            "X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-enabled=false"
        ),
        encoding="utf-8",
    )
    assert autostart.is_enabled() is False
