"""A verdict about August, printed in the tense of today.

`friction_report` answered `verdict: "clean"` and said nothing about when it
had looked. Measured on this repository on 2026-09-04:

    tasks_scanned  4
    newest event   2026-08-18T18:19:02      (18 days earlier)
    verdict        clean

Nothing was wrong with the scan. What was wrong is that "clean" is present
tense and the evidence was seventeen days old, and the reader had no way to
tell without going to look -- the same shape as `flywheel.report` telling the
reader to "check the run count" while holding the run count.

The second half of the same finding: `.dobby/state/audit.jsonl` was read by
NOTHING. Grepping every reader of every evidence store in `dobby/`, `mcp/` and
`tools/` returned one writer for it and no readers, while it was the only store
here still growing -- 394 entries reaching 2026-09-04 against trajectories that
stopped on 2026-08-18. It only became worth reading once the gateway began
recording outcomes rather than intentions.

What the report says about this repository now:

    window       oldest 2026-08-16, newest 2026-08-18, 18 days
    stale        true
    verdict      no friction over 4 task(s) ending 2026-08-18T18:19:02,
                 but that is 18 day(s) old: this is a verdict about then,
                 not a verdict about now
    audit        394 entries, 30 recording an outcome,
                 capabilities ever invoked: kg_query 182, env_probe 91

The `now` argument exists so the staleness boundary can be asserted without the
assertions depending on the day they run -- a test whose result changes with the
calendar is the thing this module is about.
"""

