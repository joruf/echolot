"""Which sound system this machine has, behind one deliberately small interface.

Everything above this line is the same everywhere: deciding which devices a side
records from, supervising a capture that dies, mixing, the speech log, the files.
Below it sits one implementation per sound system - PulseAudio/PipeWire on Linux,
WASAPI on Windows - and it is kept as thin as it can be, because every line down
here has to be written twice and tested on two machines.

A backend has to answer four questions and nothing more:

    list_sources()          what can be recorded from
    default_source()        what the system considers the microphone
    default_monitor()       what the system considers "what is being played"
    open_reader(...)        give me raw PCM from this device

plus `watch_command()` for finding out that the answers changed. Everything that
follows from those answers - `all` versus `auto`, missing devices, summing
several devices into one side, gap-free switching - is shared code and must stay
that way.

The platform is chosen lazily in `current()`, not at import time, so a backend
module can import `Device` from here without a cycle.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Device:
    """A capturable source, as the sound system describes it.

    `is_monitor` is the one judgement the backend makes for us: True means "this
    carries what is being played" (a PulseAudio monitor, a WASAPI loopback) and
    therefore normally the other side of the conversation.
    """

    name: str
    description: str
    is_monitor: bool
    state: str = ""

    def label(self) -> str:
        return self.description or self.name


class Reader(Protocol):
    """A live stream of raw signed 16-bit mono PCM from one device.

    Modelled on a pipe on purpose: `read` blocks until it has something, returns
    b"" when the stream has ended, and never raises for an ordinary end. The
    supervision around it - respawning, backing off, counting gaps - is shared
    code and must not need to know what is underneath.
    """

    def read(self, count: int) -> bytes:
        """At most `count` bytes; b"" once the stream is finished."""

    @property
    def alive(self) -> bool: ...

    @property
    def returncode(self) -> int | None:
        """Why it ended, if the platform can say. None while it runs."""

    def close(self, timeout: float = 1.0) -> None: ...


def current():
    """The backend for this machine."""
    if sys.platform == "win32":
        from . import wasapi

        return wasapi
    from . import pulse

    return pulse


def name() -> str:
    return getattr(current(), "NAME", "unknown")
