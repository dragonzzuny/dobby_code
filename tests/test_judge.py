"""The judge adapter, and the property that makes it safe to have.

`.dobby/ontology.json` states:

    "model_assertion provenance is NEVER confidence=verified; verification
     requires a command/scan/eval_result source."

Before the adapter existed, `Evaluator.evaluate` put every record with a non-None
`passed` into the set that decides PASS/FAIL. Model-judgment criteria always
returned None, so the rule held by accident. The moment a judge started answering,
its opinion would have counted exactly as much as a test exit code — turning
"deterministic-first" into a false claim without a line of that sentence being
edited. The first test class below is that property; everything else is plumbing.

No network is used here. The provider layer is mocked, deliberately: a test that
needs a paid call to run is a test that does not run.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import dobby.judge as judge_mod
from dobby.core.evaluator import Evaluator
from dobby.judge import (MAX_ARTIFACT_CHARS, MAX_MODEL_CONFIDENCE, build_prompt,
                         judge_criterion, parse_verdict)

CRITERION = {"id": "final-message-honesty",
             "description": "the final message states the real verdict "
                            "including anything NOT verified",
             "kind": "model_judgment", "severity": "high"}


def _reply(text: str, ok: bool = True, error=None):
    return types.SimpleNamespace(ok=ok, text=text, error=error)


class TestAdvisoryCannotMoveTheVerdict(unittest.TestCase):
    """The whole reason this adapter is allowed to exist."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "criteria.json")

    def _write(self, criteria):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"criteria": criteria}, f)

    def _judged(self, verdict_text, extra_criteria=()):
        self._write([CRITERION, *extra_criteria])
        evaluator = Evaluator(self.path, self.dir, judge=True,
                              artifact="some final message")
        # judge_criterion imports from .providers inside the function, so the
        # module attribute is what resolves at call time.
        with mock.patch("dobby.providers.resolve_role", return_value="claude"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply(verdict_text)):
            return evaluator.evaluate()

    def test_a_model_pass_alone_does_not_produce_PASS(self):
        result = self._judged("VERDICT: PASS\nWHY: it names what was not verified")
        self.assertEqual(result["verdict"], "NO_DETERMINISTIC_CHECKS",
                         "a model opinion produced a verified verdict")
        self.assertEqual(len(result["advisory"]), 1)
        self.assertIs(result["advisory"][0]["passed"], True)

    def test_a_model_fail_alone_does_not_produce_FAIL(self):
        """Symmetry matters: an advisory record decides nothing in either direction."""
        result = self._judged("VERDICT: FAIL\nWHY: it overclaims")
        self.assertEqual(result["verdict"], "NO_DETERMINISTIC_CHECKS")
        self.assertIs(result["advisory"][0]["passed"], False)

    def test_a_model_fail_cannot_overturn_a_passing_deterministic_check(self):
        passing = {"id": "a-file-exists", "description": "criteria file is there",
                   "kind": "path_exists", "path": "criteria.json"}
        result = self._judged("VERDICT: FAIL\nWHY: I disagree", (passing,))
        self.assertEqual(result["verdict"], "PASS")

    def test_the_basis_states_how_many_were_excluded(self):
        result = self._judged("VERDICT: PASS\nWHY: fine")
        self.assertIn("1 advisory judgment(s) excluded", result["verdict_basis"])

    def test_confidence_never_reaches_certainty(self):
        result = self._judged("VERDICT: PASS\nWHY: fine")
        self.assertLessEqual(result["advisory"][0]["confidence"],
                             MAX_MODEL_CONFIDENCE)
        self.assertLess(result["advisory"][0]["confidence"], 1.0)


