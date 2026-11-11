"""
The single-file executable of Echolot: its name, its version, its child processes.

The executable itself is never started here - ``build-exe.py`` does that after every
build. These tests pretend to be frozen and check that nothing in that mode reaches for
a Python interpreter that is not there (in the executable ``sys.executable`` is the
program itself), plus the version the release tag is made from.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import exe_support  # noqa: E402
from echolot import VERSION_LABEL, __build__, __version__, autostart  # noqa: E402

ENTRY = ROOT / "run.py"


def _load(name, filename):
    """Import a file of the project by path (``build-exe.py`` is not a module name)."""
    spec = importlib.util.spec_from_file_location(name, str(ROOT / filename))
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUILD = _load("build_exe_echolot", "build-exe.py")


class FrozenTestCase(unittest.TestCase):
    """Pretend to run as the executable at ``self.executable``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.bundle = self.tmp / "_MEIabc"
        self.bundle.mkdir()
        self.executable = self.tmp / "bin" / "echolot-linux-x86_64-1.7.1-build13"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"exe")
        for patcher in (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", str(self.bundle), create=True),
            mock.patch.object(sys, "executable", str(self.executable)),
            mock.patch.object(exe_support, "IS_FROZEN", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


# ----------------------------------------------------------------------
# Name
# ----------------------------------------------------------------------
class ExecutableNameTests(unittest.TestCase):
    def test_the_file_is_named_after_machine_and_version(self):
        for machine, expected in (
            ("x86_64", "echolot-linux-x86_64-0.3.4-build31"),
            ("AMD64", "echolot-linux-x86_64-0.3.4-build31"),
            ("aarch64", "echolot-linux-aarch64-0.3.4-build31"),
            ("arm64", "echolot-linux-aarch64-0.3.4-build31"),
        ):
            with mock.patch.object(BUILD.platform, "machine", return_value=machine):
                self.assertEqual(BUILD.executable_name("0.3.4", "31"), expected)

    def test_the_spec_names_the_entry_script_and_the_file(self):
        text = BUILD.spec_text(Path("/stage"), "echolot-linux-x86_64-0.3.4-build31")
        self.assertIn(repr(str(ENTRY)), text)
        self.assertIn("gi.repository.Gtk", text)
        self.assertIn("name='echolot-linux-x86_64-0.3.4-build31'", text)
        self.assertIn("[('/stage', '.')]", text)

    def test_the_build_artifacts_are_ignored_by_git(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in ("build/", "dist/"):
            self.assertTrue(entry in lines or "/" + entry in lines, entry)

    def test_every_data_file_exists(self):
        for relative in BUILD.DATA_FILES:
            self.assertTrue((ROOT / relative).exists(), relative)


# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------
class VersionTests(unittest.TestCase):
    def test_the_build_script_reads_the_version_the_program_states(self):
        """Executable name, release tag and ``--version`` must all come from one place."""
        self.assertEqual(BUILD.read_version(), (__version__, str(__build__)))

    def test_the_label_is_version_and_build(self):
        self.assertEqual(VERSION_LABEL, f"{__version__} ({__build__})")

    def test_a_version_without_a_build_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "echolot").mkdir()
            (Path(tmp) / "echolot" / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
            with mock.patch.object(BUILD, "ROOT", Path(tmp)), self.assertRaises(SystemExit):
                BUILD.read_version()

    def test_version_flag_answers_without_a_display_and_without_the_audio_tools(self):
        """``--version`` is the start test of the build: it must not need FFmpeg, parec or a display."""
        environment = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        environment["PATH"] = "/nonexistent"
        done = subprocess.run(
            [sys.executable, str(ENTRY), "--version"],
            capture_output=True, text=True, env=environment, timeout=60, check=False, cwd=str(ROOT),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"Echolot {VERSION_LABEL}", done.stdout)


# ----------------------------------------------------------------------
# Child processes
# ----------------------------------------------------------------------
class ChildEnvironmentTests(unittest.TestCase):
    def test_children_get_no_paths_into_the_unpacked_files(self):
        """Otherwise the tools the program starts load the program's libraries."""
        env = {
            "LD_LIBRARY_PATH": "/tmp/_MEIabc",
            "LD_LIBRARY_PATH_ORIG": "/opt/lib",
            "TCL_LIBRARY": "/tmp/_MEIabc/_tcl_data",
            "XDG_DATA_DIRS": "/tmp/_MEIabc/share:/usr/share",
            "_PYI_ARCHIVE_FILE": "/home/me/display-off",
            "HOME": "/home/me",
        }
        with mock.patch.object(exe_support, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True):
            self.assertEqual(exe_support.child_environment(env), {
                "LD_LIBRARY_PATH": "/opt/lib",
                "XDG_DATA_DIRS": "/usr/share",
                "HOME": "/home/me",
            })
            self.assertNotIn("LD_LIBRARY_PATH",
                             exe_support.child_environment({"LD_LIBRARY_PATH": "/tmp/_MEIabc"}))

    def test_a_checkout_hands_its_environment_on_unchanged(self):
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            self.assertEqual(exe_support.child_environment({"LD_LIBRARY_PATH": "/x", "A": "b"}),
                             {"LD_LIBRARY_PATH": "/x", "A": "b"})

    def test_the_executable_cleans_every_child_and_does_it_once(self):
        original = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", original)
        with mock.patch.object(exe_support, "IS_FROZEN", True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEIabc", create=True), \
                mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEIabc",
                                             "LD_LIBRARY_PATH_ORIG": "/opt/lib"}):
            exe_support.use_system_environment_for_children()
            patched = subprocess.Popen
            exe_support.use_system_environment_for_children()
            self.assertIs(subprocess.Popen, patched)
            done = subprocess.run(["sh", "-c", "printf %s \"$LD_LIBRARY_PATH\""],
                                  capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout, "/opt/lib")

    def test_outside_the_executable_nothing_is_patched(self):
        original = subprocess.Popen
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            exe_support.use_system_environment_for_children()
        self.assertIs(subprocess.Popen, original)


# ----------------------------------------------------------------------
# No interpreter calls when frozen
# ----------------------------------------------------------------------
class RelaunchTests(FrozenTestCase):
    def test_the_program_is_started_again_through_itself(self):
        self.assertEqual(exe_support.relaunch_command("/x/display_off.py", "--helper"),
                         [str(self.executable.resolve()), "--helper"])

    def test_a_checkout_starts_it_through_python(self):
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            self.assertEqual(exe_support.relaunch_command("/x/display_off.py", "--helper"),
                             [str(self.executable), "-u", "/x/display_off.py", "--helper"])


# ----------------------------------------------------------------------
# No interpreter calls when frozen
# ----------------------------------------------------------------------
class RelaunchTests(FrozenTestCase):
    def test_the_program_is_started_again_through_itself(self):
        self.assertEqual(exe_support.relaunch_command("/x/run.py", "--helper"),
                         [str(self.executable.resolve()), "--helper"])

    def test_a_checkout_starts_it_through_python(self):
        with mock.patch.object(exe_support, "IS_FROZEN", False):
            self.assertEqual(exe_support.relaunch_command("/x/run.py", "--helper"),
                             [str(self.executable), "-u", "/x/run.py", "--helper"])


class AutostartTests(FrozenTestCase):
    def test_the_autostart_entry_starts_the_executable_itself(self):
        self.assertEqual(autostart.exec_command(), '"%s" --autostart' % self.executable)
        self.assertEqual(autostart.exec_command(autostart=False), '"%s"' % self.executable)

    def test_a_checkout_starts_the_script_through_python(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(autostart.exec_command(),
                             '%s "%s" --autostart' % (self.executable, ROOT / "run.py"))



if __name__ == "__main__":
    unittest.main()
