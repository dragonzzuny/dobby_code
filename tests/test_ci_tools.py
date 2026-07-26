"""The CI reporting tools are on the trust path, so they get tested too.

These scripts exist because job logs on this repository answer 403 without admin
rights while annotations answer 200, which made every red run undiagnosable. If
the reporter is wrong, a failure is reported as something other than what it is —
and shipping it unverified would repeat precisely the mistake that produced
fourteen unread red runs.

Two properties matter more than the rest:

  * The reporter must not die on the content it is reporting. The runner's stdout
    is cp1252, this project's output contains Korean, and cp1252 has no mapping
    for it. A UnicodeEncodeError raised inside the reporter replaces the real
    failure with a fake one.
  * `%` must be escaped before the escapes that introduce `%`, or an assertion
    message containing a percentage renders as `%250A` and the traceback is
    unreadable.
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from ci_annotate import (BLOCK_START, chunk, emit, escape, extract_blocks,  # noqa: E402
                         fallback_tail)

REALISTIC_LOG = """..........F...E
======================================================================
FAIL: test_passes_and_exits_zero (tests.test_doctor_damage.TestHealthyRepo.test_passes_and_exits_zero)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\\a\\dobby_code\\dobby_code\\tests\\test_doctor_damage.py", line 79
    self.assertEqual(payload["verdict"], "all checks pass")
AssertionError: 'usable, with 1 advisory gap(s): 100% bootstrapped' != 'all checks pass'
======================================================================
ERROR: test_thing (tests.test_other.T.test_thing)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "D:\\a\\x\\tests\\test_other.py", line 12, in test_thing
    os.path.relpath(a, b)
ValueError: path is on mount 'C:', start on mount 'D:'
----------------------------------------------------------------------
Ran 738 tests in 240.1s

FAILED (failures=1, errors=1)
"""


class TestEscaping(unittest.TestCase):
    def test_percent_is_escaped_before_the_escapes_that_use_it(self):
        """Wrong order yields %250A, which renders as literal text."""
        self.assertEqual(escape("50%\nnext"), "50%25%0Anext")

    def test_carriage_return_and_newline_both_encoded(self):
        self.assertEqual(escape("a\r\nb"), "a%0D%0Ab")

    def test_a_percent_heavy_message_round_trips_readably(self):
        out = escape("'usable, with 1 advisory gap(s): 100% bootstrapped'")
        self.assertIn("100%25", out)
        self.assertNotIn("%2525", out)


class TestBlockExtraction(unittest.TestCase):
    def setUp(self):
        self.blocks = extract_blocks(REALISTIC_LOG)

    def test_both_failures_are_found(self):
        self.assertEqual(len(self.blocks), 2, self.blocks)

    def test_the_first_block_is_whole(self):
        first = self.blocks[0]
        self.assertTrue(first.startswith("FAIL: test_passes_and_exits_zero"))
        self.assertIn("AssertionError", first)
        self.assertIn("line 79", first)

    def test_the_second_block_names_its_exception(self):
        self.assertIn("ValueError: path is on mount", self.blocks[1])

    def test_the_summary_line_is_not_swallowed_into_a_block(self):
        for block in self.blocks:
            self.assertNotIn("Ran 738 tests", block)

    def test_a_clean_log_yields_no_blocks(self):
        self.assertEqual(extract_blocks("....\nRan 4 tests in 0.1s\n\nOK\n"), [])

    def test_block_start_does_not_match_a_mere_mention(self):
        """'FAILED (failures=1)' is a summary, not a block header."""
        self.assertIsNone(BLOCK_START.match("FAILED (failures=1, errors=1)"))


class TestFallbackTail(unittest.TestCase):
    def test_a_traceback_with_no_unittest_blocks_still_reports(self):
        text = ('Traceback (most recent call last):\n'
                '  File "cli.py", line 163, in cmd_init\n'
                'FileExistsError: kg.bootstrap.json exists\n')
        tail = fallback_tail(text)
        self.assertEqual(len(tail), 1)
        self.assertIn("FileExistsError", tail[0])

    def test_blank_lines_are_dropped_so_the_tail_carries_content(self):
        text = "real line\n" + "\n" * 60
        self.assertIn("real line", fallback_tail(text, lines=5)[0])

    def test_empty_output_yields_nothing_rather_than_a_blank_annotation(self):
        self.assertEqual(fallback_tail("   \n\n"), [])


class TestChunking(unittest.TestCase):
    def test_a_short_message_is_one_chunk(self):
        self.assertEqual(chunk("abc", limit=100), ["abc"])

    def test_no_chunk_exceeds_the_limit(self):
        message = "".join(f"line {i} of some traceback\n" for i in range(400))
        parts = chunk(message, limit=200)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 200 + 40)   # one line of slack

    def test_chunking_loses_nothing(self):
        message = "".join(f"line {i}\n" for i in range(200))
        self.assertEqual("".join(chunk(message, limit=100)), message)

    def test_splits_happen_at_line_boundaries(self):
        message = "".join(f"line {i}\n" for i in range(100))
        for part in chunk(message, limit=100):
            self.assertTrue(part.endswith("\n") or part == "line 99\n",
                            repr(part[-20:]))


class _NarrowStdout(io.StringIO):
    """A stdout that can only hold one code page, like the runner's."""

    def __init__(self, encoding: str):
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, text: str) -> int:
        text.encode(self._encoding)     # raises exactly as the real one would
        return super().write(text)


