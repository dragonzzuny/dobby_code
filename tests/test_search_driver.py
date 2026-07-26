"""The search driver, and the two things it must refuse to do.

`dobby/search.py` implemented the DRAFT/DEBUG/IMPROVE policy and had never
executed a model call — a recorded gap. Closing it introduces two ways to be
quietly wrong, and both are tested here rather than argued in a docstring.

1. **Scoring a model's output with the model's own opinion.** A search climbs
   whatever gradient it is handed. Given self-assessment it maximises
   self-assessment, reports steady improvement, and produces nothing better; the
   failure cannot be seen because the metric and the artifact have the same
   author. So a score comes from running a command, and a driver with no scorer
   yields no viable node and says why.

2. **Scoring a failed provider call as zero.** A timeout, an auth error, or a
   shim that cannot deliver the prompt is the absence of a candidate, not a bad
   one. Zero would make infrastructure trouble indistinguishable from a hard
   search problem, and the search would dutifully try to improve on it.

No network: the provider layer is mocked, and the command scorer is real.
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

from dobby.search import search
from dobby.search_driver import (CANDIDATE_PLACEHOLDER, command_scorer,
                                 driver_report, provider_expander,
                                 write_candidate)


def _reply(text, ok=True, error=None):
    return types.SimpleNamespace(ok=ok, text=text, error=error)


class _Base(unittest.TestCase):
    """Every test here gets a hard block on reaching a real provider.

    This is not belt-and-braces. `provider_expander` originally bound
    `resolve_role` and `run_by_id` when the expander was BUILT, and several tests
    build the expander outside their `mock.patch` block — so the patches did
    nothing and the "mocked" suite issued real paid calls, then hung on a 900s
    provider timeout. The import was moved into the call, and this guard makes the
    same class of mistake fail loudly instead of quietly spending money: any
    unmocked call raises, rather than dialling out.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.data = os.path.join(self.dir, ".dobby")
        os.makedirs(self.data, exist_ok=True)

        def _refuse(*args, **kwargs):
            raise AssertionError(
                "a test reached the real provider layer; the mock did not take "
                "effect where it was expected")

        for target in ("dobby.providers.run_by_id",
                       "dobby.providers.resolve_role"):
            patcher = mock.patch(target, side_effect=_refuse)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestCandidateFiles(_Base):
    def test_candidates_land_under_gitignored_state(self):
        path = write_candidate(self.data, 1, "hello")
        self.assertTrue(path.replace("\\", "/").endswith(
            ".dobby/state/search/candidate_001.txt"), path)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")

    def test_state_is_the_right_home_because_output_is_untrusted(self):
        """Both installers exclude .dobby/state; model text must not travel."""
        with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as handle:
            ignored = handle.read()
        self.assertIn(".dobby/state", ignored)


class TestCommandScorerRefusals(_Base):
    def test_a_command_without_the_placeholder_is_rejected(self):
        """It would score the same thing every time and look like a plateau."""
        with self.assertRaises(ValueError) as caught:
            command_scorer("echo 1", data_dir=self.data)
        self.assertIn(CANDIDATE_PLACEHOLDER, str(caught.exception))
        self.assertIn("plateau", str(caught.exception))

    def test_a_nonzero_exit_is_not_a_score_of_zero(self):
        score = command_scorer(
            f'{sys.executable} -c "import sys; sys.exit(3)" {CANDIDATE_PLACEHOLDER}',
            data_dir=self.data)
        value, why = score("content", 1)
        self.assertIsNone(value)
        self.assertIn("exited 3", why)

    def test_exit_zero_with_no_number_is_not_a_score_of_zero(self):
        score = command_scorer(
            f'{sys.executable} -c "print(\'done\')" {CANDIDATE_PLACEHOLDER}',
            data_dir=self.data)
        value, why = score("content", 1)
        self.assertIsNone(value)
        self.assertIn("not a score of zero", why)

    def test_a_blocked_command_reports_the_guard_not_a_number(self):
        """This originally EXECUTED. `rm -rf /` passed guard_command, and only
        GNU rm's own --no-preserve-root failsafe stopped it. The guard now refuses
        machine-level targets outright, so writing this test is what found that."""
        score = command_scorer(f"rm -rf / {CANDIDATE_PLACEHOLDER}",
                               data_dir=self.data)
        value, why = score("content", 1)
        self.assertIsNone(value)
        self.assertIn("blocked", why)

    def test_a_timeout_is_reported_as_such(self):
        score = command_scorer(
            f'{sys.executable} -c "import time; time.sleep(6)" '
            f'{CANDIDATE_PLACEHOLDER}',
            data_dir=self.data, timeout_s=2)
        value, why = score("content", 1)
        self.assertIsNone(value)
        self.assertIn("timed out", why)


