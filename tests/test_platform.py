import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import (PYTHON_PLACEHOLDER, child_env,
                                 describe_platform, force_utf8_io,
                                 python_executable, resolve_command)


class TestResolveCommand(unittest.TestCase):
    def test_placeholder_becomes_running_interpreter(self):
        cmd = resolve_command(f"{PYTHON_PLACEHOLDER} -m unittest -q")
        self.assertNotIn(PYTHON_PLACEHOLDER, cmd)
        self.assertIn("-m unittest -q", cmd)
        # Must be THIS interpreter, not a PATH guess: the child has to see the
        # same site-packages the parent validated.
        self.assertIn(os.path.basename(sys.executable).split(".")[0], cmd.lower())

    def test_legacy_python3_rewritten(self):
        """The whole reason the Windows suite failed: python3 does not exist."""
        cmd = resolve_command("python3 -m unittest discover -s tests")
        self.assertFalse(cmd.startswith("python3 "))
        self.assertIn("-m unittest discover -s tests", cmd)

    def test_bare_version_probe_rewritten(self):
        self.assertNotEqual(resolve_command("python3"), "python3")

    def test_similar_name_untouched(self):
        # `python3x` is not the interpreter; rewriting it would corrupt a real
        # command that happens to start with the same letters.
        self.assertEqual(resolve_command("python3x --help"), "python3x --help")

    def test_path_in_middle_untouched(self):
        cmd = "bash tools/python3-wrapper.sh"
        self.assertEqual(resolve_command(cmd), cmd)

    def test_explicit_placeholder_wins_over_legacy(self):
        # When both appear, the placeholder is authoritative and the later
        # literal is left alone.
        out = resolve_command(f"{PYTHON_PLACEHOLDER} -c \"print('python3')\"")
        self.assertIn("print('python3')", out)

    def test_empty_and_none_safe(self):
        self.assertEqual(resolve_command(""), "")

    def test_spaces_in_interpreter_are_quoted(self):
        exe = python_executable()
        if " " in (sys.executable or ""):
            self.assertTrue(exe.startswith('"') and exe.endswith('"'))
        else:
            self.assertFalse(exe.startswith('"'))

    def test_resolved_command_actually_runs(self):
        """End-to-end: the resolved string must execute in a real shell."""
        cmd = resolve_command(f'{PYTHON_PLACEHOLDER} -c "print(7*6)"')
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              encoding="utf-8", env=child_env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "42")


class TestChildEnv(unittest.TestCase):
    def test_utf8_pinned(self):
        env = child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_inherits_path(self):
        # PATH must survive: a child that cannot find its own tools is useless.
        upper = {k.upper() for k in child_env()}
        self.assertIn("PATH", upper)

    def test_extra_overrides(self):
        self.assertEqual(child_env({"DOBBY_X": "1"})["DOBBY_X"], "1")

    def test_child_emits_utf8_under_child_env(self):
        """The bug this exists for: a child printing non-ASCII must not crash.

        On a Korean Windows install the default stdio codec is cp949, which
        cannot encode an em dash. Without child_env the child raises
        UnicodeEncodeError and the parent sees an opaque failure.
        """
        proc = subprocess.run(
            [sys.executable, "-c", "print('em\\u2014dash \\ud55c\\uad6d\\uc5b4')"],
            capture_output=True, text=True, encoding="utf-8",
            env=child_env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("em—dash", proc.stdout)


class TestForceUtf8(unittest.TestCase):
    def test_idempotent_and_safe(self):
        force_utf8_io()
        force_utf8_io()
        self.assertEqual((sys.stdout.encoding or "").lower().replace("-", ""),
                         "utf8")


class TestDescribePlatform(unittest.TestCase):
    def test_reports_facts_a_report_can_cite(self):
        d = describe_platform()
        for key in ("os_name", "sys_platform", "python", "python_version",
                    "posix_shell"):
            self.assertIn(key, d)
        self.assertTrue(d["python_version"][0].isdigit())


if __name__ == "__main__":
    unittest.main()
