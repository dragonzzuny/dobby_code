"""The project kernel's six invariants, exercised rather than asserted.

`dobby/project/` names them PK-1..PK-6 in its docstrings. A named invariant that
has never been run is a comment, so each one gets a test that fails when the
invariant does — and, where the failure mode is subtle, a test that fails for the
right reason rather than incidentally.

    PK-1  a failing or absent baseline yields no work item at all
    PK-2  an item is DONE only when a RUN says so: SUCCEEDED, at least one
          promoted artifact, and no unconfirmed external effect
    PK-3  DONE is not selectable again
    PK-4  a session whose contract or tree no longer matches the baseline does
          not start work; it demands a re-baseline
    PK-5  recovery outranks new work
    PK-6  a portfolio write that carried a stale version is refused, not merged

The three the roadmap called the minimum for this kernel are PK-1, PK-3 and
PK-4; the other three are here because `promote_from_run`, the selector's
recovery branch and the optimistic version check are the parts that decide
whether a long-running project stays honest.
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import (Baseline, Portfolio, ProjectStore, SessionEnvelope,
                           StalePortfolio, WorkItem, attach_run, close_session,
                           initialise, open_session, promote_from_run,
                           select_next)
from dobby.project import models as M
from dobby.project.init import (build_manifest, discover_smoke_checks,
                                items_from_specs, repo_digest, take_baseline)
from dobby.runtime import (ArtifactContract, EXTERNAL_REVERSIBLE, RunStore,
                           Runner, TaskGraph, TaskNode, idempotency_key)
from dobby.runtime import graph as G

#: A command that proves nothing except that a shell ran. Enough for a baseline
#: in a directory that has no code in it.
PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_SMOKE = '{python} -c "import sys; sys.exit(3)"'


def item(work_item_id, **kw) -> WorkItem:
    kw.setdefault("project_id", "p")
    kw.setdefault("title", work_item_id)
    kw.setdefault("acceptance_checks", ["true"])
    return WorkItem(work_item_id=work_item_id, **kw)


def passing_baseline(**kw) -> Baseline:
    kw.setdefault("project_id", "p")
    kw.setdefault("git_sha", "sha")
    kw.setdefault("manifest_digest", "digest")
    kw.setdefault("passed", True)
    return Baseline(**kw)


# ---------------------------------------------------------------------------
# PK-1 — nothing starts on a tree nobody has checked
# ---------------------------------------------------------------------------

class PK1BaselineGatesEverything(unittest.TestCase):
    def test_no_baseline_yields_no_item_and_says_which_kind_of_nothing(self):
        selection = select_next(Portfolio("p", items=[item("W001")]),
                                baseline=None)
        self.assertIsNone(selection.item)
        self.assertTrue(selection.needs_rebaseline)
        self.assertIn("no baseline", selection.reason)

    def test_a_failing_baseline_names_the_check_that_failed(self):
        baseline = passing_baseline(
            passed=False,
            smoke_results=({"check": "pytest -q", "passed": False},
                           {"check": "ruff", "passed": True}))
        selection = select_next(Portfolio("p", items=[item("W001")]),
                                baseline=baseline)
        self.assertIsNone(selection.item)
        self.assertTrue(selection.needs_rebaseline)
        self.assertIn("pytest -q", selection.reason)
        self.assertNotIn("ruff", selection.reason,
                         "a passing check must not be reported as a blocker")

    def test_a_failing_baseline_outranks_even_a_recoverable_item(self):
        """PK-1 is checked before PK-5, and the order is the point.

        Reconciling an external effect against a tree that does not build is
        work whose result nobody can trust either.
        """
        stuck = item("W001", latest_run_id="run-1")
        selection = select_next(
            Portfolio("p", items=[stuck]),
            baseline=passing_baseline(passed=False),
            unconfirmed_effects={"run-1": [{"idempotency_key": "k"}]})
        self.assertIsNone(selection.item)
        self.assertTrue(selection.needs_rebaseline)


class PK1EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_with_a_failing_smoke_check_produces_no_startable_work(self):
        report = initialise(self.data, self.root, smoke=(FAILING_SMOKE,),
                            item_specs=[{"outcome": "ship it",
                                         "acceptance_checks": ["true"]}])
        self.assertFalse(report["baseline"]["passed"])

        envelope = open_session(self.data)
        self.assertTrue(envelope.needs_rebaseline, envelope.to_dict())
        self.assertIsNone(envelope.active_work_item_id,
                          "an item was handed out on a tree that fails its own "
                          "smoke check")

    def test_a_project_with_no_smoke_check_is_unestablished_not_sound(self):
        """The initialiser's refusal to invent a check has to cost something."""
        report = initialise(self.data, self.root, item_specs=[{"outcome": "x"}])
        self.assertEqual(report["smoke_checks"], [])
        self.assertFalse(report["baseline"]["passed"])
        self.assertIn("no smoke check", report["baseline"]["note"])

    def test_init_invents_neither_a_check_nor_a_work_item(self):
        self.assertEqual(discover_smoke_checks(self.root), ())
        report = initialise(self.data, self.root)
        self.assertEqual(report["work_items"], [])