class TestCommandScorerMeasures(_Base):
    def _counting_scorer(self):
        """Scores a candidate by how many of three markers it contains."""
        helper = os.path.join(self.dir, "score.py")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                "text = open(sys.argv[1], encoding='utf-8').read()\n"
                "print('checking')\n"
                "print(sum(m in text for m in ('alpha', 'beta', 'gamma')))\n")
        return command_scorer(
            f'{sys.executable} "{helper}" {CANDIDATE_PLACEHOLDER}',
            data_dir=self.data)

    def test_the_candidate_path_reaches_the_command(self):
        score = self._counting_scorer()
        value, why = score("alpha beta", 1)
        self.assertEqual(why, "")
        self.assertEqual(value, 2.0)

    def test_the_last_number_wins_so_a_command_may_print_progress(self):
        score = self._counting_scorer()
        value, _ = score("alpha beta gamma", 2)
        self.assertEqual(value, 3.0)

    def test_a_worse_candidate_scores_lower(self):
        score = self._counting_scorer()
        self.assertLess(score("alpha", 3)[0], score("alpha beta", 4)[0])


class TestExpanderTreatsFailureAsAbsence(_Base):
    def test_no_provider_yields_a_buggy_node(self):
        expand = provider_expander("task", score=None)
        with mock.patch("dobby.providers.resolve_role", return_value=None):
            out = expand("DRAFT", None, {"attempt": 1})
        self.assertTrue(out["buggy"])
        self.assertIn("no usable provider", out["error"])

    def test_a_provider_failure_is_not_scored_zero(self):
        expand = provider_expander("task", score=lambda c, i: (0.0, ""))
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("", ok=False, error="timeout")):
            out = expand("DRAFT", None, {"attempt": 1})
        self.assertTrue(out["buggy"])
        self.assertNotIn("score", out)
        self.assertIn("timeout", out["error"])

    def test_an_empty_reply_is_a_failure_not_an_empty_candidate(self):
        expand = provider_expander("task", score=lambda c, i: (1.0, ""))
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id", return_value=_reply("   ")):
            out = expand("DRAFT", None, {"attempt": 1})
        self.assertTrue(out["buggy"])
        self.assertIn("no output", out["error"])

    def test_without_a_scorer_the_candidate_is_uncomparable(self):
        expand = provider_expander("task", score=None)
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("a solution")):
            out = expand("DRAFT", None, {"attempt": 1})
        self.assertTrue(out["buggy"])
        self.assertIsNone(out["score"])
        self.assertIn("cannot be compared", out["error"])
        # The content is kept: it is real output, just not rankable.
        self.assertEqual(out["content"], "a solution")

    def test_a_scored_reply_is_viable(self):
        expand = provider_expander("task", score=lambda c, i: (0.75, ""))
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("a solution")):
            out = expand("DRAFT", None, {"attempt": 1})
        self.assertFalse(out["buggy"])
        self.assertEqual(out["score"], 0.75)
        self.assertEqual(out["meta"]["provider"], "codex")


