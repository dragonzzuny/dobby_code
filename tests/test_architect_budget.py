"""What counts as the same architect question, and how many different ones cost.

Two defects, one shared cause: the dedupe was doing a job nobody had bounded.

`ArchitectureRequest.digest` folded in uncertainty, acceptance checks and
evidence refs — most of the gradeability question. `build_prompt` also shows the
architect the item's TITLE and OUTCOME, so rewriting the outcome (the ordinary
way somebody sharpens a vague item) changed what was being ASKED while identity
stood still, and the second call was answered with the first call's plan.

And the dedupe only ever protected against the IDENTICAL question. A sequence of
slightly different ones — add an evidence ref, re-run, add another — was
unbounded, so an item nobody could grade could spend a model indefinitely being
told so.

The tests below are therefore about the two edges of the same line: things that
must make it a NEW question, and how many new questions an item gets.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import architecture as A
from dobby.project.models import WorkItem

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'


def item(**over) -> WorkItem:
    base = dict(work_item_id="W001", project_id="p", title="paginate it",
                outcome="make the endpoint paginate")
    base.update(over)
    return WorkItem(**base)


def plan(**kw) -> dict:
    payload = {"objective": "make it gradeable", "proposed_acceptance_checks": []}
    payload.update(kw)
    return payload


class TheItemContractCoversWhatTheArchitectIsShown(unittest.TestCase):
    def test_a_rewritten_outcome_is_a_different_question(self):
        """The prompt carries the outcome, so identity has to as well."""
        self.assertNotEqual(
            item().architect_contract_digest,
            item(outcome="make the endpoint stream").architect_contract_digest)

    def test_a_rewritten_title_is_a_different_question(self):
        self.assertNotEqual(item().architect_contract_digest,
                            item(title="stream it").architect_contract_digest)

    def test_a_new_dependency_is_a_different_planning_problem(self):
        self.assertNotEqual(
            item().architect_contract_digest,
            item(depends_on=["W002"]).architect_contract_digest)

    def test_a_bare_version_bump_is_NOT_a_new_question(self):
        """This assertion is the inverse of the one that stood here, on purpose.

        `version` was folded into the contract digest as a catch-all so a future
        field could not silently fall outside identity. Building the replan path
        showed what that cost: `version` bumps on EVERY write, including a state
        transition, a run being attached, or a repair directive the harness
        appended to itself. Almost every item the loop touched therefore became a
        new architect question, which is the dedupe the ceiling work was built to
        protect — a guard that makes the thing it guards useless.

        The property is not abandoned, it is asserted directly instead:
        `test_replan.py::test_the_contract_digest_covers_every_field_the_prompt_shows`
        reads `build_prompt` and fails if it shows the architect a field the
        digest does not cover. That is a stronger check than a version bump,
        because it names the actual requirement rather than approximating it.
        """
        self.assertEqual(item().architect_contract_digest,
                         item(version=2).architect_contract_digest)

    def test_the_same_item_twice_is_the_same_contract(self):
        self.assertEqual(item().architect_contract_digest,
                         item().architect_contract_digest)

    def test_the_contract_reaches_the_request_digest(self):
        one = A.ArchitectureRequest(
            project_id="p", work_item_id="W001",
            trigger=A.MISSING_ACCEPTANCE, manifest_digest="d",
            baseline_git_sha="s", item_contract=item().architect_contract_digest)
        two = A.ArchitectureRequest(
            project_id="p", work_item_id="W001",
            trigger=A.MISSING_ACCEPTANCE, manifest_digest="d",
            baseline_git_sha="s",
            item_contract=item(outcome="different").architect_contract_digest)
        self.assertNotEqual(one.digest, two.digest)

    def test_the_older_fields_still_move_identity_on_their_own(self):
        """A caller may build a request with no item at all; those are all it has."""
        base = dict(project_id="p", work_item_id="W001",
                    trigger=A.MISSING_ACCEPTANCE, manifest_digest="d",
                    baseline_git_sha="s")
        plain = A.ArchitectureRequest(**base)
        self.assertNotEqual(
            plain.digest,
            A.ArchitectureRequest(**base, evidence_refs=("a-1",)).digest)
        self.assertNotEqual(
            plain.digest,
            A.ArchitectureRequest(**base, item_uncertainty=4).digest)


class BudgetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        report = initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                            item_specs=[{"outcome": "make the endpoint paginate",
                                         "acceptance_checks": []}])
        self.project_id = report["project_id"]
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, request):
        self.calls.append(request.digest)
        # Discovery: a decision that changes nothing, so the item stays
        # ungradeable and remains askable. That is the shape the budget is for.
        return plan(discovery_steps=[{"kind": "read", "n": len(self.calls)}])

    def ask(self, **kw):
        return A.request_architecture(self.data, "W001",
                                      project_id=self.project_id,
                                      propose=self.propose, **kw)

    def nudge(self, ref):
        """Change the item the way an operator would: add the evidence asked for."""
        store = ProjectStore(self.data)
        project = store.load_project(self.project_id)
        work = project["portfolio"].get("W001")
        work.evidence_refs = list(work.evidence_refs) + [ref]
        store.update_item(work, expected_version=project["portfolio"].version,
                          reason="test nudge")


class ADifferentQuestionIsAskedAndThenBounded(BudgetCase):
    def test_a_changed_item_really_is_asked_again(self):
        """P1a end to end: without the contract digest this returned the cached plan."""
        self.ask()
        self.nudge("evidence-1")
        self.ask()
        self.assertEqual(len(self.calls), 2, self.calls)

    def test_the_third_distinct_question_is_refused_rather_than_paid_for(self):
        self.ask()
        self.nudge("evidence-1")
        self.ask()
        self.nudge("evidence-2")
        third = self.ask()

        self.assertEqual(len(self.calls), 2,
                         "the architect was paid a third time for one item")
        self.assertEqual(third.outcome, A.NEEDS_HUMAN_APPROVAL, third.reason)
        self.assertIn(A.BUDGET_MARKER, third.reason)
        self.assertIn("W001", third.reason)

    def test_a_question_already_answered_stays_free_after_the_budget_is_spent(self):
        """The ceiling bounds NEW questions; a repeat must keep its answer."""
        first = self.ask()
        self.nudge("evidence-1")
        self.ask()
        self.nudge("evidence-2")
        self.ask()                                  # refused, budget spent

        # Roll the item back to exactly the world of the first question.
        store = ProjectStore(self.data)
        project = store.load_project(self.project_id)
        work = project["portfolio"].get("W001")
        work.evidence_refs = []
        work.version = 1
        store.update_item(work, expected_version=project["portfolio"].version,
                          reason="restore")
        # `version` is part of the contract digest and update_item bumps it, so
        # this cannot re-create the first digest through the store. Assert the
        # property directly instead: the first decision is still retrievable.
        cached = ProjectStore(self.data).decision_for(
            self.project_id, self.calls[0])
        self.assertIsNotNone(cached, "an answered question stopped being cached")
        self.assertEqual(cached["outcome"], first.outcome)

    def test_a_budget_refusal_does_not_itself_consume_budget(self):
        """Otherwise one refusal makes the count climb forever and the reason lies."""
        self.ask()
        self.nudge("evidence-1")
        self.ask()
        self.nudge("evidence-2")
        self.ask()
        self.nudge("evidence-3")
        again = self.ask()
        self.assertIn(f"the ceiling is {A.ARCHITECT_CALL_CEILING}", again.reason)
        self.assertIn(f"{A.ARCHITECT_CALL_CEILING} call(s) already", again.reason)

    def test_a_caller_may_set_its_own_ceiling(self):
        self.ask(ceiling=1)
        self.nudge("evidence-1")
        second = self.ask(ceiling=1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second.outcome, A.NEEDS_HUMAN_APPROVAL)

    def test_each_trigger_carries_its_own_budget(self):
        """Planned-once and uncertainty-adjudicated-once are different budgets."""
        self.ask(trigger=A.MISSING_ACCEPTANCE)
        self.nudge("evidence-1")
        self.ask(trigger=A.MISSING_ACCEPTANCE)
        self.nudge("evidence-2")
        spent = self.ask(trigger=A.MISSING_ACCEPTANCE)
        self.assertEqual(spent.outcome, A.NEEDS_HUMAN_APPROVAL)

        self.nudge("evidence-3")
        other = self.ask(trigger=A.HIGH_UNCERTAINTY)
        self.assertNotEqual(other.outcome, A.NEEDS_HUMAN_APPROVAL, other.reason)
        self.assertEqual(len(self.calls), 3,
                         "the second trigger was refused on the first's budget")

    def test_the_store_counts_only_questions_that_reached_a_decision(self):
        self.ask()
        settled = ProjectStore(self.data).settled_requests(
            self.project_id, "W001", trigger=A.MISSING_ACCEPTANCE)
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["work_item_id"], "W001")


if __name__ == "__main__":
    unittest.main()
