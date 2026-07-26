import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.progress import (BAR_WIDTH, MIN_SAMPLES, Tracker, eta_waves,
                            render_report)
from dobby.sandbox import (SandboxError, extract, grep, run, sandbox_env,
                           sweep, _resolve_inside)

PY = sys.executable


class TestSandboxRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = self.tmp.name

    def test_large_output_never_enters_the_result(self):
        """The whole point: 200k of output, a few hundred bytes returned."""
        cmd = f'"{PY}" -c "print(\'line\' * 20 + chr(10), end=\'\'); ' \
              f'[print(\'x\' * 200) for _ in range(1000)]"'
        result = run(cmd, data_dir=self.data, timeout_s=60)
        self.assertTrue(result.ok, result.error)
        cost = result.context_cost()
        self.assertGreater(cost["bytes_produced"], 150_000)
        self.assertLess(cost["bytes_entered_context"], 3_000)
        self.assertGreater(cost["withheld_pct"], 95.0)

    def test_output_is_reachable_on_disk(self):
        cmd = f'"{PY}" -c "print(\'needle here\')"'
        result = run(cmd, data_dir=self.data, timeout_s=60)
        self.assertIsNotNone(result.stdout)
        self.assertTrue(result.stdout.exists())
        self.assertEqual(result.stdout.lines_total, 1)

    def test_exit_code_captured(self):
        result = run(f'"{PY}" -c "import sys; sys.exit(3)"',
                     data_dir=self.data, timeout_s=60)
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.ok)

    def test_failure_preview_comes_from_stderr(self):
        cmd = (f'"{PY}" -c "import sys; '
               f'sys.stderr.write(\'BOOM the real reason\'); sys.exit(1)"')
        result = run(cmd, data_dir=self.data, timeout_s=60)
        self.assertIn("BOOM the real reason", result.preview)

    def test_timeout_keeps_partial_output_and_says_so(self):
        cmd = (f'"{PY}" -c "import time,sys; print(\'before\'); '
               f'sys.stdout.flush(); time.sleep(30)"')
        result = run(cmd, data_dir=self.data, timeout_s=2)
        self.assertTrue(result.timed_out)
        self.assertIn("timeout", result.error)
        self.assertIn("output up to the kill is kept", result.error)

    def test_size_kill_reports_the_tail_is_missing(self):
        cmd = f'"{PY}" -c "[print(\'y\' * 1000) for _ in range(200000)]"'
        result = run(cmd, data_dir=self.data, timeout_s=60,
                     max_capture_bytes=200_000)
        self.assertTrue(result.killed_for_size)
        self.assertIn("tail is missing", result.error)

    def test_destructive_command_is_blocked(self):
        result = run("rm -rf /", data_dir=self.data,
                     protected_paths=["/"])
        self.assertIsNone(result.exit_code)
        self.assertIn("guard", result.error)

    def test_network_blocked_is_reported_false_not_claimed(self):
        """The module must not imply isolation it does not provide."""
        result = run(f'"{PY}" -c "print(1)"', data_dir=self.data, timeout_s=60)
        self.assertFalse(result.network_blocked)

    def test_missing_cwd_raises(self):
        with self.assertRaises(SandboxError):
            run("echo x", data_dir=self.data, cwd="/definitely/not/here/xyz")

    def test_empty_stream_produces_no_capture(self):
        result = run(f'"{PY}" -c "pass"', data_dir=self.data, timeout_s=60)
        self.assertIsNone(result.stdout)
        self.assertIsNone(result.stderr)


