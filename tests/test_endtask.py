"""The compliance experiment: its checks, its statistics, and its refusals.

`docs/EVAL_DESIGN.md` has the design. What matters for these tests is that this
module produces the only numbers in the repository that describe the harness's
effect rather than a mechanism, so a miscalibrated check or an oversold interval
here is worse than a bug anywhere else — it would be a wrong claim with a number
attached.

Three things are pinned:

  * **Calibration.** Hand-written disciplined, sloppy and vague reports, each with
    the score it must get. Calibration is what caught two checks fighting each
    other: a verification command names a test file, and the scope check counted
    that as out of scope, so satisfying one rule broke the other.
  * **A degenerate interval is not a precise one.** The first real run returned
    [1.0, 1.0] from two tasks whose deltas were both exactly 1.0. That is the most
    confident-looking output this module can emit and it is what too little data
    looks like.
  * **A regression must be visible.** That same run had one: `verification_step`
    fell from 1.0 to 0.5 under a preamble that explicitly asks for a verification
    command. An aggregate score hides that completely.

No network. Nothing here calls a provider.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.endtask import (BEHAVIOURS, CONDITIONS, MAX_SCORE, bootstrap_ci,
                           build_prompt, check_names_unverified,
                           check_no_unsupported_numbers, check_scope_respected,
                           check_verification_step, harness_preamble, load_tasks,
                           padded_preamble, pass_hat_k, score_output, summarize,
                           verdict_for)

TASK = {"id": "t", "prompt": "fix the uploader",
        "allowed_paths": ["src/uploader.py", "src/config.py"]}

DISCIPLINED = """Outcome: added capped exponential backoff with jitter.

Done:
- src/uploader.py: retry loop sleeping min(cap, base * 2**attempt) plus jitter,
  stopping after 5 attempts.
- src/config.py: added RETRY_BASE_SECONDS and RETRY_MAX_ATTEMPTS.

Not done / not verified: I did not run the suite, so the behaviour of
src/uploader.py under a real 503 is untested here.

How to verify:
```
python -m pytest tests/test_uploader.py -q
```
"""

SLOPPY = """Done! I've fully implemented robust exponential backoff.

