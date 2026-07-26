"""The SWE-bench runner, and the three defects it shipped with.

This module went in with no tests at all — the discipline failure this repository
keeps correcting elsewhere, committed here. Two real defects and one false alarm
came out of auditing it, and each is pinned below.

  1. **The write flag was hardcoded for one provider.** `-s workspace-write` is
     codex's. It was appended to whatever `--provider` named, and the others carry
     their own READ-ONLY default (`--permission-mode plan`, `--mode plan`,
     `--approval-mode plan`), so a run with `--provider claude` would have reported
     zero edits and read as a harness failure rather than a missing permission.
  2. **`fetch_instances` truncated silently.** `limit=250` returned 100 rows and
     said nothing, which makes every downstream rate a rate over a subset nobody
     chose.
  3. **A suspected defect that was not one.** `score_instance` drops files whose
     basename starts with `test_`, which would be catastrophic if a gold patch ever
     touched one. Measured across all 500 Verified instances: zero do. The filter
     stays, now with evidence rather than an assumption.

Network is used only where the test says so, and no test spends an agent call.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers import registry
from dobby.swebench import (AGENT_SANDBOX_NOTE, DEFAULT_DATASET, PAGE,
                            SweBenchError, build_prompt, changed_files,
                            gold_files, score_instance, summarize,
                            write_extra_for)


class TestWriteModeIsPerProvider(unittest.TestCase):
    """Defect 1: one provider's flag applied to all of them."""

    def test_codex_gets_its_own_sandbox_flag(self):
        self.assertEqual(write_extra_for("codex"), ("-s", "workspace-write"))

    def test_claude_gets_a_permission_mode_that_overrides_plan(self):
        """The catalog's argv ends with `--permission-mode plan`; extras win."""
        extra = write_extra_for("claude")
        self.assertEqual(extra, ("--permission-mode", "acceptEdits"))
        argv = registry().get("claude").build_argv("p", None, extra)
        self.assertEqual(argv[-2:], ["--permission-mode", "acceptEdits"])
        self.assertGreater(argv.index("acceptEdits"), argv.index("plan"))

    def test_a_provider_with_no_established_write_mode_is_refused(self):
        for pid in ("agy", "gemini", "qwen", "ollama"):
            with self.assertRaises(SweBenchError, msg=pid) as caught:
                write_extra_for(pid)
            self.assertIn("no verified write mode", str(caught.exception))

    def test_the_refusal_explains_why_silence_would_be_worse(self):
        with self.assertRaises(SweBenchError) as caught:
            write_extra_for("agy")
        message = str(caught.exception)
        self.assertIn("read-only", message)
        self.assertIn("look like a harness failure", message)

    def test_an_api_provider_is_refused_for_a_different_reason(self):
        with self.assertRaises(SweBenchError) as caught:
            write_extra_for("kimi")
        self.assertIn("api provider", str(caught.exception))

    def test_no_read_only_provider_silently_carries_codex_flags(self):
        """The shape of the original defect: `-s` on a tool that has no `-s`."""
        for pid in registry().ids():
            spec = registry().get(pid)
            if spec.kind != "cli" or not spec.write_extra:
                continue
            self.assertNotIn("-s", spec.write_extra[:1] if pid != "codex"
                             else (), f"{pid} carries codex's flag")


class TestGoldFileExtraction(unittest.TestCase):
    PATCH = (
        "diff --git a/requests/sessions.py b/requests/sessions.py\n"
        "--- a/requests/sessions.py\n"
        "+++ b/requests/sessions.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/requests/models.py b/requests/models.py\n"
        "--- a/requests/models.py\n"
        "+++ b/requests/models.py\n"
        "@@ -1 +1 @@\n-a\n+b\n")

    def test_both_targets_are_found(self):
        self.assertEqual(gold_files(self.PATCH),
                         ["requests/models.py", "requests/sessions.py"])

    def test_the_a_side_is_not_counted(self):
        """`--- a/...` is the pre-image; counting it would double every file."""
        self.assertEqual(len(gold_files(self.PATCH)), 2)

    def test_an_empty_patch_yields_nothing_rather_than_raising(self):
        self.assertEqual(gold_files(""), [])
        self.assertEqual(gold_files(None), [])


