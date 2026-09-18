"""Device discovery on PipeWire/PulseAudio via `pactl`.

Two things are needed for every recording:

    mic      the source you speak into        -> default source
    speaker  what the other side says         -> monitor source of the default sink

`pactl -f json` is the nicest format but it prints warnings for non-ASCII device
descriptions, so names and states come from the ASCII-safe short listing and JSON
is only consulted for the human readable labels. If JSON fails entirely, labels
are derived from the device name and recording still works - a missing label is
cosmetic, a missing device is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import t
from .backend import Device, current as _backend

__all__ = ["ALL", "AUTO", "Device", "Resolution", "resolve", "list_sources"]

#: Record every device of the matching kind - the default, so that no choice can
#: be wrong and nothing is missed.
ALL = "all"
#: Follow whatever the system default is right now.
AUTO = "auto"


@dataclass(frozen=True)
class Resolution:
    """The concrete devices a session will record from.

    A side can be more than one device: `ALL` records every input and every
    output monitor the machine has, so no device choice can be wrong and a source
    that only shows up sometimes is still captured.
    """

    mics: tuple[str, ...]
    speakers: tuple[str, ...]
    mic_label: str
    speaker_label: str
    problems: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.mics and self.speakers)

    @property
    def mic(self) -> str | None:
        """The leading microphone, for everything that names a single device."""
        return self.mics[0] if self.mics else None

    @property
    def speaker(self) -> str | None:
        return self.speakers[0] if self.speakers else None

    @property
    def devices(self) -> tuple[str, ...]:
        return self.mics + self.speakers



# -- what the sound system says, whichever one it is ---------------------


def list_sources() -> list[Device]:
    """All capturable sources: real inputs first, then monitors."""
    return _backend().list_sources()


def default_source() -> str | None:
    return _backend().default_source()


def default_monitor(sources: list[Device] | None = None) -> str | None:
    return _backend().default_monitor(sources)


def resolve(mic_setting: str, speaker_setting: str) -> Resolution:
    """Turn the configured values into the devices to record from.

    Three kinds of value per side: `ALL` for everything the machine offers,
    `AUTO` for whatever is the system default right now, or one device by name.
    """
    sources = list_sources()
    known = {device.name: device for device in sources}
    problems: list[str] = []

    mics = _resolve_side(mic_setting, sources, known, problems, monitors=False)
    speakers = _resolve_side(speaker_setting, sources, known, problems, monitors=True)

    return Resolution(
        mics=tuple(mics),
        speakers=tuple(speakers),
        mic_label=_side_label(mics, known),
        speaker_label=_side_label(speakers, known),
        problems=tuple(problems),
    )


def _resolve_side(
    setting: str,
    sources: list[Device],
    known: dict[str, Device],
    problems: list[str],
    *,
    monitors: bool,
) -> list[str]:
    """Devices for one side, in the order they should be recorded."""
    if setting == ALL:
        # Everything of the matching kind first, then the rest: a machine with no
        # monitor at all should still record its inputs rather than nothing.
        wanted = [d.name for d in sources if d.is_monitor is monitors]
        if wanted:
            return wanted
        problems.append(t("devices.monitor_none") if monitors else t("devices.mic_none"))
        return []

    if isinstance(setting, (list, tuple)):
        # An explicit selection, ticked by hand in the levels dialog. Devices that
        # have since disappeared are named rather than silently dropped.
        wanted = [name for name in setting if name in known]
        for name in setting:
            if name not in known:
                problems.append(
                    t("devices.output_missing", name=name)
                    if monitors
                    else t("devices.mic_missing", name=name)
                )
        if not wanted:
            problems.append(t("devices.monitor_none") if monitors else t("devices.mic_none"))
        return wanted

    if setting and setting != AUTO:
        if setting in known:
            return [setting]
        problems.append(
            t("devices.output_missing", name=setting)
            if monitors
            else t("devices.mic_missing", name=setting)
        )

    chosen = default_monitor(sources) if monitors else default_source()
    if chosen and chosen not in known:
        # A default that is not in the source list would fail at capture time.
        if not monitors:
            problems.append(t("devices.mic_unusable", name=chosen))
        chosen = next((d.name for d in sources if d.is_monitor is monitors), None)
    if not chosen:
        problems.append(t("devices.monitor_none") if monitors else t("devices.mic_none"))
        return []
    return [chosen]


def _side_label(names: list[str], known: dict[str, Device]) -> str:
    """What the user is shown for a side: the device, or how many of them."""
    if not names:
        return "-"
    if len(names) == 1:
        name = names[0]
        return known[name].label() if name in known else name
    return t("devices.several", count=len(names))