class TestEmitSurvivesTheConsole(unittest.TestCase):
    """The failure that would replace a real finding with a fake one."""

    def _emit_through(self, encoding: str, message: str) -> str:
        fake = _NarrowStdout(encoding)
        real, sys.stdout = sys.stdout, fake
        try:
            emit(message)
        finally:
            sys.stdout = real
        return fake.getvalue()

    def test_a_plain_write_of_korean_to_cp1252_would_raise(self):
        """Establish that the hazard is real before testing the guard."""
        with self.assertRaises(UnicodeEncodeError):
            _NarrowStdout("cp1252").write("한국어")

    def test_emit_does_not_raise_on_korean_under_cp1252(self):
        out = self._emit_through("cp1252", "::error::한국어 failed")
        self.assertIn("::error::", out)
        self.assertIn("failed", out)

    def test_emit_does_not_raise_on_an_em_dash_under_cp949(self):
        """The complementary case: cp949 holds Korean but not an em dash."""
        out = self._emit_through("cp949", "::error::em—dash failed")
        self.assertIn("::error::", out)

    def test_representable_text_is_left_alone(self):
        out = self._emit_through("cp1252", "::error::plain ascii")
        self.assertEqual(out, "::error::plain ascii\n")


class TestCiStepDriver(unittest.TestCase):
    """End-to-end: the wrapper must relay the exit code and annotate failures."""

    def _run_step(self, child_code: str, title: str = "t") -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "ci_step.py"),
             "--title", title, "--", sys.executable, "-c", child_code],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO, timeout=300)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_a_passing_child_produces_no_annotation(self):
        rc, out = self._run_step("print('fine')")
        self.assertEqual(rc, 0)
        self.assertIn("fine", out)
        self.assertNotIn("::error", out)

    def test_the_childs_exit_code_is_relayed(self):
        rc, _ = self._run_step("import sys; sys.exit(3)")
        self.assertEqual(rc, 3)

    def test_a_failing_child_gets_an_annotation_with_context(self):
        rc, out = self._run_step(
            "import sys\n"
            "print('=' * 70)\n"
            "print('FAIL: test_x (tests.t.C.test_x)')\n"
            "print('AssertionError: 50% wrong')\n"
            "sys.exit(1)")
        self.assertEqual(rc, 1)
        self.assertIn("::error title=t (context)::", out)
        self.assertIn("FAIL: test_x", out)
        self.assertIn("50%25", out)          # escaped, not raw

    def test_the_context_annotation_names_the_variables_that_hid_the_bugs(self):
        _, out = self._run_step("import sys; sys.exit(1)")
        for field in ("py3.", "enc=", "utf8_mode=", "cwd=", "tmp="):
            self.assertIn(field, out, f"context is missing {field}")

    def test_a_missing_binary_is_named_not_traced(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "ci_step.py"),
             "--title", "t", "--", "dobby-no-such-binary-xyz"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 127)
        self.assertIn("cannot start", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout)

    def test_no_command_is_refused_rather_than_silently_passing(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "ci_step.py"),
             "--title", "t"],
            capture_output=True, text=True, cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 2)


class TestEnvReport(unittest.TestCase):
    def test_it_reports_the_four_values_that_mattered(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "ci_env_report.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        for key in ("stdout encoding", "utf8_mode", "tempdir", "same volume",
                    "provider CLIs found"):
            self.assertIn(key, proc.stdout)

    def test_the_report_is_ascii_so_it_survives_every_code_page(self):
        """A report that cannot print on cp1252 cannot report a cp1252 problem."""
        source = os.path.join(REPO, "tools", "ci_env_report.py")
        with open(source, encoding="utf-8") as f:
            body = f.read()
        # The module docstring is allowed prose; the emitted strings are not.
        emitted = [line for line in body.splitlines()
                   if "sys.stdout.write" in line or "print(" in line]
        for line in emitted:
            try:
                line.encode("cp1252")
            except UnicodeEncodeError:
                self.fail(f"emitting line is not cp1252-safe: {line.strip()}")


class TestWorkflowAndMirrorAgree(unittest.TestCase):
    """A mirror that has drifted from the workflow is worse than none."""

    def test_every_step_runs_through_the_reporter_or_is_a_known_exception(self):
        path = os.path.join(REPO, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        allowed_bare = ("python -m pip install", "python tools/ci_env_report.py")
        offenders = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("run:"):
                continue
            command = stripped[len("run:"):].strip()
            if command.startswith("python tools/ci_step.py"):
                continue
            if any(command.startswith(ok) for ok in allowed_bare):
                continue
            offenders.append(command)
        self.assertEqual(offenders, [],
                         "these steps would fail without a readable reason: "
                         + "; ".join(offenders))

    def test_the_workflow_sets_no_env_block(self):
        """PYTHONUTF8 here would hide the failures the matrix exists to find."""
        path = os.path.join(REPO, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            for line in f:
                self.assertFalse(line.rstrip().endswith("env:")
                                 and not line.lstrip().startswith("#"),
                                 "the workflow declares an env: block")


class TestAnnotateEntryPoint(unittest.TestCase):
    def test_a_log_on_disk_is_annotated(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "step.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write(REALISTIC_LOG)
            proc = subprocess.run(
                [sys.executable, os.path.join(REPO, "tools", "ci_annotate.py"),
                 log, "--title", "engine tests"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertEqual(proc.stdout.count("::error title=engine tests"), 3,
                         proc.stdout)

    def test_a_missing_log_says_so_instead_of_crashing(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "ci_annotate.py"),
             os.path.join(REPO, "no-such-file.log")],
            capture_output=True, text=True, cwd=REPO, timeout=120)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("no log at", proc.stdout)


if __name__ == "__main__":
    unittest.main()