class TestOptInOnly(unittest.TestCase):
    """Judging spends money and leaves the machine, so it never happens by default."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "criteria.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"criteria": [CRITERION]}, f)

    def test_default_does_not_call_a_provider(self):
        called = []
        with mock.patch("dobby.providers.run_by_id",
                        side_effect=lambda *a, **k: called.append(1)):
            record = Evaluator(self.path, self.dir).evaluate()["records"][0]
        self.assertEqual(called, [], "a provider was called without opting in")
        self.assertIsNone(record["passed"])
        self.assertIn("opt-in", record["evidence"])

    def test_the_record_is_advisory_even_when_not_run(self):
        record = Evaluator(self.path, self.dir).evaluate()["records"][0]
        self.assertTrue(record["advisory"])

    def test_judging_without_an_artifact_says_so(self):
        record = Evaluator(self.path, self.dir,
                           judge=True).evaluate()["records"][0]
        self.assertIsNone(record["passed"])
        self.assertIn("no artifact", record["evidence"])


class TestVerdictParsing(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(parse_verdict("VERDICT: PASS\nWHY: because")[:2],
                         (True, "PASS"))

    def test_fail(self):
        self.assertEqual(parse_verdict("VERDICT: FAIL\nWHY: nope")[:2],
                         (False, "FAIL"))

    def test_unclear_is_not_a_pass_and_not_a_fail(self):
        passed, token, _ = parse_verdict("VERDICT: UNCLEAR\nWHY: truncated")
        self.assertIsNone(passed)
        self.assertEqual(token, "UNCLEAR")

    def test_prose_that_merely_contains_the_word_pass_is_not_a_verdict(self):
        """Lenient parsing is how a judge starts agreeing with everything."""
        passed, token, _ = parse_verdict(
            "I think this would pass most reasonable reviewers, honestly.")
        self.assertIsNone(passed)
        self.assertEqual(token, "UNPARSEABLE")

    def test_an_empty_reply_is_distinguished_from_an_unparseable_one(self):
        self.assertEqual(parse_verdict("")[1], "NO_REPLY")

    def test_the_why_line_is_captured(self):
        _, _, why = parse_verdict("VERDICT: FAIL\nWHY: it claims CI is green")
        self.assertEqual(why, "it claims CI is green")

    def test_a_verdict_after_preamble_is_still_found(self):
        passed, token, _ = parse_verdict("Sure, here is my grade.\n"
                                         "VERDICT: PASS\nWHY: fine")
        self.assertIs(passed, True)

    def test_a_lowercase_verdict_is_not_accepted(self):
        """The contract is exact; a near-miss is a signal the model drifted."""
        self.assertEqual(parse_verdict("verdict: pass")[1], "UNPARSEABLE")


class TestPromptConstruction(unittest.TestCase):
    def test_a_secret_in_the_artifact_is_redacted_before_sending(self):
        prompt = build_prompt(CRITERION, "token sk-abcdefghijklmnopqrstuvwx here")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", prompt)

    def test_the_criterion_text_reaches_the_prompt(self):
        self.assertIn("including anything NOT verified",
                      build_prompt(CRITERION, "x"))

    def test_truncation_is_declared_so_the_judge_can_answer_unclear(self):
        prompt = build_prompt(CRITERION, "y" * (MAX_ARTIFACT_CHARS + 500))
        self.assertIn("TRUNCATED", prompt)
        self.assertIn("reply UNCLEAR", prompt)

    def test_a_short_artifact_carries_no_truncation_claim(self):
        self.assertNotIn("TRUNCATED", build_prompt(CRITERION, "short"))

    def test_unclear_is_named_as_an_acceptable_answer(self):
        """A judge with no way to abstain will guess."""
        prompt = build_prompt(CRITERION, "x")
        self.assertIn("UNCLEAR is a correct", prompt)


class TestJudgeSelection(unittest.TestCase):
    def test_the_author_is_excluded_from_grading_its_own_work(self):
        seen = {}

        def fake_resolve(role, exclude=None):
            seen["role"] = role
            seen["exclude"] = exclude
            return "codex"

        with mock.patch("dobby.providers.resolve_role", fake_resolve), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("VERDICT: PASS\nWHY: ok")):
            judge_criterion(CRITERION, "artifact", exclude={"claude"})
        self.assertEqual(seen["role"], "critic")
        self.assertIn("claude", seen["exclude"])

    def test_no_available_judge_is_reported_as_not_run(self):
        with mock.patch("dobby.providers.resolve_role", return_value=None):
            record = judge_criterion(CRITERION, "artifact", exclude={"claude"})
        self.assertIsNone(record["passed"])
        self.assertIn("NOT RUN", record["evidence"])
        self.assertIn("claude", record["evidence"])

    def test_a_provider_failure_is_not_a_fail_verdict(self):
        """A judge that could not answer has not said the work is bad."""
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("", ok=False, error="exit 1")):
            record = judge_criterion(CRITERION, "artifact")
        self.assertIsNone(record["passed"])
        self.assertIn("NOT RUN", record["evidence"])
        self.assertEqual(record["confidence"], 0.0)

    def test_an_unparseable_reply_keeps_the_raw_text_as_evidence(self):
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("looks good to me")):
            record = judge_criterion(CRITERION, "artifact")
        self.assertIsNone(record["passed"])
        self.assertIn("raw reply", record["evidence"])
        self.assertIn("looks good to me", record["evidence"])

    def test_every_record_is_advisory_and_names_the_ontology(self):
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("VERDICT: PASS\nWHY: ok")):
            record = judge_criterion(CRITERION, "artifact")
        self.assertTrue(record["advisory"])
        self.assertIn("never verification", record["evidence"])
        self.assertEqual(record["judge_provider"], "codex")

    def test_a_forced_provider_skips_role_resolution(self):
        with mock.patch("dobby.providers.resolve_role",
                        side_effect=AssertionError("should not resolve")), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("VERDICT: PASS\nWHY: ok")):
            record = judge_criterion(CRITERION, "artifact", provider_id="agy")
        self.assertEqual(record["judge_provider"], "agy")


class TestSliceExposesTheFlag(unittest.TestCase):
    def test_the_evidence_string_names_a_flag_that_exists(self):
        """The not-run message tells the user to pass `dobby slice --judge`."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "slice", "--help"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("--judge", proc.stdout)
        self.assertIn("--judge-provider", proc.stdout)


if __name__ == "__main__":
    unittest.main()
