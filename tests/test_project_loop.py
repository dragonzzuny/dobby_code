"""The loop, judged by where it stops.

A loop that keeps acting is easy. The property worth testing is the opposite
one: that it halts at each boundary only a person can cross, that it says which
boundary in a token a caller can branch on, and that it never marks an item DONE
that the run did not earn.

So almost every test here asserts a `stopped` reason, and the two that assert
progress also assert the evidence behind it.
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, advance, initialise, open_session
from dobby.project import loop as L
from dobby.project import models as M
from dobby.runtime import RunStore, idempotency_key

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_SMOKE = '{python} -c "import sys; sys.exit(3)"'
#: A check the verify gate runs against the tree. Cheap and honest either way.
PASSING_CHECK = '{python} -c "import sys; sys.exit(0)"'
FAILING_CHECK = '{python} -c "import sys; sys.exit(1)"'


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, items, *, smoke=PASSING_SMOKE, baseline=True):
        report = initialise(self.data, self.root, smoke=(smoke,),
                            item_specs=items, run_baseline=baseline)
        self.project_id = report["project_id"]
        return report

    def items(self):
        store = ProjectStore(self.data)
        return store.load_project(self.project_id)["portfolio"]


class ItHaltsAtEveryBoundary(LoopCase):
    def test_an_empty_portfolio_is_complete_not_merely_finished(self):
        self.init([])
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.PORTFOLIO_COMPLETE, result)
        self.assertEqual(result["iterations"], [])

    def test_a_failing_baseline_stops_before_anything_runs(self):
        self.init([{"outcome": "x", "acceptance_checks": [PASSING_CHECK]}],
                  smoke=FAILING_SMOKE)
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.BASELINE_FAILED, result)
        self.assertEqual(result["iterations"], [],
                         "a run was started on a tree that fails its own checks")
        self.assertFalse(result["baseline_passed"])

    def test_an_item_with_no_machine_checkable_acceptance_wants_an_architect(self):
        self.init([{"outcome": "make it better", "acceptance_checks": []}])
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.NEEDS_ARCHITECT, result)
        self.assertEqual(result["iterations"], [])
        self.assertIn("nothing could grade the result", result["detail"])

    def test_high_uncertainty_also_wants_an_architect(self):
        self.init([{"outcome": "rewrite the scheduler",
                    "acceptance_checks": [PASSING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.NEEDS_ARCHITECT, result)

    def test_an_unconfirmed_external_effect_stops_the_loop(self):
        """The outside world may have moved and nothing in here knows how."""
        self.init([{"outcome": "first", "acceptance_checks": [PASSING_CHECK]},
                   {"outcome": "second", "acceptance_checks": [PASSING_CHECK]}])
        first = advance(self.data)
        self.assertEqual(first["items_completed"], ["W001"], first)

        run_id = first["iterations"][0]["run_id"]
        RunStore(self.data).claim_effect(
            idempotency_key(run_id, "execute"), run_id, "execute", "1")

        second = advance(self.data)
        self.assertEqual(second["stopped"], L.NEEDS_RECONCILIATION, second)
        self.assertEqual(second["iterations"], [],
                         "new work was started while an effect was unreconciled")

    def test_everything_blocked_is_not_reported_as_complete(self):
        self.init([{"outcome": "x", "acceptance_checks": [PASSING_CHECK],
                    "depends_on": ["W999"]}])
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.NOTHING_STARTABLE, result)
        self.assertNotEqual(result["stopped"], L.PORTFOLIO_COMPLETE)

    def test_the_ceiling_is_reported_as_a_ceiling(self):
        self.init([{"outcome": f"item {n}", "acceptance_checks": [PASSING_CHECK],
                    "priority": 10 - n} for n in range(3)])
        result = advance(self.data, max_items=2)
        self.assertEqual(result["stopped"], L.MAX_ITEMS, result)
        self.assertEqual(len(result["items_completed"]), 2)

    def test_every_stop_reason_is_a_declared_one(self):
        """A caller branches on this token, so it may not be improvised."""
        self.init([])
        self.assertIn(advance(self.data)["stopped"], L.STOP_REASONS)


class ItStopsOnAFailureRatherThanStepOver(LoopCase):
    def test_an_item_whose_checks_fail_blocks_and_halts_the_loop(self):
        self.init([{"outcome": "will not verify",
                    "acceptance_checks": [FAILING_CHECK], "priority": 9},
                   {"outcome": "would have been next",
                    "acceptance_checks": [PASSING_CHECK], "priority": 1}])
        result = advance(self.data, max_items=0)

        self.assertEqual(result["stopped"], L.ITEM_BLOCKED, result)
        self.assertEqual(result["items_completed"], [],
                         "an item whose acceptance check failed was marked done")
        self.assertEqual(len(result["iterations"]), 1,
                         "the loop stepped over a blocked item and kept going")

        portfolio = self.items()
        self.assertEqual(portfolio.get("W001").state, M.BLOCKED)
        self.assertEqual(portfolio.get("W002").state, M.OPEN,
                         "the second item was touched despite the halt")

    def test_running_again_steps_over_the_blocked_item_deliberately(self):
        self.init([{"outcome": "will not verify",
                    "acceptance_checks": [FAILING_CHECK], "priority": 9},
                   {"outcome": "the next one",
                    "acceptance_checks": [PASSING_CHECK], "priority": 1}])
        self.assertEqual(advance(self.data)["stopped"], L.ITEM_BLOCKED)

        second = advance(self.data)
        self.assertEqual(second["items_completed"], ["W002"], second)
        self.assertEqual(self.items().get("W001").state, M.BLOCKED,
                         "the blocked item was quietly retried")


class ItMakesProgressAndCanProveIt(LoopCase):
    def test_one_item_completes_with_a_run_and_an_artifact_behind_it(self):
        self.init([{"outcome": "the only item",
                    "acceptance_checks": [PASSING_CHECK]}])
        result = advance(self.data)

        self.assertEqual(result["items_completed"], ["W001"], result)
        step = result["iterations"][0]
        self.assertEqual(step["run_state"], "SUCCEEDED")
        self.assertEqual(step["item_state"], M.DONE)
        self.assertTrue(step["evidence_refs"],
                        "DONE with no artifact id is a claim with no receipt")

        # The receipts are real: every id names a PROMOTED artifact of that run.
        promoted = {a["artifact_id"] for a
                    in RunStore(self.data).artifacts(step["run_id"],
                                                     state="PROMOTED")}
        self.assertTrue(set(step["evidence_refs"]) <= promoted, step)

    def test_draining_closes_the_portfolio_and_says_so(self):
        self.init([{"outcome": f"item {n}", "acceptance_checks": [PASSING_CHECK],
                    "priority": 10 - n} for n in range(3)])
        result = advance(self.data, max_items=0)

        self.assertEqual(result["stopped"], L.PORTFOLIO_COMPLETE, result)
        self.assertEqual(result["items_completed"], ["W001", "W002", "W003"])
        self.assertEqual(result["coverage"]["remaining"], 0)
        self.assertEqual(result["coverage"]["fraction_done"], 1.0)

    def test_dependencies_are_respected_across_iterations(self):
        self.init([{"outcome": "second", "acceptance_checks": [PASSING_CHECK],
                    "priority": 99, "depends_on": ["W002"]},
                   {"outcome": "first", "acceptance_checks": [PASSING_CHECK],
                    "priority": 1}])
        result = advance(self.data, max_items=0)
        self.assertEqual(result["items_completed"], ["W002", "W001"],
                         "the dependent ran before its dependency")

    def test_each_item_is_judged_against_a_freshly_taken_baseline(self):
        """The loop re-baselines because the previous item changed the tree."""
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK],
                    "priority": 9},
                   {"outcome": "two", "acceptance_checks": [PASSING_CHECK],
                    "priority": 1}])
        first_baseline = ProjectStore(self.data) \
            .load_project(self.project_id)["baseline"]

        # A change to the tree between items, of the kind a real work item makes.
        advance(self.data)
        with open(os.path.join(self.root, "written_by_the_item.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("# the item changed the tree\n")

        second = advance(self.data)
        self.assertEqual(second["items_completed"], ["W002"], second)

        latest = ProjectStore(self.data) \
            .load_project(self.project_id)["baseline"]
        self.assertNotEqual(latest.repo_digest, first_baseline.repo_digest,
                            "the loop worked from a baseline taken against a "
                            "tree that no longer existed")
        self.assertTrue(latest.passed)

    def test_a_change_that_breaks_the_tree_stops_the_next_item(self):
        """The re-baseline is a check, not a rubber stamp."""
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK],
                    "priority": 9},
                   {"outcome": "two", "acceptance_checks": [PASSING_CHECK],
                    "priority": 1}],
                  smoke='{python} -c "import sys, os; '
                        'sys.exit(1 if os.path.exists(\'broke_it.txt\') else 0)"')
        self.assertEqual(advance(self.data)["items_completed"], ["W001"])

        with open(os.path.join(self.root, "broke_it.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write("x")

        result = advance(self.data)
        self.assertEqual(result["stopped"], L.BASELINE_FAILED, result)
        self.assertEqual(result["iterations"], [])
        self.assertEqual(self.items().get("W002").state, M.OPEN)


class TheSessionRecordSurvivesTheLoop(LoopCase):
    def test_every_iteration_leaves_a_closed_envelope_and_an_event(self):
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK]}])
        result = advance(self.data)
        store = ProjectStore(self.data)

        session_id = result["iterations"][0]["session_id"]
        envelope = store.get_envelope(session_id)
        self.assertIsNotNone(envelope.closed_at,
                             "the loop left a shift open behind it")

        kinds = [e["kind"] for e in store.events(self.project_id)]
        for expected in ("project_created", "baseline_recorded",
                         "session_opened", "item_updated", "session_closed"):
            self.assertIn(expected, kinds)

    def test_the_item_points_at_its_run_before_the_run_starts(self):
        """Recorded first, so a crash leaves a traceable item, not an orphan."""
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK]}])
        result = advance(self.data)
        item = self.items().get("W001")
        self.assertEqual(item.latest_run_id, result["iterations"][0]["run_id"])

    def test_a_static_run_is_labelled_as_one(self):
        """A drained portfolio of static runs looks exactly like a finished
        project, so the distinction may not be left for a reader to infer."""
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK]}])
        self.assertTrue(advance(self.data)["static"])
        self.assertFalse(
            advance(self.data, execute_command='{python} -c "pass"')["static"])


class TheCommandLine(LoopCase):
    """The loop through `dobby project run`, since that is how it is used."""

    def _cli(self, *args):
        import subprocess
        from dobby.core.platform import child_env
        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "project", *args,
             "--repo", self.tmp.name],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=child_env(), timeout=600)
        return proc.returncode, proc.stdout, proc.stderr

    def setUp(self):
        super().setUp()
        # `--repo` puts the store at <tmp>/.dobby, which is where `init` wrote it.
        self.init([{"outcome": "one", "acceptance_checks": [PASSING_CHECK],
                    "priority": 9},
                   {"outcome": "two", "acceptance_checks": [PASSING_CHECK],
                    "priority": 1}])

    def test_run_advances_one_item_by_default(self):
        code, out, err = self._cli("run")
        self.assertEqual(code, 0, err[-600:])
        payload = json.loads(out)
        self.assertEqual(payload["items_completed"], ["W001"], payload)
        self.assertEqual(payload["stopped"], L.MAX_ITEMS)

    def test_until_empty_drains_the_portfolio(self):
        code, out, err = self._cli("run", "--until", "empty")
        self.assertEqual(code, 0, err[-600:])
        payload = json.loads(out)
        self.assertEqual(payload["items_completed"], ["W001", "W002"], payload)
        self.assertEqual(payload["stopped"], L.PORTFOLIO_COMPLETE)
        self.assertEqual(payload["coverage"]["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