import io
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.friction import friction_report  # noqa: E402
from dobby.core.trajectory import Trajectory  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = self.tmp.name

    def audit(self, rows):
        path = os.path.join(self.data, "state", "audit.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def a_quiet_task(self):
        t = Trajectory(self.data, "demo")
        t.append("evidence", {"detail": "small"})
        t.handoff(["a"], [], [], ["e"], ["n"])
        return t

    def a_noisy_task(self):
        t = Trajectory(self.data, "noisy")
        for _ in range(3):
            t.append("execute", {"command": "pytest -x", "exit_code": 1})
        return t


class TheWindowIsPartOfTheFinding(Case):
    def test_the_report_says_what_period_it_looked_at(self):
        self.a_quiet_task()
        window = friction_report(self.data)["window"]
        self.assertIn("oldest", window)
        self.assertIn("newest", window)
        self.assertIn("days_since_newest", window)

    def test_a_fresh_quiet_corpus_is_clean_and_says_when(self):
        task = self.a_quiet_task()
        out = friction_report(self.data)
        self.assertFalse(out["stale"])
        self.assertTrue(out["verdict"].startswith("clean"))
        self.assertIn(out["window"]["newest"], out["verdict"])
        self.assertEqual(out["handoff_gaps"], [])
        self.assertTrue(os.path.exists(task.path))

    def test_an_old_quiet_corpus_is_not_called_clean(self):
        """The defect. It used to answer the bare word `clean`."""
        self.a_quiet_task()
        out = friction_report(self.data, now="2027-01-01T00:00:00")
        self.assertTrue(out["stale"])
        self.assertNotEqual(out["verdict"], "clean")
        self.assertIn("not a verdict about now", out["verdict"])

    def test_the_stale_verdict_names_the_age_rather_than_implying_it(self):
        self.a_quiet_task()
        out = friction_report(self.data, now="2027-01-01T00:00:00")
        self.assertIn(str(out["window"]["days_since_newest"]), out["verdict"])

    def test_the_boundary_is_a_parameter_and_is_honoured(self):
        self.a_quiet_task()
        newest = friction_report(self.data)["window"]["newest"]
        year = int(newest[:4])
        later = f"{year + 1}{newest[4:]}"
        self.assertFalse(
            friction_report(self.data, now=later,
                            stale_after_days=100000)["stale"])
        self.assertTrue(
            friction_report(self.data, now=later, stale_after_days=1)["stale"])

    def test_real_friction_is_reported_even_when_the_corpus_is_old(self):
        """Staleness must not become a way of not saying what was found."""
        self.a_noisy_task()
        out = friction_report(self.data, now="2027-01-01T00:00:00")
        self.assertTrue(out["repeated_commands"])
        self.assertIn("friction found", out["verdict"])

    def test_the_friction_verdict_carries_the_window_too(self):
        self.a_noisy_task()
        out = friction_report(self.data)
        self.assertIn(out["window"]["newest"], out["verdict"])

    def test_an_empty_directory_still_says_there_is_nothing_yet(self):
        out = friction_report(self.data)
        self.assertEqual(out["tasks_scanned"], 0)
        self.assertEqual(out["verdict"], "no trajectories yet")
        self.assertEqual(out["window"], {})


class TheGatewayLogIsRead(Case):
    """It was written by one module and read by none."""

    OK = {"t": "2026-09-01T10:00:00", "kind": "result", "id": "kg_query",
          "ok": True, "error": None}

    def failure(self, error, *, t="2026-09-01T11:00:00", cid="bootstrap_scan"):
        return {"t": t, "kind": "result", "id": cid, "ok": False,
                "error": error}

    def test_a_missing_log_is_not_an_error_and_not_a_finding(self):
        out = friction_report(self.data)
        self.assertEqual(out["audit_entries"], 0)
        self.assertEqual(out["capability_failures"], [])

    def test_entries_and_outcomes_are_counted_separately(self):
        """An old log is all intentions. Reporting zero failures out of it
        would be reporting zero out of nothing."""
        self.audit([{"t": "2026-08-01T09:00:00", "kind": "invoke",
                     "id": "kg_query", "args": {}}, self.OK])
        out = friction_report(self.data)
        self.assertEqual(out["audit_entries"], 2)
        self.assertEqual(out["audited_outcomes"], 1)

    def test_a_failed_call_becomes_a_signal(self):
        self.audit([self.failure("missing args ['scan_root']")])
        failures = friction_report(self.data)["capability_failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["capability"], "bootstrap_scan")
        self.assertEqual(failures[0]["times"], 1)

    def test_a_successful_call_is_not_one(self):
        self.audit([self.OK])
        self.assertEqual(friction_report(self.data)["capability_failures"], [])

    def test_the_same_failure_twice_is_one_signal_counted_twice(self):
        self.audit([self.failure("timeout after 120s", t="2026-09-01T11:00:00"),
                    self.failure("timeout after 300s", t="2026-09-02T11:00:00")])
        failures = friction_report(self.data)["capability_failures"]
        self.assertEqual(len(failures), 1,
                         "the same failure with a different duration split")
        self.assertEqual(failures[0]["times"], 2)
        self.assertEqual(failures[0]["first_seen"], "2026-09-01T11:00:00")
        self.assertEqual(failures[0]["last_seen"], "2026-09-02T11:00:00")

    def test_genuinely_different_failures_stay_apart(self):
        self.audit([self.failure("missing args ['scan_root']"),
                    self.failure("schema mismatch at $.steps")])
        self.assertEqual(
            len(friction_report(self.data)["capability_failures"]), 2)

    def test_the_same_message_from_two_capabilities_stays_apart(self):
        self.audit([self.failure("boom", cid="a"),
                    self.failure("boom", cid="b")])
        self.assertEqual(
            len(friction_report(self.data)["capability_failures"]), 2)

    def test_the_most_frequent_failure_comes_first(self):
        self.audit([self.failure("rare thing", cid="a"),
                    self.failure("common thing", cid="b"),
                    self.failure("common thing", cid="b")])
        failures = friction_report(self.data)["capability_failures"]
        self.assertEqual(failures[0]["capability"], "b")
        self.assertEqual(failures[0]["times"], 2)

    def test_which_capabilities_are_ever_invoked_is_reported(self):
        """On this repository the answer was two of them, ever."""
        self.audit([{"t": "2026-09-01T09:00:00", "kind": "invoke",
                     "id": "kg_query", "args": {}},
                    {"t": "2026-09-01T09:01:00", "kind": "invoke",
                     "id": "kg_query", "args": {}},
                    {"t": "2026-09-01T09:02:00", "kind": "invoke",
                     "id": "env_probe", "args": {}}])
        used = friction_report(self.data)["capabilities_used"]
        self.assertEqual(used, {"kg_query": 2, "env_probe": 1})

    def test_the_log_window_is_reported_beside_the_trajectory_window(self):
        """They are different stores and they go stale independently -- which
        is how the seventeen-day gap here was visible at all."""
        self.audit([{"t": "2026-08-04T22:50:04", "kind": "invoke",
                     "id": "kg_query", "args": {}}, self.OK])
        out = friction_report(self.data)
        self.assertEqual(out["audit_window"],
                         {"oldest": "2026-08-04T22:50:04",
                          "newest": "2026-09-01T10:00:00"})

    def test_a_torn_last_line_is_skipped_rather_than_fatal(self):
        """An append-only log read while it is being appended to."""
        path = self.audit([self.OK])
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write('{"t": "2026-09-01T12:00:00", "kind": "res')
        self.assertEqual(friction_report(self.data)["audit_entries"], 1)

    def test_failures_with_no_trajectories_are_still_routed(self):
        self.audit([self.failure("missing args ['scan_root']")])
        verdict = friction_report(self.data)["verdict"]
        self.assertIn("capability failure", verdict)
        self.assertNotEqual(verdict, "no trajectories yet")

    def test_a_capability_failure_stops_a_quiet_corpus_being_clean(self):
        self.a_quiet_task()
        self.audit([self.failure("missing args ['scan_root']")])
        self.assertIn("friction found", friction_report(self.data)["verdict"])


class NothingElseMoved(Case):
    """The signals that were already right must still be right."""

    def test_the_original_signals_survive(self):
        t = Trajectory(self.data, "demo")
        for _ in range(3):
            t.append("execute", {"command": "pytest -x", "exit_code": 1})
        t.append("evidence", {"detail": "x" * 9000})
        t.record_failure("retrieval", "miss", "keyword gap", "e")
        out = friction_report(self.data)
        self.assertEqual(out["tasks_scanned"], 1)
        self.assertEqual(out["repeated_commands"][0]["times"], 3)
        self.assertTrue(out["consecutive_repeats"])
        self.assertTrue(out["oversized_events"])
        self.assertEqual(out["failure_hotspots"], {"retrieval": 1})
        self.assertIn(os.path.basename(t.path), out["handoff_gaps"])


if __name__ == "__main__":
    unittest.main()