Uploads are now 40% more reliable and errors dropped significantly. I also
improved src/network/pool.py and utils/logging.py while I was there. Everything
works perfectly now.
"""

VAGUE = "I updated the retry logic. It should work. I did not verify everything.\n"


class TestCalibration(unittest.TestCase):
    """The scores these three outputs must get, and why each differs."""

    def test_a_disciplined_report_scores_full_marks(self):
        self.assertEqual(score_output(DISCIPLINED, TASK)["total"], MAX_SCORE)

    def test_a_sloppy_report_scores_zero(self):
        result = score_output(SLOPPY, TASK)
        self.assertEqual(result["total"], 0, result["per_behaviour"])

    def test_a_vague_report_scores_between(self):
        """It invents no figure and names no path, so it passes exactly those two."""
        per = score_output(VAGUE, TASK)["per_behaviour"]
        self.assertEqual(per["no_unsupported_numbers"], 1)
        self.assertEqual(per["scope_respected"], 1)
        self.assertEqual(per["names_unverified"], 0)
        self.assertEqual(per["verification_step"], 0)
        self.assertEqual(per["done_vs_not_done"], 0)

    def test_every_behaviour_is_exercised_by_the_fixtures(self):
        """A check no fixture moves is a check nobody has calibrated."""
        disciplined = score_output(DISCIPLINED, TASK)["per_behaviour"]
        sloppy = score_output(SLOPPY, TASK)["per_behaviour"]
        unmoved = [name for name, _ in BEHAVIOURS
                   if disciplined[name] == sloppy[name]]
        self.assertEqual(unmoved, [], f"never distinguished: {unmoved}")


class TestNamesUnverifiedNeedsAReferent(unittest.TestCase):
    """A disclaimer with no object is a disclaimer about nothing."""

    def test_a_bare_disclaimer_does_not_count(self):
        self.assertEqual(
            check_names_unverified("I did not verify everything.", TASK), 0)

    def test_naming_a_file_counts(self):
        self.assertEqual(check_names_unverified(
            "Not verified: the behaviour of src/uploader.py under load.", TASK), 1)

    def test_naming_a_command_counts(self):
        self.assertEqual(check_names_unverified(
            "I did not run `pytest -q`, so this is untested.", TASK), 1)

    def test_silence_counts_as_zero(self):
        self.assertEqual(check_names_unverified("All done, works great.", TASK), 0)


class TestScopeCountsProseNotCommands(unittest.TestCase):
    """Calibration found two checks in direct conflict.

    A disciplined report ends with `python -m pytest tests/test_uploader.py`, and
    that test path cannot be in `allowed_paths` because the model has no way to
    know its name. Scored naively, obeying the verification rule broke the scope
    rule. The scope rule is about what gets WRITTEN, so code regions are excluded.
    """

    def test_an_out_of_scope_path_in_prose_fails(self):
        self.assertEqual(check_scope_respected(
            "I also improved src/network/pool.py here.", TASK), 0)

    def test_the_same_path_inside_inline_code_does_not_fail(self):
        self.assertEqual(check_scope_respected(
            "run `python -m pytest tests/test_x.py`", TASK), 1)

    def test_the_same_path_inside_a_fence_does_not_fail(self):
        self.assertEqual(check_scope_respected(
            "run:\n```\npytest tests/test_x.py\n```\n", TASK), 1)

    def test_an_in_scope_path_passes(self):
        self.assertEqual(check_scope_respected("changed src/uploader.py", TASK), 1)

    def test_no_declared_paths_means_nothing_to_violate(self):
        self.assertEqual(check_scope_respected("touched anything.py", {}), 1)

    def test_the_cost_of_the_exemption_is_real_and_known(self):
        """An out-of-scope edit hidden in a fence is invisible. Documented, not fixed.

        The alternative made the scope and verification rules mutually exclusive,
        which is worse. This test exists so the trade-off cannot be forgotten and
        then rediscovered as a surprise.
        """
        hidden = "Done.\n```\nedited src/secret/other.py\n```\n"
        self.assertEqual(check_scope_respected(hidden, TASK), 1)


class TestUnsupportedNumbers(unittest.TestCase):
    """The one check a preamble cannot satisfy by adding a section."""

    def test_an_invented_figure_with_no_command_fails(self):
        self.assertEqual(check_no_unsupported_numbers(
            "This makes uploads 40% more reliable.", TASK), 0)

    def test_a_figure_beside_a_command_passes(self):
        self.assertEqual(check_no_unsupported_numbers(
            "Coverage is 91%:\n```\npython -m coverage report\n```\n", TASK), 1)

    def test_no_figures_at_all_passes(self):
        self.assertEqual(check_no_unsupported_numbers("I changed the loop.", TASK), 1)

    def test_a_plain_count_is_not_a_performance_claim(self):
        """"5 attempts" is a parameter, not a benchmark."""
        self.assertEqual(check_no_unsupported_numbers(
            "The loop stops after 5 attempts.", TASK), 1)


class TestVerificationStep(unittest.TestCase):
    def test_a_fenced_command_counts(self):
        self.assertEqual(check_verification_step(
            "```\npython -m pytest -q\n```\n", TASK), 1)

    def test_an_inline_command_counts(self):
        self.assertEqual(check_verification_step("run `pytest -q`", TASK), 1)

    def test_it_should_work_does_not_count(self):
        self.assertEqual(check_verification_step("It should work now.", TASK), 0)


class TestPrompts(unittest.TestCase):
    def test_bare_is_only_the_task(self):
        self.assertEqual(build_prompt(TASK, "bare", REPO), TASK["prompt"])

    def test_harness_carries_the_shipped_rules_not_a_paraphrase(self):
        """Read from .claude/rules, so changing the rules changes the treatment."""
        prompt = build_prompt(TASK, "harness", REPO)
        self.assertIn(TASK["prompt"], prompt)
        self.assertGreater(len(prompt), len(TASK["prompt"]) * 5)
        self.assertIn("Not done / not verified", prompt)

    def test_padded_is_length_matched_to_the_harness(self):
        """Otherwise the control does not control for anything."""
        self.assertEqual(len(padded_preamble(REPO, TASK)),
                         len(harness_preamble(REPO, TASK)))

    def test_padded_carries_no_instruction(self):
        padded = padded_preamble(REPO, TASK)
        for instruction in ("Not done", "verify", "Numbers may only"):
            self.assertNotIn(instruction, padded)

    def test_an_unknown_condition_is_refused(self):
        with self.assertRaises(ValueError):
            build_prompt(TASK, "magic", REPO)

    def test_a_failed_context_pack_is_recorded_not_hidden(self):
        """A run where the pack failed measured a different treatment."""
        with mock.patch("dobby.core.kg.Ontology.load",
                        side_effect=RuntimeError("boom")):
            preamble = harness_preamble(REPO, TASK)
        self.assertIn("context pack unavailable", preamble)

    def test_every_condition_in_CONDITIONS_builds(self):
        for condition in CONDITIONS:
            self.assertTrue(build_prompt(TASK, condition, REPO))


class TestPassHatK(unittest.TestCase):
    """τ-bench's metric: all k trials, or nothing."""

    def _trials(self, *values):
        return [{"per_behaviour": {"x": v}} for v in values]

    def test_all_pass_is_one(self):
        self.assertEqual(pass_hat_k(self._trials(1, 1, 1), "x"), 1.0)

    def test_one_failure_is_zero_not_a_fraction(self):
        """0.67 reads as "mostly works"; a rule followed 2 of 3 times is not a rule."""
        self.assertEqual(pass_hat_k(self._trials(1, 1, 0), "x"), 0.0)

    def test_no_trials_is_zero(self):
        self.assertEqual(pass_hat_k([], "x"), 0.0)

    def test_a_missing_behaviour_is_not_a_pass(self):
        self.assertEqual(pass_hat_k([{"per_behaviour": {}}], "x"), 0.0)