class TestPromptsCarryWhatTheActionNeeds(_Base):
    def _captured_prompt(self, action, parent, context):
        seen = {}

        def spy(pid, prompt, **kwargs):
            seen["prompt"] = prompt
            return _reply("out")

        expand = provider_expander("THE TASK", score=lambda c, i: (1.0, ""))
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id", spy):
            expand(action, parent, context)
        return seen["prompt"]

    def test_draft_carries_the_task(self):
        prompt = self._captured_prompt("DRAFT", None, {"attempt": 1})
        self.assertIn("THE TASK", prompt)

    def test_debug_carries_the_failed_attempt_and_its_error(self):
        parent = types.SimpleNamespace(content="broken code", score=None,
                                       error="ZeroDivisionError")
        prompt = self._captured_prompt("DEBUG", parent, {"attempt": 2})
        self.assertIn("broken code", prompt)
        self.assertIn("ZeroDivisionError", prompt)

    def test_debug_without_a_recorded_error_says_so_rather_than_blank(self):
        parent = types.SimpleNamespace(content="x", score=None, error="")
        prompt = self._captured_prompt("DEBUG", parent, {"attempt": 2})
        self.assertIn("no comparable score", prompt)

    def test_improve_carries_the_score_and_what_was_tried(self):
        parent = types.SimpleNamespace(content="ok code", score=0.5, error="")
        prompt = self._captured_prompt(
            "IMPROVE", parent, {"attempt": 3, "summary": "tried A; tried B"})
        self.assertIn("0.5", prompt)
        self.assertIn("ok code", prompt)
        self.assertIn("tried A; tried B", prompt)


class TestDriverReportSeparatesAnsweredFromScored(_Base):
    def test_a_gap_between_answered_and_scored_is_visible(self):
        calls = [
            {"action": "DRAFT", "ok": True, "score": 1.0, "duration_s": 2.0},
            {"action": "DRAFT", "ok": True, "score": None, "duration_s": 3.0},
            {"action": "DEBUG", "ok": False, "duration_s": 0.5},
        ]
        report = driver_report(None, calls)
        self.assertEqual(report["provider_calls"], 3)
        self.assertEqual(report["answered"], 2)
        self.assertEqual(report["scored"], 1)
        self.assertEqual(report["unscored_answers"], 1)
        self.assertEqual(report["agent_seconds"], 5.5)
        self.assertEqual(report["by_action"], {"DRAFT": 2, "DEBUG": 1})

    def test_no_calls_reports_zeroes_rather_than_dividing(self):
        report = driver_report(None, [])
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["agent_seconds"], 0.0)


class TestEndToEndWithARealScorer(_Base):
    """The integration the gap was about: policy + provider + a real objective.

    The provider is mocked so this runs anywhere, but the scorer is a real
    subprocess reading a real file, and `search()` is the real policy.
    """

    def setUp(self):
        super().setUp()
        helper = os.path.join(self.dir, "score.py")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                "text = open(sys.argv[1], encoding='utf-8').read()\n"
                "print(sum(m in text for m in ('alpha', 'beta', 'gamma')))\n")
        self.score = command_scorer(
            f'{sys.executable} "{helper}" {CANDIDATE_PLACEHOLDER}',
            data_dir=self.data)

    def test_the_search_climbs_and_reports_a_viable_best(self):
        replies = iter(["alpha", "alpha beta", "alpha beta gamma",
                        "alpha beta gamma", "alpha beta gamma"])

        def spy(pid, prompt, **kwargs):
            return _reply(next(replies, "alpha beta gamma"))

        expand = provider_expander("collect the markers", score=self.score)
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id", spy):
            result = search(expand=expand, max_nodes=5, min_drafts=2,
                            debug_depth=2, patience=4)
        best = result.best
        self.assertIsNotNone(best, result.summary())
        self.assertEqual(best.score, 3.0)
        self.assertTrue(best.viable())
        report = driver_report(result, expand.calls)
        self.assertEqual(report["answered"], report["provider_calls"])
        self.assertEqual(report["unscored_answers"], 0)

    def test_with_no_scorer_nothing_becomes_viable(self):
        """The honest outcome: no objective, no ranking."""
        expand = provider_expander("collect the markers", score=None)
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("alpha beta gamma")):
            result = search(expand=expand, max_nodes=4, min_drafts=2,
                            debug_depth=2, patience=3)
        self.assertIsNone(result.best)

    def test_a_dead_provider_does_not_produce_a_best_node(self):
        expand = provider_expander("x", score=self.score)
        with mock.patch("dobby.providers.resolve_role", return_value="codex"), \
                mock.patch("dobby.providers.run_by_id",
                           return_value=_reply("", ok=False, error="exit 1")):
            result = search(expand=expand, max_nodes=4, min_drafts=2,
                            debug_depth=2, patience=3)
        self.assertIsNone(result.best)
        report = driver_report(result, expand.calls)
        self.assertEqual(report["answered"], 0)
        self.assertGreater(report["provider_calls"], 0)