class TestConfinementInTheExecutionPath(unittest.TestCase):
    """Regression: the helper existed, was unit-tested, and was NEVER CALLED.

    `_resolve_inside` passed its own tests while `run()` ignored it entirely, so
    the module documented a control it did not apply. Testing a helper in
    isolation is exactly how that stays invisible — these tests go through
    `run()`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.inside = os.path.join(self.root.name, "sub")
        os.makedirs(self.inside, exist_ok=True)

    def test_cwd_inside_root_is_allowed(self):
        result = run(f'"{PY}" -c "print(1)"', data_dir=self.tmp.name,
                     cwd=self.inside, root=self.root.name, timeout_s=60)
        self.assertTrue(result.ok, result.error)

    def test_cwd_outside_root_is_refused_before_launch(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        with self.assertRaises(SandboxError) as ctx:
            run(f'"{PY}" -c "print(1)"', data_dir=self.tmp.name,
                cwd=other, root=self.root.name, timeout_s=60)
        self.assertIn("escapes the sandbox root", str(ctx.exception))

    def test_no_root_means_no_confinement_and_that_is_documented(self):
        """The default is an absence, not an implied guarantee."""
        other = tempfile.mkdtemp()
        result = run(f'"{PY}" -c "print(1)"', data_dir=self.tmp.name,
                     cwd=other, timeout_s=60)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.cwd, os.path.abspath(other))


class TestPathConfinement(unittest.TestCase):
    def test_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SandboxError):
                _resolve_inside(root, os.path.join("..", "..", "etc"))

    def test_inside_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            path = _resolve_inside(root, "sub/file.txt")
            self.assertTrue(path.startswith(os.path.realpath(root)))

    def test_root_itself_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(_resolve_inside(root, "."),
                             os.path.realpath(root))


class TestSandboxEnv(unittest.TestCase):
    def test_proxies_cleared_when_network_disallowed(self):
        os.environ["HTTPS_PROXY"] = "http://example:8080"
        try:
            env = sandbox_env(allow_network=False)
            self.assertNotIn("HTTPS_PROXY", env)
            self.assertEqual(env["DOBBY_SANDBOX"], "1")
        finally:
            os.environ.pop("HTTPS_PROXY", None)

    def test_proxies_kept_when_network_allowed(self):
        os.environ["HTTPS_PROXY"] = "http://example:8080"
        try:
            self.assertIn("HTTPS_PROXY", sandbox_env(allow_network=True))
        finally:
            os.environ.pop("HTTPS_PROXY", None)

    def test_utf8_always_pinned(self):
        self.assertEqual(sandbox_env(allow_network=False)["PYTHONUTF8"], "1")


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        body = "\\n".join([f"line {i}" for i in range(1, 51)]
                          + ["ERROR: the thing broke", "trailing"])
        self.result = run(f'"{PY}" -c "print(\'{body}\'.replace(chr(92)+chr(110), chr(10)))"',
                          data_dir=self.tmp.name, timeout_s=60)
        self.assertTrue(self.result.ok, self.result.error)
        self.cap = self.result.stdout

    def test_grep_returns_only_matches(self):
        out = grep(self.cap, "ERROR")
        self.assertEqual(out["matched_lines"], 1)
        self.assertIn("the thing broke", out["text"])
        self.assertNotIn("line 20", out["text"])

    def test_context_lines_around_a_match(self):
        out = extract(self.cap, pattern="ERROR", around=2)
        self.assertGreaterEqual(out["matched_lines"], 3)

    def test_head_and_tail(self):
        self.assertEqual(extract(self.cap, head=3)["returned_lines"], 3)
        self.assertIn("trailing", extract(self.cap, tail=2)["text"])

    def test_line_numbers_are_included(self):
        self.assertIn("1: line 1", extract(self.cap, head=1)["text"])

    def test_combining_selectors_is_refused_not_guessed(self):
        with self.assertRaises(SandboxError) as ctx:
            extract(self.cap, pattern="line", head=5)
        self.assertIn("ambiguous", str(ctx.exception))

    def test_line_cap_is_reported(self):
        out = extract(self.cap, pattern="line", max_lines=5)
        self.assertTrue(out["line_cap_hit"])
        self.assertIn("capped", out["note"])

    def test_char_cap_is_reported(self):
        out = extract(self.cap, pattern="line", max_chars=40)
        self.assertTrue(out["char_cap_hit"])

    def test_uncapped_result_says_complete(self):
        out = extract(self.cap, pattern="ERROR")
        self.assertIn("complete", out["note"])

    def test_bad_pattern_is_an_error_not_a_crash(self):
        self.assertIn("error", extract(self.cap, pattern="[unclosed"))

    def test_missing_capture_reports_cleanly(self):
        os.remove(self.cap.path)
        self.assertIn("error", extract(self.cap, head=1))


class TestSweep(unittest.TestCase):
    def test_old_captures_removed_recent_kept(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run(f'"{PY}" -c "print(\'x\')"', data_dir=tmp.name, timeout_s=60)
        fresh = sweep(tmp.name, keep_hours=24.0)
        self.assertEqual(fresh["removed"], 0)
        self.assertGreaterEqual(fresh["kept"], 1)
        aged = sweep(tmp.name, keep_hours=0.0)
        self.assertGreaterEqual(aged["removed"], 1)


# ======================================================================
class TestEtaRefusal(unittest.TestCase):
    def test_refuses_below_min_samples(self):
        """One or two completions measure those units, not a rate."""
        t = Tracker(total=100)
        for _ in range(MIN_SAMPLES - 1):
            t.complete_unit(1.0)
        eta = t.eta()
        self.assertFalse(eta["estimable"])
        self.assertIn("samples needed", eta["reason"])

    def test_estimates_once_enough_samples_exist(self):
        t = Tracker(total=10)
        for _ in range(MIN_SAMPLES):
            t.complete_unit(2.0)
        eta = t.eta()
        self.assertTrue(eta["estimable"])
        self.assertAlmostEqual(eta["remaining_s"], 14.0, places=1)

    def test_unknown_total_gives_a_rate_never_a_completion_time(self):
        t = Tracker(total=None)
        for _ in range(5):
            t.complete_unit(1.0)
        eta = t.eta()
        self.assertFalse(eta["estimable"])
        self.assertIn("no completion time", eta["reason"])
        self.assertIsNotNone(eta["rate_per_min"])

    def test_complete_reports_zero(self):
        t = Tracker(total=3)
        for _ in range(3):
            t.complete_unit(1.0)
        self.assertEqual(t.eta()["remaining_s"], 0.0)


class TestEtaRange(unittest.TestCase):
    def test_erratic_work_widens_the_range(self):
        steady = Tracker(total=20)
        erratic = Tracker(total=20)
        for _ in range(5):
            steady.complete_unit(2.0)
        for d in (0.5, 8.0, 1.0, 6.0, 2.0):
            erratic.complete_unit(d)
        s = steady.eta()["remaining_range_s"]
        e = erratic.eta()["remaining_range_s"]
        self.assertLess(s[1] - s[0], e[1] - e[0])

    def test_range_is_clamped_to_observed_extremes(self):
        t = Tracker(total=10)
        for d in (1.0, 1.0, 1.0, 9.0):
            t.complete_unit(d)
        eta = t.eta()
        per = eta["per_unit"]
        remaining = eta["remaining"]
        low, high = eta["remaining_range_s"]
        self.assertGreaterEqual(low, remaining * per["min"] - 1e-6)
        self.assertLessEqual(high, remaining * per["max"] + 1e-6)

    def test_upper_bound_never_below_the_central_estimate(self):
        t = Tracker(total=10)
        for d in (5.0, 5.0, 5.0):
            t.complete_unit(d)
        eta = t.eta()
        self.assertLessEqual(eta["remaining_range_s"][0], eta["remaining_s"])
        self.assertGreaterEqual(eta["remaining_range_s"][1], eta["remaining_s"])

    def test_caveat_always_present(self):
        t = Tracker(total=10)
        for _ in range(4):
            t.complete_unit(1.0)
        self.assertIn("observed spread", t.eta()["caveat"])


class TestTrackerBookkeeping(unittest.TestCase):
    def test_failures_counted_separately_but_still_consume_time(self):
        t = Tracker(total=10)
        t.complete_unit(2.0)
        t.complete_unit(2.0, failed=True)
        t.complete_unit(2.0)
        self.assertEqual(t.done, 3)
        self.assertEqual(t.succeeded, 2)
        self.assertEqual(t.failures, 1)
        self.assertAlmostEqual(t.per_unit_stats()["mean"], 2.0)

    def test_start_and_complete_measures_wall_clock(self):
        t = Tracker(total=2)
        t.start_unit()
        time.sleep(0.05)
        t.complete_unit()
        self.assertGreater(t.durations[0], 0.01)

    def test_fraction_and_remaining(self):
        t = Tracker(total=4)
        t.complete_unit(1.0)
        self.assertEqual(t.fraction, 0.25)
        self.assertEqual(t.remaining, 3)

    def test_negative_duration_clamped(self):
        t = Tracker(total=1)
        t.complete_unit(-5.0)
        self.assertEqual(t.durations[0], 0.0)


class TestBar(unittest.TestCase):
    def test_bar_has_no_escape_codes(self):
        t = Tracker(label="panel", total=8)
        for _ in range(4):
            t.complete_unit(1.0)
        bar = t.bar()
        self.assertNotIn("\x1b", bar)
        self.assertNotIn("\r", bar)
        self.assertIn("4/8", bar)

    def test_bar_says_when_eta_is_not_estimable(self):
        t = Tracker(total=10)
        t.complete_unit(1.0)
        self.assertIn("not yet estimable", t.bar())

    def test_unknown_total_bar(self):
        t = Tracker(total=None)
        t.complete_unit(1.0)
        self.assertIn("?", t.bar())

    def test_failures_shown(self):
        t = Tracker(total=5)
        t.complete_unit(1.0, failed=True)
        self.assertIn("1 failed", t.bar())

    def test_width_respected(self):
        t = Tracker(total=10)
        t.complete_unit(1.0)
        self.assertIn("-" * 5, t.bar(width=BAR_WIDTH))

    def test_render_report_handles_empty(self):
        self.assertIn("nothing in progress", render_report([]))


class TestWaveEta(unittest.TestCase):
    def test_extrapolates_on_waves_not_items(self):
        out = eta_waves(waves_total=4, waves_done=2, wave_durations=[10.0, 12.0])
        self.assertTrue(out["estimable"])
        self.assertAlmostEqual(out["remaining_s"], 22.0, places=1)

    def test_refuses_before_the_first_wave_finishes(self):
        out = eta_waves(waves_total=3, waves_done=0, wave_durations=[])
        self.assertFalse(out["estimable"])
        self.assertIn("slowest member", out["reason"])

    def test_caveat_names_the_slowest_member_effect(self):
        out = eta_waves(waves_total=3, waves_done=1, wave_durations=[5.0])
        self.assertIn("SLOWEST", out["caveat"])

    def test_complete(self):
        out = eta_waves(waves_total=2, waves_done=2, wave_durations=[1.0, 1.0])
        self.assertEqual(out["remaining_s"], 0.0)

    def test_zero_waves(self):
        self.assertFalse(eta_waves(waves_total=0, waves_done=0,
                                   wave_durations=[])["estimable"])


if __name__ == "__main__":
    unittest.main()