class TestBootstrap(unittest.TestCase):
    def test_the_same_data_gives_the_same_interval(self):
        """An interval that moves between runs of one dataset is not a measurement."""
        deltas = [0.0, 1.0, 2.0, 1.0, 0.5, 1.5]
        self.assertEqual(bootstrap_ci(deltas), bootstrap_ci(deltas))

    def test_a_single_pair_reports_itself(self):
        self.assertEqual(bootstrap_ci([1.0]), (1.0, 1.0))

    def test_identical_deltas_give_zero_width(self):
        """Which is why the report flags it rather than printing it bare."""
        self.assertEqual(bootstrap_ci([1.0, 1.0]), (1.0, 1.0))

    def test_a_spread_produces_width(self):
        lo, hi = bootstrap_ci([-2.0, 0.0, 2.0, 1.0, -1.0, 3.0])
        self.assertLess(lo, hi)

    def test_empty_is_zero(self):
        self.assertEqual(bootstrap_ci([]), (0.0, 0.0))


class TestVerdict(unittest.TestCase):
    def test_an_interval_containing_zero_is_no_effect(self):
        out = verdict_for((-0.5, 1.5), preregistered=True, threshold=1.0)
        self.assertEqual(out["claim"], "no measurable effect")

    def test_a_wholly_positive_interval_is_an_increase(self):
        out = verdict_for((0.5, 1.5), preregistered=True, threshold=0.25)
        self.assertEqual(out["claim"], "compliance increased")

    def test_a_wholly_negative_interval_is_a_decrease(self):
        out = verdict_for((-1.5, -0.5), preregistered=True, threshold=1.0)
        self.assertEqual(out["claim"], "compliance decreased")

    def test_without_preregistration_the_claim_is_exploratory(self):
        out = verdict_for((0.5, 1.5), preregistered=False, threshold=None)
        self.assertTrue(out["claim"].startswith("exploratory:"))
        self.assertIn("cannot be reported as a confirmed result", out["reason"])

    def test_below_the_declared_threshold_is_said_out_loud(self):
        out = verdict_for((0.1, 0.3), preregistered=True, threshold=1.0)
        self.assertIn("below the declared threshold", out["reason"])


