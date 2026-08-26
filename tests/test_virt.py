"""Detecting the hypervisor - the reason a guest cannot hear its host."""

from __future__ import annotations

import subprocess

import pytest

from echolot import virt


@pytest.fixture(autouse=True)
def no_cache():
    """The answer is cached for the process; each test needs a clean one."""
    virt.hypervisor.cache_clear()
    yield
    virt.hypervisor.cache_clear()


def fake_run(stdout: str, returncode: int = 0):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    return run


def test_a_named_hypervisor_is_reported(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run("vmware\n"))
    assert virt.hypervisor() == "vmware"
    assert virt.in_vm() is True
    assert virt.label() == "VMware"


def test_bare_metal_is_not_a_virtual_machine(monkeypatch):
    # systemd prints "none" and exits non-zero on real hardware.
    monkeypatch.setattr(subprocess, "run", fake_run("none\n", returncode=1))
    monkeypatch.setattr(virt, "DMI", virt.Path("/nonexistent"))
    assert virt.hypervisor() is None
    assert virt.in_vm() is False
    assert virt.label() == ""


def test_an_unknown_identifier_is_passed_through(monkeypatch):
    monkeypatch.setattr(subprocess, "run", fake_run("bochs\n"))
    assert virt.label() == "bochs"  # better a raw name than none


def test_dmi_is_used_when_systemd_is_missing(monkeypatch, tmp_path):
    def missing(args, **kwargs):
        raise FileNotFoundError("systemd-detect-virt")

    monkeypatch.setattr(subprocess, "run", missing)
    (tmp_path / "sys_vendor").write_text("VMware, Inc.\n", encoding="utf-8")
    (tmp_path / "product_name").write_text("VMware Virtual Platform\n", encoding="utf-8")
    monkeypatch.setattr(virt, "DMI", tmp_path)

    assert virt.hypervisor() == "vmware"


def test_dmi_recognises_the_usual_suspects(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_run(""))
    monkeypatch.setattr(virt, "DMI", tmp_path)
    for vendor, expected in (
        ("innotek GmbH", "virtualbox"),
        ("QEMU", "qemu"),
        ("Xen", "xen"),
        ("Parallels Software", "parallels"),
    ):
        virt.hypervisor.cache_clear()
        (tmp_path / "sys_vendor").write_text(vendor, encoding="utf-8")
        assert virt.hypervisor() == expected, vendor


def test_unreadable_dmi_is_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", fake_run(""))
    monkeypatch.setattr(virt, "DMI", tmp_path / "gone")
    assert virt.hypervisor() is None


def test_a_hanging_detector_does_not_hang_the_warning(monkeypatch):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 3)

    monkeypatch.setattr(subprocess, "run", timeout)
    monkeypatch.setattr(virt, "DMI", virt.Path("/nonexistent"))
    assert virt.hypervisor() is None


def test_the_answer_is_only_looked_up_once(monkeypatch):
    calls = []

    def counted(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "vmware\n", "")

    monkeypatch.setattr(subprocess, "run", counted)
    for _ in range(5):
        virt.hypervisor()
    assert len(calls) == 1
