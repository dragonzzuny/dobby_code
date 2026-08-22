"""Choosing the shape before choosing the worker, and refusing to pay for either.

The pilot priced the generic graph: 3.00x the provider calls, 2.94x the cost per
verified task, 15.58x the thinking tokens, for 3/3 verified in every arm. B_gated
cost 1.9% more than a bare call, so the GATE is nearly free and the graph was the
whole bill.

What follows is a policy, and a policy is worth testing for the cases where it
must REFUSE rather than the ones where it proceeds. The tests are grouped that
way: the fast path must not appear where evidence says it should not, the
architect must not be reachable without a trigger, and agy must not be reachable
at all outside isolation — that last one is not a preference, it is the provider
this repository measured writing files under an argv documented as read-only.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import loop as L
from dobby.project.execution_policy import (MAX_SCOPED_PATHS, ExecutionClass,
                                            TaskProfile, choose_execution,
                                            explain, profile_item)
from dobby.project.fastpath import (build_order, deterministic_report,
                                    direct_gated_graph)
from dobby.project.models import UNCERTAINTY_ESCALATION, WorkItem
from dobby.providers.detect import Availability
from dobby.providers.policy import (ARCHITECT, CRITIC, IMPLEMENT,
                                    ISOLATED_DELEGATE, MIN_ECONOMIC_SAMPLES,
                                    ROLE_POLICY, admissible, candidates_for,
                                    economics)

PASSING = '{python} -c "import sys; sys.exit(0)"'


def profile(**kw) -> TaskProfile:
    """Mirrors what `profile_item` actually produces, including LOCAL_WRITE.

    The helper defaulted to the dataclass's NONE and disagreed with the function
    it stood in for, which is how a helper stops testing anything.
    """
    base = dict(acceptance_declared=True, expected_paths=("app.py",),
                uncertainty=1, side_effect_class="LOCAL_WRITE")
    base.update(kw)
    return TaskProfile(**base)


def available(*ids):
    return {i: Availability(id=i, state="available", detail="", path="x",
                            cost_tier="standard", kind="cli",
                            verified_here=True) for i in ids}


class TheFastPathIsTheDefaultAndSaysWhy(unittest.TestCase):
    def test_a_scoped_gradeable_first_attempt_gets_one_call(self):
        self.assertIs(choose_execution(profile()), ExecutionClass.DIRECT_GATED)

    def test_the_choice_carries_its_reason(self):
        chosen = choose_execution(profile())
        self.assertIn("no prior failure", explain(profile(), chosen))

    def test_the_fast_path_graph_is_exactly_one_node(self):
        """The whole measured saving. Three nodes cost 3x and verified the same."""
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix the off-by-one",
                        acceptance_checks=[PASSING])
        graph = direct_gated_graph(item, profile(), static=True)
        self.assertEqual(list(graph.nodes), ["execute"])

    def test_it_carries_the_gate_the_generic_graph_carried(self):
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix it", acceptance_checks=[PASSING])
        node = direct_gated_graph(item, profile(), static=True).nodes["execute"]
        self.assertEqual(node.contract.acceptance_checks, [PASSING])
        self.assertEqual(node.contract.expected_paths, ["app.py"])
        self.assertEqual(node.contract.side_effect_class, "LOCAL_WRITE")

    def test_an_item_with_no_plan_still_gets_the_write_grant(self):
        """The defect the 4-arm pilot caught, in twelve runs, in D_adaptive.

        `profile_item` derived the side-effect class from the compiled plan's
        write set, so an item nobody had planned profiled as read-only. The fast
        path then granted no write, and all three tasks failed 0/3 having been
        asked to edit a file they were not permitted to touch — the same defect
        the write grant was built to fix, arriving through a new door.

        `runner.default_graph` has always assumed LOCAL_WRITE for its execute
        node. A replacement for it must not silently grant less.
        """
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix the off-by-one",
                        acceptance_checks=[PASSING])
        got = profile_item(item)
        self.assertEqual(got.side_effect_class, "LOCAL_WRITE")
        self.assertEqual(got.expected_paths, (),
                         "an empty scope must mean 'check the tree', not "
                         "'this node writes nothing'")
        node = direct_gated_graph(item, got, static=True).nodes["execute"]
        self.assertEqual(node.contract.side_effect_class, "LOCAL_WRITE")

    def test_an_item_may_declare_its_own_scope_without_an_architect(self):
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix it", acceptance_checks=[PASSING],
                        expected_paths=["app.py"])
        self.assertEqual(profile_item(item).expected_paths, ("app.py",))

    def test_the_worker_is_told_its_scope_not_just_its_task(self):
        """A worker failed for touching a file nobody named was given an unseen rule."""
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix it", acceptance_checks=[PASSING])
        order = build_order(item, profile())
        self.assertIn("You may change ONLY: app.py", order)
        self.assertIn("Do not modify the checks", order)


class EscalationNeedsEvidence(unittest.TestCase):
    def test_no_acceptance_means_no_amount_of_execution_helps(self):
        self.assertIs(choose_execution(profile(acceptance_declared=False)),
                      ExecutionClass.ARCHITECT_REPLAN)

    def test_a_recorded_failure_is_what_buys_a_plan(self):
        self.assertIs(choose_execution(profile(prior_failures=1)),
                      ExecutionClass.ARCHITECT_REPLAN)

    def test_an_unfailed_item_never_reaches_the_architect(self):
        """The generic plan node ran on every item; that was the 3x."""
        for paths in ((), ("a.py",), ("a.py", "b.py", "c.py")):
            self.assertNotEqual(
                choose_execution(profile(expected_paths=paths)),
                ExecutionClass.ARCHITECT_REPLAN, paths)

    def test_wide_scope_gets_the_compiled_path_not_the_fast_one(self):
        wide = tuple(f"f{i}.py" for i in range(MAX_SCOPED_PATHS + 1))
        self.assertIs(choose_execution(profile(expected_paths=wide)),
                      ExecutionClass.COMPILED_SERIAL)

    def test_high_uncertainty_gets_the_compiled_path(self):
        self.assertIs(
            choose_execution(profile(uncertainty=UNCERTAINTY_ESCALATION)),
            ExecutionClass.COMPILED_SERIAL)

    def test_an_effect_above_local_write_is_a_human_boundary(self):
        self.assertIs(
            choose_execution(profile(side_effect_class="EXTERNAL_IRREVERSIBLE")),
            ExecutionClass.HUMAN_BOUNDARY)

    def test_a_capability_without_anywhere_safe_to_run_it_stops(self):
        """Isolation is a precondition for the delegate, not a preference."""
        self.assertIs(
            choose_execution(profile(requires_live_web=True,
                                     worktree_available=False)),
            ExecutionClass.HUMAN_BOUNDARY)

    def test_the_same_capability_with_a_worktree_delegates(self):
        self.assertIs(
            choose_execution(profile(requires_live_web=True,
                                     worktree_available=True)),
            ExecutionClass.AGY_ISOLATED_DELEGATE)


class TheProfileCostsNothing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_reads_the_item_and_calls_nothing(self):
        from dobby.providers.run import recording

        initialise(self.data, self.root, smoke=(PASSING,),
                   item_specs=[{"outcome": "make the endpoint paginate",
                                "acceptance_checks": [PASSING]}])
        store = ProjectStore(self.data)
        item = store.load_project(None)["portfolio"].get("W001")
        with recording() as calls:
            got = profile_item(item, store=store,
                               project_id=store.load_project(None)["project_id"])
        self.assertEqual(calls, [],
                         "profiling asked a provider whether the task was easy")
        self.assertTrue(got.acceptance_declared)

    def test_a_blocked_item_profiles_as_having_failed(self):
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        acceptance_checks=[PASSING], state="BLOCKED",
                        blocked_reason="the run ended FAILED")
        self.assertEqual(profile_item(item).prior_failures, 1)


class TheRoleDecidesWhoAndTheClassDecidesHowMuch(unittest.TestCase):
    def test_agy_is_not_an_implementer_on_the_original_tree(self):
        """Measured writing under an argv documented as read-only.

        This asserted agy's ABSENCE from the candidate list. It is now listed
        and GATED, which is a stronger arrangement for the same property: absent
        means it can never be a fallback even once a worktree exists, and the
        operator's intent is to use the subscription often — just never on the
        original tree. The gate is `admissible()`, so the rule lives next to the
        measurement that justifies it rather than in a tuple's ordering.
        """
        ok, why = admissible("agy", ROLE_POLICY[IMPLEMENT], isolated=False)
        self.assertFalse(ok)
        self.assertIn("measured writing files", why)

    def test_agy_may_implement_once_a_worktree_exists(self):
        ok, _ = admissible("agy", ROLE_POLICY[IMPLEMENT], isolated=True)
        self.assertTrue(ok)

    def test_agy_is_unreachable_without_isolation(self):
        ok, why = admissible("agy", ROLE_POLICY[ISOLATED_DELEGATE],
                             isolated=False)
        self.assertFalse(ok)
        self.assertIn("isolated workspace", why)

    def test_agy_is_reachable_with_isolation(self):
        """Excluding it entirely would waste the capability it actually has."""
        ok, _ = admissible("agy", ROLE_POLICY[ISOLATED_DELEGATE], isolated=True)
        self.assertTrue(ok)

    def test_codex_is_the_default_implementer(self):
        self.assertEqual(ROLE_POLICY[IMPLEMENT].candidates[0], "codex")

    def test_a_provider_with_no_verified_write_flag_cannot_implement(self):
        ok, why = admissible("gemini", ROLE_POLICY[IMPLEMENT])
        self.assertFalse(ok)

    def test_the_architect_role_still_excludes_agy(self):
        self.assertNotIn("agy", ROLE_POLICY[ARCHITECT].candidates)

    def test_candidates_are_filtered_by_what_is_installed(self):
        self.assertEqual(candidates_for(IMPLEMENT, availability=available("claude")),
                         ["claude"])
        self.assertEqual(candidates_for(ISOLATED_DELEGATE,
                                        availability=available("claude")), [])

    def test_the_critic_pool_is_not_the_implementer_alone(self):
        self.assertGreater(len(ROLE_POLICY[CRITIC].candidates), 1)


class UnmeasuredEconomicsAreNotCheapEconomics(unittest.TestCase):
    def test_no_samples_means_no_cost_number(self):
        """Reading a missing cost as a low one routes work to the least instrumented."""
        row = economics({}, "codex", "implement")
        self.assertIsNone(row["usd_per_verified"])
        self.assertEqual(row["economics_status"], "unmeasured")

    def test_too_few_samples_is_still_unmeasured(self):
        card = {"codex/implement": {"usd_per_verified": 0.01,
                                    "cost_samples": MIN_ECONOMIC_SAMPLES - 1}}
        self.assertEqual(economics(card, "codex", "implement")
                         ["economics_status"], "unmeasured")

    def test_enough_samples_becomes_measured(self):
        card = {"codex/implement": {"usd_per_verified": 0.42,
                                    "cost_samples": MIN_ECONOMIC_SAMPLES}}
        row = economics(card, "codex", "implement")
        self.assertEqual(row["economics_status"], "measured")
        self.assertEqual(row["usd_per_verified"], 0.42)


class TheReportIsAssembledNotGenerated(unittest.TestCase):
    class FakeStep:
        def __init__(self, node_id, state, artifact_id=None, failure=None):
            self.node_id, self.state = node_id, state
            self.attempts, self.worker = 1, "provider"
            self.artifact_id, self.failure, self.verdict = artifact_id, failure, {}

    class FakeResult:
        def __init__(self, steps, state="SUCCEEDED"):
            self.steps, self.state = steps, state

    def item(self, **kw):
        base = dict(work_item_id="W001", project_id="p", title="t",
                    outcome="fix it", acceptance_checks=[PASSING])
        base.update(kw)
        return WorkItem(**base)

    def test_it_names_its_source_so_nobody_reads_it_as_a_model_summary(self):
        report = deterministic_report(
            self.item(), self.FakeResult([self.FakeStep("execute", "SUCCEEDED",
                                                        "a-1")]))
        self.assertIn("no provider call was made", report["source"])

    def test_it_carries_the_promoted_artifacts_from_the_record(self):
        report = deterministic_report(
            self.item(), self.FakeResult([self.FakeStep("execute", "SUCCEEDED",
                                                        "a-1")]))
        self.assertEqual(report["promoted_artifacts"], ["a-1"])
        self.assertEqual(report["unestablished"], [])

    def test_an_ungraded_item_is_said_to_be_ungraded(self):
        report = deterministic_report(
            self.item(acceptance_checks=[]),
            self.FakeResult([self.FakeStep("execute", "SUCCEEDED", "a-1")]))
        self.assertTrue(any("no acceptance check" in u
                            for u in report["unestablished"]))

    def test_a_run_that_promoted_nothing_says_so(self):
        report = deterministic_report(
            self.item(), self.FakeResult([self.FakeStep("execute", "FAILED")],
                                         state="FAILED"))
        self.assertTrue(any("no artifact was promoted" in u
                            for u in report["unestablished"]))


class TheLoopUsesTheShapeAndReportsIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_adaptive_run_reports_the_class_it_chose(self):
        initialise(self.data, self.root, smoke=(PASSING,),
                   item_specs=[{"outcome": "make the endpoint paginate",
                                "acceptance_checks": [PASSING]}])
        result = L.advance(self.data, policy="adaptive")
        shape = result["iterations"][0]["graph"]
        self.assertTrue(shape.startswith("DIRECT_GATED"), shape)

    def test_without_the_policy_the_generic_graph_still_runs(self):
        """Opt-in: changing what executes is somebody's decision."""
        initialise(self.data, self.root, smoke=(PASSING,),
                   item_specs=[{"outcome": "make the endpoint paginate",
                                "acceptance_checks": [PASSING]}])
        result = L.advance(self.data)
        self.assertEqual(result["iterations"][0]["graph"], "generic")


if __name__ == "__main__":
    unittest.main()