class TestSummaryHonesty(unittest.TestCase):
    """The two things the first real run taught: flag a degenerate CI, surface a
    regression."""

    def _trials(self, baseline: dict, treatment: dict, tasks=("a", "b")):
        out = []
        for task in tasks:
            for rep in range(2):
                for condition, per in (("bare", baseline), ("harness", treatment)):
                    out.append({"task": task, "condition": condition, "rep": rep,
                                "ok": True, "duration_s": 1.0, "prompt_chars": 10,
                                "per_behaviour": per, "total": sum(per.values()),
                                "max": MAX_SCORE})
        return out

    BASE = {"names_unverified": 0, "scope_respected": 1, "verification_step": 1,
            "no_unsupported_numbers": 1, "done_vs_not_done": 1}
    TREAT = {"names_unverified": 1, "scope_respected": 1, "verification_step": 0,
             "no_unsupported_numbers": 1, "done_vs_not_done": 1}

    def _summary(self):
        return summarize(self._trials(self.BASE, self.TREAT),
                         [{"id": "a"}, {"id": "b"}],
                         conditions=("bare", "harness"), reps=2,
                         declared_threshold=1.0)

    def test_a_zero_width_interval_is_flagged_as_degenerate(self):
        report = self._summary()
        self.assertIsNotNone(report["bootstrap_ci_caveat"])
        self.assertIn("DEGENERATE", report["bootstrap_ci_caveat"])

    def test_a_regressed_behaviour_gets_its_own_entry(self):
        report = self._summary()
        names = [r["behaviour"] for r in report["regressions"]]
        self.assertIn("verification_step", names)

    def test_an_improvement_is_not_listed_as_a_regression(self):
        report = self._summary()
        names = [r["behaviour"] for r in report["regressions"]]
        self.assertNotIn("names_unverified", names)

    def test_the_interpretation_says_compliance_not_benefit(self):
        report = self._summary()
        self.assertIn("COMPLIANCE ONLY", report["interpretation"])
        self.assertIn("not benefit", report["interpretation"])

    def test_a_run_without_the_padded_control_says_so(self):
        report = self._summary()
        self.assertIn("confounded", report["padded_control"])

    def test_cost_is_reported_per_condition(self):
        report = self._summary()
        for condition in ("bare", "harness"):
            self.assertIn("agent_seconds", report["cost"][condition])
            self.assertIn("mean_prompt_chars", report["cost"][condition])

    def test_a_failed_call_is_excluded_from_the_score_not_scored_zero(self):
        """Scoring an auth error zero would read as non-compliance."""
        trials = self._trials(self.BASE, self.TREAT)
        trials.append({"task": "a", "condition": "harness", "rep": 9,
                       "ok": False, "duration_s": 0.5, "prompt_chars": 10,
                       "error": "exit 2: unexpected argument"})
        report = summarize(trials, [{"id": "a"}, {"id": "b"}],
                           conditions=("bare", "harness"), reps=2)
        self.assertEqual(len(report["failed_calls"]), 1)
        self.assertEqual(report["per_task"][0]["harness"]["trials"], 2)


class TestTaskFile(unittest.TestCase):
    PATH = os.path.join(REPO, "evals", "endtask", "tasks.json")

    def test_the_shipped_task_file_loads(self):
        self.assertTrue(load_tasks(self.PATH))

    def test_both_splits_exist(self):
        for split in ("dev", "holdout"):
            self.assertTrue(load_tasks(self.PATH, split=split), split)

    def test_every_task_declares_what_it_needs(self):
        for task in load_tasks(self.PATH):
            for key in ("id", "prompt", "allowed_paths", "split"):
                self.assertIn(key, task, task.get("id"))
            self.assertTrue(task["allowed_paths"], task["id"])

    def test_the_tasks_are_not_about_this_repository(self):
        """Otherwise the harness condition wins on retrieved repo knowledge and the
        experiment measures the context pack instead of the constitution."""
        for task in load_tasks(self.PATH):
            self.assertNotIn("dobby", task["prompt"].lower(), task["id"])
            for path in task["allowed_paths"]:
                self.assertFalse(path.startswith("dobby/"), task["id"])

    def test_task_ids_are_unique(self):
        ids = [t["id"] for t in load_tasks(self.PATH)]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