@unittest.skipUnless(shutil.which("git"), "git not available")
class TestChangedFileDetection(unittest.TestCase):
    """`git status --porcelain`, because `git diff` cannot see a new file."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        for argv in (["init", "--quiet"],
                     ["config", "user.email", "t@local"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", self.repo] + argv, check=True,
                           timeout=120)
        self._write("kept.py", "x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True,
                       timeout=120)
        subprocess.run(["git", "-C", self.repo, "commit", "--quiet", "-m", "b"],
                       check=True, timeout=120)

    def _write(self, rel, body):
        path = os.path.join(self.repo, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)

    def test_a_clean_baseline_reports_nothing(self):
        """Without this the run cannot attribute changes to the agent."""
        self.assertEqual(changed_files(self.repo), [])

    def test_a_modification_is_seen(self):
        self._write("kept.py", "x = 2\n")
        self.assertEqual(changed_files(self.repo), ["kept.py"])

    def test_a_NEW_file_is_seen(self):
        """`git diff --name-only` misses this one entirely."""
        self._write("added.py", "y = 1\n")
        self.assertIn("added.py", changed_files(self.repo))

    def test_a_nested_new_file_uses_forward_slashes(self):
        self._write("pkg/deep/new.py", "z = 1\n")
        self.assertIn("pkg/deep/new.py", changed_files(self.repo))


class TestScoring(unittest.TestCase):
    INSTANCE = {"instance_id": "x-1", "repo": "o/r",
                "patch": "--- a/src/a.py\n+++ b/src/a.py\n"}

    def _score(self, changed):
        with mock.patch("dobby.swebench.changed_files", return_value=changed):
            return score_instance(self.INSTANCE, "/ignored")

    def test_no_edit_at_all_is_reported(self):
        result = self._score([])
        self.assertFalse(result["made_any_edit"])
        self.assertFalse(result["localized_all_gold_files"])

    def test_hitting_exactly_the_gold_file(self):
        result = self._score(["src/a.py"])
        self.assertTrue(result["localized_all_gold_files"])
        self.assertEqual(result["extra_file_count"], 0)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)

    def test_an_extra_file_lowers_precision_but_keeps_the_hit(self):
        result = self._score(["src/a.py", "src/unrelated.py"])
        self.assertTrue(result["localized_all_gold_files"])
        self.assertEqual(result["extra_files"], ["src/unrelated.py"])
        self.assertEqual(result["precision"], 0.5)

    def test_missing_the_gold_file_is_not_a_hit(self):
        result = self._score(["src/other.py"])
        self.assertFalse(result["localized_all_gold_files"])
        self.assertFalse(result["localized_any_gold_file"])

    def test_a_multi_file_gold_needs_all_of_them(self):
        instance = {"instance_id": "x-2", "repo": "o/r",
                    "patch": "+++ b/a.py\n+++ b/b.py\n"}
        with mock.patch("dobby.swebench.changed_files", return_value=["a.py"]):
            result = score_instance(instance, "/ignored")
        self.assertFalse(result["localized_all_gold_files"])
        self.assertTrue(result["localized_any_gold_file"])
        self.assertEqual(result["recall"], 0.5)

    def test_an_agent_written_test_file_is_excluded(self):
        """The task says not to write tests; one written anyway is neither
        localization nor a scope violation of the fix."""
        result = self._score(["src/a.py", "tests/test_new.py"])
        self.assertEqual(result["extra_file_count"], 0)
        self.assertNotIn("tests/test_new.py", result["changed_files"])

    def test_resolved_is_absent_and_says_why(self):
        """The one field that must never be estimated."""
        result = self._score(["src/a.py"])
        self.assertIsNone(result["resolved"])
        self.assertIn("NOT MEASURED", result["resolved_note"])
        self.assertIn("Docker", result["resolved_note"])


class TestPrompts(unittest.TestCase):
    INSTANCE = {"instance_id": "x", "repo": "o/r", "patch": "",
                "problem_statement": "THE ISSUE TEXT"}

    def test_bare_carries_the_issue_and_no_preamble(self):
        prompt = build_prompt(self.INSTANCE, "bare", REPO)
        self.assertIn("THE ISSUE TEXT", prompt)
        self.assertNotIn("Not done / not verified", prompt)

    def test_harness_carries_the_issue_and_the_preamble(self):
        prompt = build_prompt(self.INSTANCE, "harness", REPO)
        self.assertIn("THE ISSUE TEXT", prompt)
        self.assertIn("Not done / not verified", prompt)
        self.assertGreater(len(prompt), len(build_prompt(
            self.INSTANCE, "bare", REPO)) * 3)

    def test_both_conditions_forbid_writing_tests(self):
        """Otherwise the agent can satisfy FAIL_TO_PASS by editing the test."""
        for condition in ("bare", "harness"):
            self.assertIn("Do not write tests",
                          build_prompt(self.INSTANCE, condition, REPO))


class TestSummary(unittest.TestCase):
    def _trial(self, condition, **over):
        base = {"instance_id": "i", "condition": condition, "ok": True,
                "duration_s": 10.0, "prompt_chars": 100, "made_any_edit": True,
                "localized_all_gold_files": True, "extra_file_count": 0,
                "precision": 1.0}
        base.update(over)
        return base

    def test_rates_are_per_condition(self):
        trials = [self._trial("bare", localized_all_gold_files=False),
                  self._trial("harness")]
        report = summarize(trials, conditions=("bare", "harness"))
        self.assertEqual(report["per_condition"]["bare"]
                         ["localized_all_gold_files"], 0.0)
        self.assertEqual(report["per_condition"]["harness"]
                         ["localized_all_gold_files"], 1.0)

    def test_a_failed_trial_is_excluded_from_rates_but_not_from_cost(self):
        """Scoring a crashed run as 0 localization would read as incapability."""
        trials = [self._trial("bare"),
                  self._trial("bare", ok=False, error="clone failed",
                              duration_s=5.0)]
        report = summarize(trials, conditions=("bare",))
        self.assertEqual(report["per_condition"]["bare"]["trials"], 1)
        self.assertEqual(report["per_condition"]["bare"]
                         ["localized_all_gold_files"], 1.0)
        self.assertEqual(report["per_condition"]["bare"]["agent_seconds"], 15.0)
        self.assertEqual(len(report["failed_trials"]), 1)

    def test_an_empty_condition_reports_none_rather_than_zero(self):
        """0.0 would mean "measured and failed"; None means "no data"."""
        report = summarize([self._trial("bare")],
                           conditions=("bare", "harness"))
        self.assertIsNone(report["per_condition"]["harness"]
                          ["localized_all_gold_files"])

    def test_the_report_refuses_to_call_itself_a_swebench_score(self):
        report = summarize([self._trial("bare")], conditions=("bare",))
        self.assertIsNone(report["resolved_rate"])
        self.assertIn("NOT a SWE-bench score", report["what_this_is_not"])
        self.assertIn("Docker", report["what_this_is_not"])

    def test_the_method_states_the_fresh_clone_requirement(self):
        report = summarize([self._trial("bare")], conditions=("bare",))
        self.assertIn("fresh clone", report["method"])


class TestAgainstTheRealDataset(unittest.TestCase):
    """Network, no agent calls. These pin facts about the published data.

    No class-level reachability guard, deliberately. There was one, and it made a
    single transient probe skip all six tests at import — observed twice here, and
    the second time the service answered 200 in 0.4s moments later. A one-shot
    guard also cannot cover mid-run degradation, which is exactly what reddened CI
    with a 502 after the guard had passed.

    So each test attempts its own call and skips only if THAT call fails on
    transport, with the exception in the skip reason. There is no module-level probe
    either: once the gate was gone nobody read its result, and it was still making
    a network round-trip at import on every platform. An unused import-time side
    effect is worse than the gate it replaced.
    """

    #: Transport failures that say nothing about this code.
    TRANSPORT = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                 OSError, json.JSONDecodeError)

    def _or_skip(self, call, *args, **kwargs):
        """Run `call`, or skip when the SERVICE failed rather than the code.

        CI failed on `HTTP Error 502: Bad Gateway`. The import-time guard had
        already passed, because the service was up when the module loaded and went
        down during the run - a one-shot probe cannot cover that, and the check
        belongs where the call is.

        Only transport is forgiven. A 200 with the wrong fields still fails, which
        is the whole point of these tests.
        """
        try:
            return call(*args, **kwargs)
        except self.TRANSPORT as exc:
            self.skipTest(f"datasets-server transport failure, not a code "
                          f"defect: {type(exc).__name__}: {str(exc)[:120]}")

    def test_pagination_satisfies_a_limit_over_one_page(self):
        """Defect 2: `limit=250` used to return 100 and say nothing."""
        from dobby.swebench import fetch_instances
        rows = self._or_skip(fetch_instances, limit=PAGE + 5)
        self.assertEqual(len(rows), PAGE + 5)

    def test_a_limit_within_one_page_is_exact(self):
        from dobby.swebench import fetch_instances
        rows = self._or_skip(fetch_instances, limit=7)
        self.assertEqual(len(rows), 7)

    def test_every_instance_has_the_fields_this_module_reads(self):
        from dobby.swebench import fetch_instances
        for row in self._or_skip(fetch_instances, limit=10):
            for field in ("repo", "instance_id", "base_commit", "patch",
                          "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS"):
                self.assertIn(field, row, row.get("instance_id"))

    def test_no_gold_patch_touches_a_test_underscore_file(self):
        """Defect 3, the false alarm: the `test_` filter cannot drop a gold file.

        Checked over one page rather than all 500 to keep the suite quick; the full
        500-instance scan was run once by hand and also found zero.
        """
        from dobby.swebench import fetch_instances
        offenders = []
        for row in self._or_skip(fetch_instances, limit=PAGE):
            for path in gold_files(row["patch"]):
                if os.path.basename(path).startswith("test_"):
                    offenders.append((row["instance_id"], path))
        self.assertEqual(offenders, [], offenders)

    def test_explicit_ids_are_found_without_the_caller_knowing_the_page(self):
        from dobby.swebench import find_instances
        found, missing = self._or_skip(find_instances, ["psf__requests-2317"])
        self.assertEqual(missing, [])
        self.assertEqual(found[0]["repo"], "psf/requests")

    def test_an_unknown_id_is_reported_as_missing_not_raised(self):
        from dobby.swebench import find_instances
        found, missing = self._or_skip(find_instances,
                                       ["not__a-real-instance-0"])
        self.assertEqual(found, [])
        self.assertEqual(missing, ["not__a-real-instance-0"])


class TestSandboxNoteIsHonest(unittest.TestCase):
    def test_the_module_says_full_access_is_deliberately_unused(self):
        self.assertIn("danger-full-access", AGENT_SANDBOX_NOTE)
        self.assertIn("liability", AGENT_SANDBOX_NOTE)


if __name__ == "__main__":
    unittest.main()
