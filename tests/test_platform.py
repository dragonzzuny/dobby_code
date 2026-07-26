import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import dobby.core.platform as platform_mod
from dobby.core.platform import (PYTHON_PLACEHOLDER, child_env,
                                 describe_platform, force_utf8_io,
                                 posix_shell_available, posix_shell_path,
                                 python_executable, resolve_command, shim_safe_argv)


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




class TestPosixShellIsProbedNotJustFound(unittest.TestCase):
    """The predicate that made windows-latest red on every commit.

    `posix_shell_available()` was `which("sh") or which("bash")`. On a GitHub
    Windows runner `bash` is C:\Windows\System32\bash.exe, the WSL launcher;
    with no distribution installed it prints a UTF-16LE error and exits 1. The
    check saw a file named bash and said yes, so a suite guarded by
    `skipUnless(posix_shell_available(), ...)` did not skip. It ran the installer
    through a shell that cannot execute anything, and seven assertions then failed
    with messages about missing files - none of which named the real cause.

    The guard existed. The predicate was the bug.
    """

    def setUp(self):
        posix_shell_path.cache_clear()
        self.addCleanup(posix_shell_path.cache_clear)

    def test_the_answer_is_a_usable_path_or_none(self):
        result = posix_shell_path()
        if result is not None:
            self.assertTrue(os.path.exists(result), result)

    def test_available_agrees_with_path(self):
        self.assertEqual(posix_shell_available(), posix_shell_path() is not None)

    def test_a_shell_that_exits_nonzero_is_rejected(self):
        """A WSL launcher with no distro behaves exactly like this."""
        stub = self._fake_shell(exit_code=1, stdout=b"")
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=[stub]):
            posix_shell_path.cache_clear()
            self.assertIsNone(posix_shell_path())

    def test_a_shell_that_prints_utf16_is_rejected(self):
        """The precise signature observed on the runner."""
        message = ("Windows Subsystem for Linux has no installed "
                   "distributions.").encode("utf-16-le")
        stub = self._fake_shell(exit_code=1, stdout=message)
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=[stub]):
            posix_shell_path.cache_clear()
            self.assertIsNone(posix_shell_path())

    def test_a_shell_that_exits_zero_but_says_nothing_is_rejected(self):
        """Exit 0 is not evidence; the token is."""
        stub = self._fake_shell(exit_code=0, stdout=b"")
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=[stub]):
            posix_shell_path.cache_clear()
            self.assertIsNone(posix_shell_path())

    def test_a_working_shell_is_accepted(self):
        stub = self._fake_shell(
            exit_code=0,
            stdout=platform_mod._POSIX_PROBE_TOKEN.encode("ascii"))
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=[stub]):
            posix_shell_path.cache_clear()
            self.assertEqual(posix_shell_path(), stub)

    def test_the_first_working_candidate_wins_over_a_broken_earlier_one(self):
        broken = self._fake_shell(exit_code=1, stdout=b"")
        good = self._fake_shell(
            exit_code=0,
            stdout=platform_mod._POSIX_PROBE_TOKEN.encode("ascii"))
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=[broken, good]):
            posix_shell_path.cache_clear()
            self.assertEqual(posix_shell_path(), good)

    def test_a_candidate_that_cannot_be_launched_does_not_raise(self):
        with mock.patch.object(platform_mod, "_posix_shell_candidates",
                               return_value=["dobby-no-such-shell-xyz"]):
            posix_shell_path.cache_clear()
            self.assertIsNone(posix_shell_path())

    def test_describe_platform_names_the_shell_it_accepted(self):
        """'True' is what let the wrong shell through unnoticed."""
        described = describe_platform()
        self.assertIn("posix_shell_path", described)
        self.assertEqual(described["posix_shell_path"], posix_shell_path())

    def _fake_shell(self, *, exit_code: int, stdout: bytes) -> str:
        """A real executable script that ignores -c and replies as told.

        Written as a Python script invoked through this interpreter, so it needs
        no shell to run - using a shell to fake a shell would be circular.
        """
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        payload = os.path.join(directory, "stub.py")
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                f"sys.stdout.buffer.write({stdout!r})\n"
                f"sys.exit({exit_code})\n")
        if os.name == "nt":
            launcher = os.path.join(directory, "stub.cmd")
            with open(launcher, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write(f'@"{sys.executable}" "{payload}" %*\n')
        else:
            launcher = os.path.join(directory, "stub")
            with open(launcher, "w", encoding="utf-8") as handle:
                handle.write(f'#!{sys.executable}\n'
                             f'import sys\n'
                             f'sys.stdout.buffer.write({stdout!r})\n'
                             f'sys.exit({exit_code})\n')
            os.chmod(launcher, 0o755)
        return launcher


class TestBatchShimTruncatesMultilineArguments(unittest.TestCase):
    """A `.CMD` shim silently drops everything after the first newline.

    Measured with one string through both routes:

        via .CMD shim    ["line one"]
        direct exe       ["line one\nline two\nline three"]

    Nothing else is affected - `%`, `&&`, `^` and `|` all arrive intact - so the
    hazard is narrow and was invisible until a multi-line prompt was sent. npm
    installs every one of its CLIs as a `.CMD` on Windows, so this reached the
    provider layer: a judge prompt came back "Ready to grade, but I'm missing the
    inputs" because the provider received only the preamble line and answered it.
    Nothing errored, and the reply read like an opinion about the work.

    The first test measures the truncation rather than trusting the claim.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.echo = os.path.join(self.dir, "argecho.py")
        with open(self.echo, "w", encoding="utf-8") as handle:
            handle.write("import sys, json\n"
                         "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n")

    def _make_shim(self, extension: str) -> str:
        path = os.path.join(self.dir, f"tool{extension}")
        if extension == ".ps1":
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f'& "{sys.executable}" "{self.echo}" @args\n')
        else:
            with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write("@echo off\r\n")
                handle.write(f'"{sys.executable}" "{self.echo}" %*\r\n')
        return path

    @unittest.skipUnless(os.name == "nt", "batch shims are a Windows concern")
    def test_the_truncation_is_real(self):
        shim = self._make_shim(".cmd")
        proc = subprocess.run([shim, "line one\nline two"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=120)
        self.assertIn("line one", proc.stdout)
        self.assertNotIn("line two", proc.stdout,
                         "if this passes, the shim no longer truncates and "
                         "shim_safe_argv's re-routing is no longer needed")

    @unittest.skipUnless(os.name == "nt", "batch shims are a Windows concern")
    def test_percent_and_operators_are_not_affected(self):
        """Scope the claim: only newlines are the problem."""
        shim = self._make_shim(".cmd")
        for arg in ("100% done", "a && b", "a ^ b | c"):
            proc = subprocess.run([shim, arg], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=120)
            self.assertIn(arg, proc.stdout, f"{arg!r} did not survive")

    def test_a_single_line_argument_is_left_on_the_direct_route(self):
        shim = self._make_shim(".cmd")
        argv, note = shim_safe_argv(shim, ["one line"])
        self.assertEqual(argv[0], shim)
        self.assertEqual(note, "")

    @unittest.skipUnless(os.name == "nt", "batch shims are a Windows concern")
    def test_a_multiline_argument_is_rerouted_through_the_ps1_sibling(self):
        self._make_shim(".cmd")
        ps1 = self._make_shim(".ps1")
        argv, note = shim_safe_argv(os.path.join(self.dir, "tool.cmd"),
                                    ["line one\nline two"])
        self.assertIsNotNone(argv, note)
        self.assertIn("powershell", argv[0].lower())
        self.assertIn("-File", argv)
        self.assertIn(ps1, argv)
        self.assertIn("truncates", note)

    @unittest.skipUnless(os.name == "nt", "batch shims are a Windows concern")
    def test_the_rerouted_argv_actually_delivers_the_whole_argument(self):
        """End to end: the point is the argument arriving, not the argv shape."""
        self._make_shim(".cmd")
        self._make_shim(".ps1")
        argv, _ = shim_safe_argv(os.path.join(self.dir, "tool.cmd"),
                                 ["line one\nline two\nline three"])
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180)
        self.assertIn("line two", proc.stdout, proc.stderr[-300:])
        self.assertIn("line three", proc.stdout, proc.stderr[-300:])

    @unittest.skipUnless(os.name == "nt", "batch shims are a Windows concern")
    def test_without_a_ps1_it_refuses_rather_than_truncating(self):
        """Silent truncation is the worst outcome available, so it is refused."""
        shim = self._make_shim(".cmd")
        argv, note = shim_safe_argv(shim, ["line one\nline two"])
        self.assertIsNone(argv)
        self.assertIn("only its first line", note)

    def test_a_real_executable_is_never_rerouted(self):
        argv, note = shim_safe_argv(sys.executable, ["-c", "print(1)\nprint(2)"])
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(note, "")

    def test_carriage_returns_count_as_multiline(self):
        """A CRLF prompt truncates exactly the same way."""
        if os.name != "nt":
            self.skipTest("batch shims are a Windows concern")
        shim = self._make_shim(".cmd")
        argv, note = shim_safe_argv(shim, ["line one\r\nline two"])
        self.assertIsNone(argv)
        self.assertTrue(note)


if __name__ == "__main__":
    unittest.main()