class TestCliSurface(unittest.TestCase):
    def test_the_subcommand_exists_and_documents_the_placeholder(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "search", "--help"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("--score-command", proc.stdout)
        self.assertIn("{candidate}", proc.stdout)

    def test_a_score_command_without_the_placeholder_exits_with_a_reason(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "search", "task",
             "--score-command", "echo 1"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("{candidate}", proc.stderr)




class TestCliPayloadBuildsWithoutAProvider(unittest.TestCase):
    """The formatting step that threw away three paid provider calls.

    `cmd_search` built its output with
    `result.to_dict() if hasattr(result, "to_dict") else dict(result)`. SearchResult
    has neither, so the first live run made its calls, produced a result, and died
    with a TypeError while rendering it. The guard was what made the guess
    survivable long enough to reach runtime.

    Covered here with the search itself stubbed, so the payload contract is checked
    without spending anything.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _namespace(self, **over):
        base = dict(repo=REPO, task="t", score_command=None, score_timeout=30,
                    provider=None, timeout=None, max_nodes=2, min_drafts=1,
                    debug_depth=1, patience=1, lower_is_better=False)
        base.update(over)
        return types.SimpleNamespace(**base)

    def _fake_result(self, with_best):
        from dobby.search import Node, SearchResult
        best = Node(id="n1", parent=None, action="DRAFT", content="the code",
                    score=4.0) if with_best else None
        return SearchResult(best=best, nodes=[best] if best else [],
                            stopped_because="budget", drafts=1, debugs=0,
                            improves=0, buggy_count=0, holdout_score=None,
                            holdout_note="none", history=[])

    def _run(self, with_best):
        import dobby.cli as cli
        printed = {}
        with mock.patch.object(cli, "_out", lambda obj: printed.update(obj)), \
                mock.patch("dobby.search.search",
                           return_value=self._fake_result(with_best)), \
                mock.patch("dobby.search_driver.provider_expander") as expander:
            expander.return_value.calls = [
                {"action": "DRAFT", "ok": True, "score": 4.0, "duration_s": 1.0}]
            cli.cmd_search(self._namespace())
        return printed

    def test_the_payload_renders_and_carries_the_search_summary(self):
        payload = self._run(with_best=True)
        for key in ("best_score", "nodes_evaluated", "stopped_because",
                    "selection_bias_warning", "driver"):
            self.assertIn(key, payload, sorted(payload))

    def test_the_best_candidate_content_is_included(self):
        payload = self._run(with_best=True)
        self.assertEqual(payload["best"]["content"], "the code")
        self.assertEqual(payload["best"]["score"], 4.0)

    def test_no_best_node_omits_the_key_rather_than_faking_one(self):
        payload = self._run(with_best=False)
        self.assertNotIn("best", payload)
        self.assertIsNone(payload["best_score"])

    def test_the_missing_scorer_warning_is_present(self):
        payload = self._run(with_best=False)
        self.assertIn("no --score-command", payload["driver"]["warning"])

    def test_the_selection_bias_warning_survives_into_the_payload(self):
        """The number is optimistically biased and the output must say so."""
        payload = self._run(with_best=True)
        self.assertIn("optimistically biased", payload["selection_bias_warning"])


if __name__ == "__main__":
    unittest.main()
