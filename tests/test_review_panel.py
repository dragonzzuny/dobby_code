"""The critic panel: what it counts as a vote, and what it refuses to count.

Every test injects `round_runner`, so none of them needs a provider installed.
That seam exists in `review_panel.review_change` for the same reason
`architecture.request_architecture` takes `propose`.
"""

import os
import tempfile
import unittest

from dobby.project.readonly import ReadOnlyViolation
from dobby.project.review_panel import (APPROVE, MIN_EFFECTIVE_N, REJECT,
                                        UNREADABLE, ReviewVerdict,
                                        build_review_prompt, review_change)
from dobby.providers.fanout import FanoutRound
from dobby.providers.base import ProviderResult

DIFF = "--- a/money.py\n+++ b/money.py\n+def total(rows):\n+    return sum(rows)\n"
OUTCOME = "total() must sum the rows"


def _round(tasks, texts, ok=None):
    """A FanoutRound whose i-th result carries texts[i]."""
    oks = [True] * len(texts) if ok is None else list(ok)
    results = [ProviderResult(provider=t.provider_id, ok=oks[i],
                              text=texts[i],
                              error=None if oks[i] else "boom")
               for i, t in enumerate(tasks)]
    return FanoutRound(results=results, tasks=list(tasks), wall_s=1.0,
                       serial_s=2.0, isolated=False, concurrency=2)


def _approve(reason):
    return '{"verdict": "APPROVE", "reason": "%s"}' % reason


def _reject(reason):
    return '{"verdict": "REJECT", "reason": "%s"}' % reason


class TestPrompt(unittest.TestCase):
    def test_every_critic_is_asked_the_same_question(self):
        seen = []

        def runner(tasks):
            seen.extend(t.prompt for t in tasks)
            return _round(tasks, [_approve("fine")] * len(tasks))

        with tempfile.TemporaryDirectory() as d:
            review_change(DIFF, root=d, outcome=OUTCOME,
                          panel=["claude", "codex", "gemini"],
                          round_runner=runner)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1,
                         "critics must be asked one identical question, or "
                         "disagreement cannot be told from a different question")

    def test_the_prompt_says_the_checks_already_pass(self):
        text = build_review_prompt(DIFF, outcome=OUTCOME,
                                   acceptance_checks=("pytest -q",))
        self.assertIn("ALREADY passed", text)
        self.assertIn("pytest -q", text)
        self.assertIn(OUTCOME, text)
        self.assertIn(DIFF, text)

    def test_no_declared_checks_says_so_rather_than_showing_nothing(self):
        text = build_review_prompt(DIFF, outcome=OUTCOME)
        self.assertIn("(none declared)", text)


class TestVotes(unittest.TestCase):
    def _run(self, texts, ok=None, panel=("claude", "codex", "gemini")):
        with tempfile.TemporaryDirectory() as d:
            return review_change(
                DIFF, root=d, outcome=OUTCOME, panel=list(panel),
                round_runner=lambda tasks: _round(tasks, texts, ok))

    def test_unanimous_and_diverse_approval_is_approved(self):
        v = self._run([_approve("sums the rows as asked"),
                       _approve("behaviour matches the item, nothing dropped"),
                       _approve("correct; totals computed over every entry")])
        self.assertTrue(v.approved)
        self.assertTrue(v.independent)
        self.assertEqual([x.verdict for x in v.votes], [APPROVE] * 3)

    def test_one_rejection_blocks_even_against_two_approvals(self):
        v = self._run([_approve("looks right to me"),
                       _reject("negative amounts are silently dropped"),
                       _approve("fine by inspection")])
        self.assertFalse(v.approved)
        self.assertEqual(len(v.rejections), 1)
        self.assertIn("negative amounts", v.note)
        self.assertIn("codex", v.note)

    def test_prose_is_not_a_vote(self):
        v = self._run([_approve("it is correct and complete"),
                       "Looks good to me overall, ship it.",
                       _approve("delivers the item as written")])
        verdicts = [x.verdict for x in v.votes]
        self.assertEqual(verdicts[1], UNREADABLE)
        self.assertIn("prose", v.votes[1].error)
        self.assertTrue(v.approved, "two real approvals still carry the round")

    def test_an_unknown_verdict_word_is_unreadable_not_an_approval(self):
        v = self._run(['{"verdict": "LGTM", "reason": "sure"}',
                       '{"verdict": "MAYBE", "reason": "hm"}',
                       '{"verdict": "OK", "reason": "yes"}'])
        self.assertEqual([x.verdict for x in v.votes], [UNREADABLE] * 3)
        self.assertFalse(v.approved)
        self.assertIn("not reviewed", v.note)

    def test_a_failed_provider_is_recorded_not_counted(self):
        v = self._run([_approve("delivers what the item asked for"),
                       _approve("correct across the declared paths"),
                       "irrelevant"],
                      ok=[True, True, False])
        self.assertEqual(v.votes[2].verdict, UNREADABLE)
        self.assertEqual(v.votes[2].error, "boom")
        self.assertTrue(v.approved)

    def test_every_critic_failing_is_not_an_approval(self):
        v = self._run(["", "", ""], ok=[False, False, False])
        self.assertFalse(v.approved)
        self.assertIn("Not an approval", v.note)


