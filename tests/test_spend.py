import json
import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.spend import (SPEND_FILE, record, render_detail, statusline,
                         summarize)


def write_raw(data_dir, rows):
    """Write entries with explicit end timestamps, to control overlap."""
    path = os.path.join(data_dir, SPEND_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for end_t, provider, duration, ok in rows:
            f.write(json.dumps({
                "t": end_t, "provider": provider, "role": "draft",
                "duration_s": duration, "ok": ok, "label": "", "round_id": ""
            }) + "\n")


class TestParallelismMeasurement(unittest.TestCase):
    """The bug this class exists for: a round written at once reported 11050x."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_batch_written_round_does_not_explode(self):
        # All four written within milliseconds, as record_round does.
        for provider, duration in (("claude", 42.0), ("gemini", 18.0),
                                   ("codex", 56.0), ("agy", 12.0)):
            record(self.d, provider=provider, duration_s=duration, ok=True)
        summary = summarize(self.d)
        self.assertLessEqual(summary["parallelism"], 4.0,
                             "parallelism cannot exceed the call count")
        self.assertGreater(summary["parallelism"], 1.5)

    def test_wall_time_floored_at_the_slowest_call(self):
        for provider, duration in (("a", 5.0), ("b", 60.0)):
            record(self.d, provider=provider, duration_s=duration, ok=True)
        self.assertGreaterEqual(summarize(self.d)["wall_s"], 60.0)

    def test_fully_overlapping_calls(self):
        now = time.time()
        write_raw(self.d, [(now, "a", 40.0, True), (now, "b", 40.0, True),
                           (now, "c", 40.0, True), (now, "d", 40.0, True)])
        summary = summarize(self.d)
        self.assertAlmostEqual(summary["wall_s"], 40.0, delta=0.5)
        self.assertAlmostEqual(summary["parallelism"], 4.0, delta=0.1)

    def test_serial_calls_report_one(self):
        base = time.time() - 1000
        write_raw(self.d, [(base + 10, "a", 10.0, True),
                           (base + 110, "a", 10.0, True),
                           (base + 210, "a", 10.0, True)])
        summary = summarize(self.d)
        self.assertAlmostEqual(summary["parallelism"], 1.0, delta=0.01)
        self.assertAlmostEqual(summary["wall_s"], 30.0, delta=0.1)

    def test_gaps_between_rounds_are_not_counted_as_waiting(self):
        base = time.time() - 5000
        # Two rounds an hour apart; wall time is the work, not the gap.
        write_raw(self.d, [(base + 10, "a", 10.0, True),
                           (base + 3610, "a", 10.0, True)])
        self.assertAlmostEqual(summarize(self.d)["wall_s"], 20.0, delta=0.1)

    def test_partial_overlap_merged(self):
        base = time.time() - 500
        # [0,10) and [5,15) -> union is 15s, not 20s.
        write_raw(self.d, [(base + 10, "a", 10.0, True),
                           (base + 15, "b", 10.0, True)])
        summary = summarize(self.d)
        self.assertAlmostEqual(summary["wall_s"], 15.0, delta=0.1)
        self.assertAlmostEqual(summary["agent_s"], 20.0, delta=0.1)

    def test_single_call_is_never_superlinear(self):
        record(self.d, provider="a", duration_s=30.0, ok=True)
        self.assertLessEqual(summarize(self.d)["parallelism"], 1.0)


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        for provider, duration, ok in (("claude", 40.0, True),
                                       ("claude", 20.0, True),
                                       ("gemini", 10.0, True),
                                       ("agy", 5.0, False)):
            record(self.d, provider=provider, duration_s=duration, ok=ok)

    def test_per_provider_aggregation(self):
        s = summarize(self.d)
        self.assertEqual(s["calls"], 4)
        self.assertEqual(s["failed"], 1)
        self.assertEqual(s["providers"]["claude"]["calls"], 2)
        self.assertAlmostEqual(s["providers"]["claude"]["agent_s"], 60.0)
        self.assertAlmostEqual(s["providers"]["claude"]["mean_s"], 30.0)

    def test_busiest_is_by_time_not_call_count(self):
        self.assertEqual(summarize(self.d)["busiest"], "claude")

    def test_providers_sorted_by_time(self):
        order = list(summarize(self.d)["providers"])
        self.assertEqual(order[0], "claude")

    def test_window_restricts(self):
        s = summarize(self.d, window_s=0.001)
        self.assertLessEqual(s["calls"], 4)

    def test_empty_ledger(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(summarize(empty)["calls"], 0)

    def test_corrupt_line_does_not_break_the_status_prompt(self):
        path = os.path.join(self.d, SPEND_FILE)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertEqual(summarize(self.d)["calls"], 4)


class TestStatusline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def test_idle_when_nothing_recorded(self):
        self.assertEqual(statusline(self.d), "dobby: idle")

    def test_no_escape_codes_or_newline(self):
        record(self.d, provider="claude", duration_s=10.0, ok=True)
        line = statusline(self.d)
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\n", line)

    def test_running_agents_shown_with_elapsed(self):
        line = statusline(self.d, active=[
            {"label": "codex:security", "started": time.monotonic() - 75,
             "eta_s": 40}])
        self.assertIn("codex:security", line)
        self.assertIn("1m15s", line)

    def test_round_eta_is_the_max_not_the_sum(self):
        """A round finishes when its slowest member does."""
        line = statusline(self.d, active=[
            {"label": "a", "started": time.monotonic(), "eta_s": 30},
            {"label": "b", "started": time.monotonic(), "eta_s": 90}])
        self.assertIn("1m30s", line)
        self.assertNotIn("2m00s", line)

    def test_many_running_agents_are_truncated(self):
        active = [{"label": f"p{i}", "started": time.monotonic(), "eta_s": None}
                  for i in range(6)]
        self.assertIn("…", statusline(self.d, active=active))

    def test_failures_surfaced(self):
        record(self.d, provider="agy", duration_s=5.0, ok=False)
        record(self.d, provider="agy", duration_s=5.0, ok=True)
        self.assertIn("1 failed", statusline(self.d))

    def test_spend_segment_present(self):
        record(self.d, provider="claude", duration_s=42.0, ok=True)
        line = statusline(self.d)
        self.assertIn("agents 1", line)
        self.assertIn("top claude", line)


class TestDetailRender(unittest.TestCase):
    def test_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("no agent calls", render_detail(d))

    def test_table_has_a_row_per_provider_and_the_caveat(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, provider="claude", duration_s=40.0, ok=True)
            record(d, provider="gemini", duration_s=10.0, ok=True)
            out = render_detail(d)
            self.assertIn("claude", out)
            self.assertIn("gemini", out)
            self.assertIn("BOUGHT", out)
            self.assertIn("WAITED", out)

    def test_shares_sum_to_about_one(self):
        with tempfile.TemporaryDirectory() as d:
            record(d, provider="a", duration_s=75.0, ok=True)
            record(d, provider="b", duration_s=25.0, ok=True)
            out = render_detail(d)
            self.assertIn("75%", out)
            self.assertIn("25%", out)


if __name__ == "__main__":
    unittest.main()
