"""Whether this is a virtual machine, and which one.

This matters for exactly one thing. In a guest, audio playing on the *host* is
not reachable: the sound card the guest sees carries what the guest itself plays
and nothing else. So an output monitor that delivers exact digital silence has a
different and far more likely cause here than on real hardware, and the warning
has to say so instead of leaving the user to find out after the conversation.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

DMI = Path("/sys/class/dmi/id")
DETECT_TIMEOUT = 3

#: What the detected identifier is called in a message.
NAMES = {
    "vmware": "VMware",
    "oracle": "VirtualBox",
    "virtualbox": "VirtualBox",
    "microsoft": "Hyper-V",
    "kvm": "KVM",
    "qemu": "QEMU",
    "xen": "Xen",
    "parallels": "Parallels",
}

#: Fallback for hosts without systemd: DMI strings and what they mean.
DMI_MARKERS = (
    ("vmware", "vmware"),
    ("virtualbox", "virtualbox"),
    ("innotek", "virtualbox"),
    ("qemu", "qemu"),
    ("kvm", "kvm"),
    ("xen", "xen"),
    ("parallels", "parallels"),
    ("hyper-v", "microsoft"),
    ("virtual machine", "microsoft"),
)


@lru_cache(maxsize=1)
def hypervisor() -> str | None:
    """Identifier of the hypervisor, or None on real hardware.

    Cached: the answer cannot change while the process runs, and this is read
    from a warning path that must not spawn a process every time.
    """
    detected = _from_systemd()
    if detected is not None:
        return detected
    return _from_dmi()


def in_vm() -> bool:
    return hypervisor() is not None


def label() -> str:
    """Name for a message; the raw identifier if it is one we do not know."""
    found = hypervisor()
    if not found:
        return ""
    return NAMES.get(found, found)


def _from_systemd() -> str | None:
    try:
        result = subprocess.run(
            ["systemd-detect-virt", "--vm"],
            capture_output=True,
            text=True,
            timeout=DETECT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip().lower()
    # "none" with a non-zero exit means bare metal; anything else is the answer.
    if not value or value == "none":
        return None
    return value


def _from_dmi() -> str | None:
    haystack = ""
    for entry in ("sys_vendor", "product_name", "board_vendor"):
        try:
            haystack += (DMI / entry).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
    for marker, name in DMI_MARKERS:
        if marker in haystack:
            return name
    return None