class TestIndependence(unittest.TestCase):
    def test_identical_answers_are_flagged_as_one_opinion(self):
        same = _approve("the change is correct and delivers the work item")
        with tempfile.TemporaryDirectory() as d:
            v = review_change(DIFF, root=d, outcome=OUTCOME,
                              panel=["claude", "codex", "gemini"],
                              round_runner=lambda t: _round(t, [same] * 3))
        self.assertTrue(v.approved, "they did approve; that is not the point")
        self.assertFalse(v.independent)
        self.assertIn("one opinion", v.note)
        self.assertLess(v.diversity["effective_n"], MIN_EFFECTIVE_N)

    def test_a_failed_critic_does_not_silence_the_diversity_measurement(self):
        """Regression: labels were built from `members`, texts from OK results.

        With one critic down that was three labels against two texts, `analyze`
        raised, the score came back None, and the verdict said
        `independent: true` off a measurement that never ran. Measured on real
        providers 2026-08-24 when gemini failed authentication mid-round.
        """
        with tempfile.TemporaryDirectory() as d:
            v = review_change(
                DIFF, root=d, outcome=OUTCOME,
                panel=["codex", "claude", "gemini"],
                round_runner=lambda t: _round(t, [
                    _approve("sums every row including refunds"),
                    _approve("nothing the item asked for was dropped"),
                    ""], ok=[True, True, False]))
        self.assertIsNotNone(v.diversity,
                             "two surviving answers are still measurable")
        self.assertEqual(v.diversity["n"], 2)
        self.assertEqual(v.votes[2].verdict, UNREADABLE)

    def test_unmeasurable_diversity_is_reported_as_not_independent(self):
        # Force the unmeasurable branch the way a swarm import failure would.
        import dobby.project.review_panel as rp

        with tempfile.TemporaryDirectory() as d:
            original = rp._score
            rp._score = lambda texts, labels: None
            try:
                v = review_change(
                    DIFF, root=d, outcome=OUTCOME, panel=["claude", "codex"],
                    round_runner=lambda t: _round(t, [
                        _approve("delivers the item as written"),
                        _approve("correct over every declared path")]))
            finally:
                rp._score = original
        self.assertTrue(v.approved)
        self.assertFalse(v.independent,
                         "unmeasured is not the same as independent")
        self.assertIn("unproven", v.note)

    def test_a_single_critic_is_not_called_correlated(self):
        with tempfile.TemporaryDirectory() as d:
            v = review_change(DIFF, root=d, outcome=OUTCOME, panel=["claude"],
                              round_runner=lambda t: _round(t, [_approve("ok")]))
        self.assertTrue(v.approved)
        self.assertTrue(v.independent, "one answer cannot be correlated with "
                                       "answers that were never asked for")
        self.assertIsNone(v.diversity)


class TestReadOnly(unittest.TestCase):
    def test_a_round_that_moved_the_tree_is_discarded_whole(self):
        with tempfile.TemporaryDirectory() as d:
            def runner(tasks):
                with open(os.path.join(d, "sneaky.py"), "w") as fh:
                    fh.write("x = 1\n")
                return _round(tasks, [_approve("fine")] * len(tasks))

            with self.assertRaises(ReadOnlyViolation) as cm:
                review_change(DIFF, root=d, outcome=OUTCOME,
                              panel=["claude", "codex"], round_runner=runner)
        message = str(cm.exception)
        self.assertIn("discarded", message)
        self.assertIn("critic", message)
        self.assertIn("git status", message)

    def test_a_round_that_left_the_tree_alone_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "kept.py"), "w") as fh:
                fh.write("y = 2\n")
            v = review_change(
                DIFF, root=d, outcome=OUTCOME, panel=["claude", "codex"],
                round_runner=lambda t: _round(t, [
                    _approve("the totals are computed over every row"),
                    _approve("nothing in the item was left undone")]))
        self.assertTrue(v.approved)


class TestNoPanel(unittest.TestCase):
    def test_nobody_available_is_neither_approval_nor_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            v = review_change(DIFF, root=d, outcome=OUTCOME, panel=[])
        self.assertIsInstance(v, ReviewVerdict)
        self.assertFalse(v.approved)
        self.assertEqual(v.panel, ())
        self.assertEqual(v.votes, ())
        self.assertIn("nobody looked", v.note)

    def test_no_provider_is_called_when_the_panel_is_empty(self):
        called = []

        with tempfile.TemporaryDirectory() as d:
            review_change(DIFF, root=d, outcome=OUTCOME, panel=[],
                          round_runner=lambda t: called.append(t))
        self.assertEqual(called, [])


class TestSerialisation(unittest.TestCase):
    def test_to_dict_carries_the_fields_a_ledger_needs(self):
        with tempfile.TemporaryDirectory() as d:
            v = review_change(
                DIFF, root=d, outcome=OUTCOME, panel=["claude", "codex"],
                round_runner=lambda t: _round(t, [
                    _approve("delivers the item"),
                    _reject("drops the currency rounding rule")]))
        payload = v.to_dict()
        self.assertEqual(payload["approved"], False)
        self.assertEqual(payload["panel"], ["claude", "codex"])
        self.assertEqual(payload["votes"][1]["verdict"], REJECT)
        self.assertIn("rounding", payload["votes"][1]["reason"])
        self.assertIn("independent", payload)


if __name__ == "__main__":
    unittest.main()
