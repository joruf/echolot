"""Shared test setup: make the project importable and keep tests off real state."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from echolot.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect XDG locations so no test can touch the real configuration."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def config(tmp_path) -> Config:
    """A config that stores recordings inside the test's tmp_path."""
    cfg = Config(path=tmp_path / "settings.json")
    cfg.set("recordings_dir", str(tmp_path / "recordings"))
    return cfg