# ---------------------------------------------------------------------------
# PK-2 — the run decides, not the worker
# ---------------------------------------------------------------------------

class PK2TheRunDecides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.report = initialise(
            self.data, self.root, smoke=(PASSING_SMOKE,),
            item_specs=[{"outcome": "the one item",
                         "acceptance_checks": ["true"]}])
        self.project_id = self.report["project_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _runner(self):
        return Runner(self.root, data_dir=self.data, sleep=lambda _s: None)

    def _succeeded_run(self, *, side_effect="NONE"):
        """A real run that ends SUCCEEDED with a promoted artifact."""
        runner = self._runner()
        node = TaskNode(node_id="a", kind="a", depends_on=[], worker="static",
                        contract=ArtifactContract(
                            output_schema=dict(type="object"),
                            side_effect_class=side_effect),
                        config={"payload": {"ok": True}})
        run_id = runner.start("t", TaskGraph([node]))
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        return run_id

    def test_a_run_that_did_not_succeed_blocks_the_item(self):
        store = RunStore(self.data)
        run_id = store.create_run("t", TaskGraph([
            TaskNode(node_id="a", kind="a", depends_on=[], worker="static",
                     contract=ArtifactContract(output_schema=dict(type="object")), config={"payload": {}})]))
        store.set_run_state(run_id, G.RUNNING)
        store.set_run_state(run_id, G.WAITING)      # out of budget, say

        attach_run(self.data, "W001", run_id, project_id=self.project_id)
        verdict = promote_from_run(self.data, "W001",
                                   project_id=self.project_id)
        self.assertEqual(verdict["state"], M.BLOCKED, verdict)
        self.assertTrue(any("WAITING" in r for r in verdict["reasons"]),
                        verdict["reasons"])

    def test_a_run_that_promoted_nothing_blocks_the_item(self):
        store = RunStore(self.data)
        run_id = store.create_run("t", TaskGraph([
            TaskNode(node_id="a", kind="a", depends_on=[], worker="static",
                     contract=ArtifactContract(output_schema=dict(type="object")), config={"payload": {}})]))
        store.set_run_state(run_id, G.RUNNING)
        store.set_run_state(run_id, G.SUCCEEDED)

        attach_run(self.data, "W001", run_id, project_id=self.project_id)
        verdict = promote_from_run(self.data, "W001",
                                   project_id=self.project_id)
        self.assertEqual(verdict["state"], M.BLOCKED, verdict)
        self.assertTrue(any("promoted no artifact" in r
                            for r in verdict["reasons"]), verdict["reasons"])

    def test_an_unconfirmed_external_effect_blocks_an_otherwise_good_run(self):
        run_id = self._succeeded_run()
        # The run succeeded and promoted. Then the outside world was touched and
        # never confirmed — which is the case where "the tests passed" is true
        # and "this is done" is not.
        RunStore(self.data).claim_effect(
            idempotency_key(run_id, "a"), run_id, "a", "1")

        attach_run(self.data, "W001", run_id, project_id=self.project_id)
        verdict = promote_from_run(self.data, "W001",
                                   project_id=self.project_id)
        self.assertEqual(verdict["state"], M.BLOCKED, verdict)
        self.assertTrue(any("never confirmed" in r for r in verdict["reasons"]),
                        verdict["reasons"])

    def test_all_three_conditions_met_marks_it_done_with_evidence(self):
        run_id = self._succeeded_run()
        attach_run(self.data, "W001", run_id, project_id=self.project_id)
        verdict = promote_from_run(self.data, "W001",
                                   project_id=self.project_id)
        self.assertEqual(verdict["state"], M.DONE, verdict)
        self.assertEqual(verdict["reasons"], [])
        self.assertTrue(verdict["evidence_refs"],
                        "DONE without an artifact id is a claim with no receipt")

    def test_an_item_with_no_run_cannot_be_judged_at_all(self):
        with self.assertRaises(M.ProjectError) as caught:
            promote_from_run(self.data, "W001", project_id=self.project_id)
        self.assertIn("no run", str(caught.exception))


# ---------------------------------------------------------------------------
# PK-3 — finished work is not handed out again
# ---------------------------------------------------------------------------

class PK3DoneIsNotSelectable(unittest.TestCase):
    def test_a_done_item_is_never_selected(self):
        portfolio = Portfolio("p", items=[item("W001", state=M.DONE)])
        selection = select_next(portfolio, baseline=passing_baseline())
        self.assertIsNone(selection.item)
        self.assertIn("DONE", selection.reason)

    def test_done_is_absent_from_the_selectable_set_by_construction(self):
        self.assertNotIn(M.DONE, M.SELECTABLE)
        self.assertNotIn(M.CANCELLED, M.SELECTABLE)

    def test_a_dependency_that_is_not_done_is_not_startable(self):
        portfolio = Portfolio("p", items=[
            item("W001", state=M.OPEN),
            item("W002", depends_on=["W001"], priority=9)])
        selection = select_next(portfolio, baseline=passing_baseline())
        self.assertEqual(selection.item.work_item_id, "W001",
                         "the higher-priority item ran before its dependency")

    def test_an_unknown_dependency_counts_as_unmet_not_as_absent(self):
        portfolio = Portfolio("p", items=[
            item("W002", depends_on=["typo-in-this-id"])])
        selection = select_next(portfolio, baseline=passing_baseline())
        self.assertIsNone(selection.item)
        self.assertIn("not in this portfolio", json.dumps(selection.blocked))


class PK3AcrossTwoSessions(unittest.TestCase):
    """The roadmap's second minimum test, end to end through the store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.report = initialise(
            self.data, self.root, smoke=(PASSING_SMOKE,),
            item_specs=[
                {"outcome": "first", "acceptance_checks": ["true"],
                 "priority": 9},
                {"outcome": "second", "acceptance_checks": ["true"],
                 "priority": 1}])
        self.project_id = self.report["project_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_session_b_does_not_reselect_what_session_a_verified(self):
        self.assertTrue(self.report["baseline"]["passed"],
                        self.report["baseline"])

        session_a = open_session(self.data)
        self.assertEqual(session_a.active_work_item_id, "W001",
                         "the higher-priority item should come first")

        runner = Runner(self.root, data_dir=self.data, sleep=lambda _s: None)
        run_id = runner.start("first", TaskGraph([
            TaskNode(node_id="a", kind="a", depends_on=[], worker="static",
                     contract=ArtifactContract(output_schema=dict(type="object")),
                     config={"payload": {"ok": True}})]))
        self.assertEqual(runner.run(run_id).state, G.SUCCEEDED)
        attach_run(self.data, "W001", run_id, project_id=self.project_id)

        closed = close_session(self.data, session_a.session_id)
        self.assertIsNotNone(closed.closed_at)

        store = ProjectStore(self.data)
        finished = store.load_project(self.project_id)["portfolio"].get("W001")
        self.assertEqual(finished.state, M.DONE, closed.to_dict())

        session_b = open_session(self.data)
        self.assertEqual(session_b.active_work_item_id, "W002",
                         "session B was handed the item session A finished")
        # Artifact ids, not work-item ids: the envelope carries the receipts the
        # run promoted, which is what a fresh worker can go and read.
        self.assertTrue(session_b.verified_artifact_ids,
                        "the envelope carried no evidence of finished work")
        self.assertEqual(sorted(session_b.verified_artifact_ids),
                         sorted(finished.evidence_refs))


# ---------------------------------------------------------------------------
# PK-4 — a stale contract or tree stops the shift
# ---------------------------------------------------------------------------

class PK4Staleness(unittest.TestCase):
    def test_a_changed_manifest_digest_invalidates_the_baseline(self):
        baseline = passing_baseline()
        self.assertFalse(baseline.matches("sha", "a-different-digest"))
        self.assertIn("contract",
                      baseline.staleness("sha", "a-different-digest"))

    def test_a_changed_git_sha_invalidates_the_baseline(self):
        baseline = passing_baseline()
        self.assertFalse(baseline.matches("another-sha", "digest"))
        self.assertIn("the tree is at",
                      baseline.staleness("another-sha", "digest"))

    def test_a_dirty_tree_invalidates_the_baseline(self):
        baseline = passing_baseline(repo_digest="tree-1")
        self.assertFalse(baseline.matches("sha", "digest", "tree-2"))
        self.assertIn("uncommitted",
                      baseline.staleness("sha", "digest", "tree-2"))

    def test_a_baseline_with_no_repo_digest_answers_what_it_can(self):
        """Backward compatibility, and the reason the next test matters."""
        baseline = passing_baseline(repo_digest="")
        self.assertTrue(baseline.matches("sha", "digest", "any-tree"))


class PK4EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def _init(self):
        return initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                          item_specs=[{"outcome": "x",
                                       "acceptance_checks": ["true"]}])

    def test_a_project_without_a_baseline_refuses_to_hand_out_work(self):
        initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                   item_specs=[{"outcome": "x"}], run_baseline=False)
        envelope = open_session(self.data)
        self.assertTrue(envelope.needs_rebaseline)
        self.assertIsNone(envelope.active_work_item_id)
        self.assertIn("rebaseline", envelope.next_action)

    def test_the_baseline_records_the_tree_it_was_measured_against(self):
        """Without this, the dirty-tree check below has nothing to compare."""
        report = self._init()
        self.assertTrue(report["baseline"]["passed"])
        self.assertTrue(
            report["baseline"]["repo_digest"],
            "the baseline recorded no repo_digest, so `Baseline.matches` "
            "skips the working-tree comparison entirely and a session resumes "
            "against code the baseline never saw")

    def test_an_edit_after_the_baseline_demands_a_rebaseline(self):
        """The roadmap's third minimum test.

        `git_sha` does not move when a file is edited and not committed, and
        outside git it does not exist at all — so the working-tree digest is the
        only thing standing between a session and a tree nobody checked.
        """
        self._init()
        first = open_session(self.data)
        self.assertFalse(first.needs_rebaseline, first.to_dict())
        self.assertEqual(first.active_work_item_id, "W001")

        before = repo_digest(self.root)
        with open(os.path.join(self.root, "new_module.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("# written after the baseline was taken\n")
        self.assertNotEqual(repo_digest(self.root), before,
                            "the digest did not notice a new file")

        after = open_session(self.data)
        self.assertTrue(
            after.needs_rebaseline,
            "a session started work on a tree that changed after the baseline")
        self.assertIsNone(after.active_work_item_id)

    def test_rebaselining_clears_the_refusal(self):
        self._init()
        with open(os.path.join(self.root, "new_module.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("# after\n")
        blocked = open_session(self.data)
        self.assertTrue(blocked.needs_rebaseline)

        resumed = open_session(self.data, rebaseline=True)
        self.assertFalse(resumed.needs_rebaseline, resumed.to_dict())
        self.assertEqual(resumed.active_work_item_id, "W001")


# ---------------------------------------------------------------------------
# PK-5 — recovery outranks new work
# ---------------------------------------------------------------------------

class PK5RecoveryFirst(unittest.TestCase):
    def test_an_unconfirmed_effect_beats_a_higher_priority_fresh_item(self):
        portfolio = Portfolio("p", items=[
            item("W001", priority=1, latest_run_id="run-1"),
            item("W002", priority=99)])
        selection = select_next(
            portfolio, baseline=passing_baseline(),
            unconfirmed_effects={"run-1": [{"idempotency_key": "k"}]})
        self.assertEqual(selection.item.work_item_id, "W001")
        self.assertTrue(selection.recovery)
        self.assertIn("never confirmed", selection.reason)

    def test_an_in_progress_item_is_resumed_before_anything_new_starts(self):
        portfolio = Portfolio("p", items=[
            item("W001", priority=1, state=M.IN_PROGRESS),
            item("W002", priority=99)])
        selection = select_next(portfolio, baseline=passing_baseline())
        self.assertEqual(selection.item.work_item_id, "W001")
        self.assertTrue(selection.recovery)
        self.assertIn("IN_PROGRESS", selection.reason)

    def test_the_previous_sessions_active_item_wins_among_in_flight_ones(self):
        portfolio = Portfolio("p", items=[
            item("W001", priority=99, state=M.IN_PROGRESS),
            item("W002", priority=1, state=M.IN_PROGRESS)])
        selection = select_next(portfolio, baseline=passing_baseline(),
                                active_work_item_id="W002")
        self.assertEqual(selection.item.work_item_id, "W002")

    def test_an_item_merely_named_as_next_is_not_treated_as_a_resume(self):
        """Resume means IN_PROGRESS, and the distinction is load-bearing.

        The resume branch runs BEFORE the candidate filter, so anything it
        accepts skips the dependency check. An item the last envelope only named
        as the next one to start has not started, and must go the ordinary way.
        """
        portfolio = Portfolio("p", items=[
            item("W001", state=M.OPEN),
            item("W002", state=M.OPEN, depends_on=["W001"], priority=9)])
        selection = select_next(portfolio, baseline=passing_baseline(),
                                active_work_item_id="W002")
        self.assertEqual(selection.item.work_item_id, "W001",
                         "an unstarted item was resumed past its own unmet "
                         "dependency")
        self.assertFalse(selection.recovery,
                         "ordinary selection was labelled as recovery")

    def test_ranking_is_a_total_order_so_two_sessions_agree(self):
        twins = [item("W002", priority=5, impact=5, uncertainty=1),
                 item("W001", priority=5, impact=5, uncertainty=1)]
        first = select_next(Portfolio("p", items=list(twins)),
                            baseline=passing_baseline())
        second = select_next(Portfolio("p", items=list(reversed(twins))),
                             baseline=passing_baseline())
        self.assertEqual(first.item.work_item_id, second.item.work_item_id)
        self.assertEqual(first.item.work_item_id, "W001")

    def test_an_ungradeable_item_is_flagged_rather_than_implemented(self):
        portfolio = Portfolio("p", items=[
            WorkItem(work_item_id="W001", project_id="p", title="vague",
                     acceptance_checks=[])])
        selection = select_next(portfolio, baseline=passing_baseline())
        self.assertEqual(selection.item.work_item_id, "W001")
        self.assertTrue(selection.needs_architect,
                        "an item with no machine-checkable acceptance was sent "
                        "straight to an implementation worker")

    def test_high_uncertainty_also_demands_a_decision_first(self):
        self.assertTrue(item("W001", uncertainty=M.UNCERTAINTY_ESCALATION)
                        .needs_architect)
        self.assertFalse(item("W001", uncertainty=0).needs_architect)


# ---------------------------------------------------------------------------
# PK-6 — a stale write is refused, never merged
# ---------------------------------------------------------------------------

class PK6OptimisticConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.report = initialise(
            self.data, self.root, smoke=(PASSING_SMOKE,),
            item_specs=[{"outcome": "x", "acceptance_checks": ["true"]}])
        self.project_id = self.report["project_id"]
        self.store = ProjectStore(self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_second_writer_at_the_same_version_is_refused(self):
        project = self.store.load_project(self.project_id)
        version = project["portfolio"].version
        mine = project["portfolio"].get("W001")
        theirs = self.store.load_project(self.project_id)["portfolio"] \
            .get("W001")

        mine.state = M.IN_PROGRESS
        self.store.update_item(mine, expected_version=version, reason="mine")

        theirs.state = M.CANCELLED
        with self.assertRaises(StalePortfolio) as caught:
            self.store.update_item(theirs, expected_version=version,
                                   reason="theirs")
        self.assertIn(str(version), str(caught.exception))

        after = self.store.load_project(self.project_id)["portfolio"].get("W001")
        self.assertEqual(after.state, M.IN_PROGRESS,
                         "the losing write was merged instead of refused")

    def test_every_change_leaves_an_event_that_says_why(self):
        project = self.store.load_project(self.project_id)
        target = project["portfolio"].get("W001")
        target.state = M.BLOCKED
        target.blocked_reason = "waiting on a decision"
        self.store.update_item(target,
                               expected_version=project["portfolio"].version,
                               reason="blocked by the test")

        kinds = [e["kind"] for e in self.store.events(self.project_id)]
        self.assertIn("project_created", kinds)
        self.assertIn("item_updated", kinds)
        updates = [e for e in self.store.events(self.project_id)
                   if e["kind"] == "item_updated"]
        self.assertEqual(updates[-1]["payload"]["blocked_reason"],
                         "waiting on a decision")


# ---------------------------------------------------------------------------
# The manifest is a contract, and digests decide what invalidates what
# ---------------------------------------------------------------------------

class ManifestDigestScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reinitialising_an_unchanged_project_gives_the_same_digest(self):
        one = build_manifest(self.root, smoke=(PASSING_SMOKE,),
                             project_id="fixed")
        two = build_manifest(self.root, smoke=(PASSING_SMOKE,),
                             project_id="fixed")
        self.assertEqual(one.manifest_digest, two.manifest_digest,
                         "a clock-dependent digest would invalidate every "
                         "envelope on re-init")

    def test_changing_how_the_project_is_checked_changes_the_digest(self):
        one = build_manifest(self.root, smoke=(PASSING_SMOKE,),
                             project_id="fixed")
        two = build_manifest(self.root, smoke=("pytest -q",),
                             project_id="fixed")
        self.assertNotEqual(one.manifest_digest, two.manifest_digest)

    def test_editing_a_file_does_not_change_the_contract_digest(self):
        one = build_manifest(self.root, smoke=(PASSING_SMOKE,),
                             project_id="fixed")
        with open(os.path.join(self.root, "f.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("x = 1\n")
        two = build_manifest(self.root, smoke=(PASSING_SMOKE,),
                             project_id="fixed")
        self.assertEqual(one.manifest_digest, two.manifest_digest,
                         "the tree moving must not read as the contract moving")
        self.assertNotEqual(one.repo_digest, two.repo_digest)

    def test_a_manifest_cannot_be_edited_in_place(self):
        manifest = build_manifest(self.root, project_id="fixed")
        with self.assertRaises(Exception):
            manifest.smoke_checks = ("something weaker",)

    def test_items_get_ids_in_order_and_keep_their_acceptance(self):
        items = items_from_specs("p", [{"outcome": "a",
                                        "acceptance_checks": ["x"]},
                                       {"outcome": "b"}])
        self.assertEqual([i.work_item_id for i in items], ["W001", "W002"])
        self.assertEqual(items[0].acceptance_checks, ["x"])
        self.assertTrue(items[1].needs_architect,
                        "an item with no acceptance check is not implementable")


if __name__ == "__main__":
    unittest.main()
