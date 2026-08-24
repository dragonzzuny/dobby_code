"""The spend ledger's token columns, and the three things they must not claim.

A status bar is read at a glance and cannot be qualified in a footnote, so the
line itself has to carry the qualifications: which model produced the tokens,
whether the total is complete, and whether the dollars cover the whole run.
"""

import json
import os
import tempfile
import unittest

from dobby.spend import Entry, record, statusline, summarize


class SpendCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def claude(self, **kw):
        usage = {"input_tokens": 10, "output_tokens": 100,
                 "thinking_tokens": 20, "cache_read_tokens": 5000,
                 "cache_creation_tokens": 900, "cost_usd": 0.25}
        usage.update(kw.pop("usage", {}))
        return record(self.data, provider="claude", duration_s=10, ok=True,
                      model="claude-opus-5[1m]", usage=usage, **kw)

    def codex(self, **kw):
        return record(self.data, provider="codex", duration_s=20, ok=True,
                      model="gpt-5.6-sol", **kw)


class TestEntry(SpendCase):
    def test_tokens_is_every_axis_summed(self):
        entry = self.claude()
        self.assertEqual(entry.tokens, 10 + 100 + 20 + 5000 + 900)

    def test_a_call_with_no_usage_is_unmeasured_not_free(self):
        entry = self.codex()
        self.assertFalse(entry.measured)
        self.assertEqual(entry.tokens, 0)
        self.assertIsNone(entry.cost_usd,
                          "a subscription provider reports no dollars; 0.0 "
                          "would sum into a total that reads as free")

    def test_the_model_is_recorded_even_when_the_cli_does_not_report_one(self):
        self.assertEqual(self.codex().model, "gpt-5.6-sol")


class TestBackwardCompatibility(SpendCase):
    def test_a_ledger_written_before_the_token_columns_still_parses(self):
        """`_read_tail` does `Entry(**json.loads(line))`; old lines lack these."""
        path = os.path.join(self.data, "state", "spend.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = {"t": 1.0, "provider": "claude", "role": "scout",
               "duration_s": 3.0, "ok": True, "label": "", "round_id": ""}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(old) + "\n")
        got = summarize(self.data, window_s=None)
        self.assertEqual(got["calls"], 1)
        self.assertEqual(got["tokens"], 0)
        self.assertFalse(got["tokens_complete"])


class TestSummary(SpendCase):
    def test_a_partially_measured_provider_is_marked_incomplete(self):
        self.claude()
        self.codex()
        got = summarize(self.data, window_s=None)
        self.assertTrue(got["providers"]["claude"]["complete"])
        self.assertFalse(got["providers"]["codex"]["complete"])
        self.assertFalse(got["tokens_complete"])

    def test_cost_sums_only_the_providers_that_reported_one(self):
        self.claude()
        self.codex()
        got = summarize(self.data, window_s=None)
        self.assertEqual(got["cost_usd_reported"], 0.25)
        self.assertIsNone(got["providers"]["codex"]["cost_usd"])

    def test_models_are_collected_per_provider(self):
        self.claude()
        record(self.data, provider="claude", duration_s=5, ok=True,
               model="claude-fable-5", usage={"output_tokens": 1})
        got = summarize(self.data, window_s=None)
        self.assertEqual(sorted(got["providers"]["claude"]["models"]),
                         ["claude-fable-5", "claude-opus-5[1m]"])

    def test_skills_are_collected(self):
        self.claude(skill="project-run")
        self.codex(skill="panel")
        self.assertEqual(summarize(self.data, window_s=None)["skills"],
                         ["panel", "project-run"])


class TestStatusLine(SpendCase):
    def test_idle_when_nothing_was_recorded(self):
        self.assertEqual(statusline(self.data), "dobby: idle")

    def test_every_provider_that_worked_appears(self):
        self.claude(skill="project-run")
        self.codex(skill="project-run")
        record(self.data, provider="agy", duration_s=9, ok=True,
               skill="project-run", model="gemini-3.5-flash-high",
               usage={"input_tokens": 300000})
        line = statusline(self.data)
        for provider in ("claude", "codex", "agy"):
            self.assertIn(provider, line)
        self.assertIn("skill project-run", line)

    def test_an_incomplete_total_is_marked_with_a_plus(self):
        self.claude()
        self.codex()
        line = statusline(self.data)
        self.assertIn("+", line,
                      "codex reported no usage, so every total containing it "
                      "is a floor and the line has to say so")

    def test_a_complete_total_carries_no_plus_on_the_total_segment(self):
        self.claude()
        segment = [s for s in statusline(self.data).split(" | ")
                   if s.startswith("total ")][0]
        self.assertNotIn("+", segment)

    def test_the_line_is_one_row_of_plain_text(self):
        self.claude()
        line = statusline(self.data)
        self.assertNotIn("\n", line)
        self.assertNotIn("\x1b", line)



class TestDashboard(SpendCase):
    """The multi-line block, and the bar it refuses to draw."""

    def _populate(self):
        self.claude(skill="ledgered-task")
        self.codex(skill="ledgered-task")

    def test_idle_when_nothing_was_recorded(self):
        from dobby.spend import dashboard
        self.assertEqual(dashboard(self.data), "dobby: idle")

    def test_every_provider_gets_a_bar_and_a_share(self):
        from dobby.spend import dashboard
        self._populate()
        line = [row for row in dashboard(self.data, window_s=None).splitlines()
                if "claude:" in row][0]
        self.assertIn("[", line)
        self.assertIn("%", line)

    def test_no_context_bar_without_a_measurement(self):
        """dobby cannot observe the host's window; it must not draw one."""
        from dobby.spend import dashboard
        self._populate()
        self.assertNotIn("ctx:", dashboard(self.data, window_s=None))
        self.assertIn("ctx:", dashboard(self.data, window_s=None,
                                        context_used=0.5))

    def test_the_skill_detail_is_clipped_not_wrapped(self):
        from dobby.spend import dashboard
        self._populate()
        out = dashboard(self.data, skill="s", detail="x" * 200, window_s=None)
        self.assertIn("…", out)
        self.assertTrue(all(len(row) < 200 for row in out.splitlines()))

    def test_a_partial_dollar_total_says_so(self):
        from dobby.spend import dashboard
        self._populate()
        self.assertIn("metered only", dashboard(self.data, window_s=None))

    def test_a_dimension_with_no_ceiling_gets_no_bar(self):
        """An unbounded budget drawn as full reads as exhausted."""
        from dobby.spend import _ratio
        self.assertNotIn("[", _ratio(500, None))
        self.assertIn("[", _ratio(500, 1000))

    def test_a_bar_is_clamped_to_its_width(self):
        from dobby.spend import BAR_WIDTH, _bar
        for share in (-1.0, 0.0, 0.5, 1.0, 5.0):
            self.assertEqual(len(_bar(share)), BAR_WIDTH + 2)

    def test_session_seconds_run_from_the_first_call_start(self):
        from dobby.spend import summarize
        self._populate()
        got = summarize(self.data, window_s=None)
        self.assertGreaterEqual(got["session_s"], 20.0,
                                "the first codex call ran 20s; the session "
                                "cannot be shorter than the work in it")

if __name__ == "__main__":
    unittest.main()
