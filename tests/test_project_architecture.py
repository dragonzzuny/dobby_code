"""The architect boundary, judged by what it refuses.

`request_architecture` is the only path on which a model may change the
definition of done, so almost every test here is a proposal that must NOT be
applied. The two that do apply assert what was applied and where it came from.

The `propose` seam takes the request and returns the raw plan payload, so every
rule below is exercised without a provider, a network, or a model — which is
also why these run in seconds rather than minutes.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, advance, initialise
from dobby.project import architecture as A
from dobby.project import loop as L
from dobby.project import models as M

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_CHECK = '{python} -c "import sys; sys.exit(1)"'


def plan(**kw) -> dict:
    """A payload shaped like a plan, with only what a test cares about set."""
    payload = {"objective": "make it gradeable",
               "proposed_acceptance_checks": []}
    payload.update(kw)
    return payload


class ArchitectureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.smoke = PASSING_SMOKE

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, items, *, smoke=PASSING_SMOKE):
        report = initialise(self.data, self.root, smoke=(smoke,),
                            item_specs=items)
        self.project_id = report["project_id"]
        self.smoke = smoke
        return report

    def store(self):
        return ProjectStore(self.data)

    def item(self, work_item_id="W001"):
        return self.store().load_project(self.project_id)["portfolio"].get(
            work_item_id)

    def portfolio(self):
        return self.store().load_project(self.project_id)["portfolio"]

    def ask(self, payload, *, work_item_id="W001"):
        return A.request_architecture(
            self.data, work_item_id, project_id=self.project_id,
            propose=lambda _request: payload)


# ---------------------------------------------------------------------------
# What may be applied
# ---------------------------------------------------------------------------

class APlanMayOnlyUseWhatTheProjectDeclares(ArchitectureCase):
    def test_an_item_with_no_acceptance_gets_the_projects_own_check(self):
        self.init([{"outcome": "ship the thing", "acceptance_checks": []}])
        self.assertTrue(self.item().needs_architect)

        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke]))

        self.assertEqual(decision.outcome, A.APPLIED, decision.reason)
        self.assertEqual(decision.applied_checks, (self.smoke,))
        after = self.item()
        self.assertEqual(after.acceptance_checks, [self.smoke])
        self.assertFalse(after.needs_architect,
                         "the item is gradeable now and must not be re-asked")
        self.assertEqual(after.planned_by, decision.plan_id)

    def test_the_portfolio_version_moves_only_when_a_plan_is_applied(self):
        self.init([{"outcome": "one", "acceptance_checks": []},
                   {"outcome": "two", "acceptance_checks": []}])
        before = self.portfolio().version

        refused = self.ask(plan(), work_item_id="W001")
        self.assertEqual(refused.outcome, A.REJECTED, refused.reason)
        self.assertIsNone(refused.portfolio_version)
        self.assertEqual(self.portfolio().version, before,
                         "a refused plan moved the portfolio")

        applied = self.ask(plan(proposed_acceptance_checks=[self.smoke]),
                           work_item_id="W002")
        self.assertEqual(applied.outcome, A.APPLIED, applied.reason)
        self.assertGreater(self.portfolio().version, before)
        self.assertEqual(applied.portfolio_version, self.portfolio().version)

    def test_a_check_the_manifest_never_declared_needs_a_person(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan(
            proposed_acceptance_checks=["pytest -q tests/test_invented.py"]))

        self.assertEqual(decision.outcome, A.NEEDS_HUMAN_APPROVAL,
                         decision.reason)
        self.assertIn("never declared", decision.reason)
        self.assertEqual(self.item().acceptance_checks, [],
                         "an unapproved command was applied anyway")

    def test_a_new_destructive_command_is_named_as_destructive(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan(
            proposed_acceptance_checks=["rm -rf /"]))

        self.assertEqual(decision.outcome, A.NEEDS_HUMAN_APPROVAL,
                         decision.reason)
        self.assertIn("destructive", decision.reason)
        self.assertIn("rm -rf /", decision.reason)
        self.assertEqual(self.item().acceptance_checks, [])


class APlanMayNeverWeakenTheDefinitionOfDone(ArchitectureCase):
    def test_dropping_an_existing_check_is_a_hard_reject(self):
        self.init([{"outcome": "make the endpoint paginate",
                    "acceptance_checks": [self.__class__.__name__ and
                                          FAILING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        decision = self.ask(plan(proposed_acceptance_checks=[]))

        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIn("drops acceptance check", decision.reason)
        self.assertEqual(self.item().acceptance_checks, [FAILING_CHECK],
                         "the item lost the check the plan tried to drop")

    def test_replacing_a_check_with_an_easier_one_is_a_hard_reject(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [FAILING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke]))

        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertEqual(self.item().acceptance_checks, [FAILING_CHECK])

    def test_adding_to_an_existing_check_is_allowed(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [FAILING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        decision = self.ask(plan(
            proposed_acceptance_checks=[FAILING_CHECK, self.smoke]))

        self.assertEqual(decision.outcome, A.APPLIED, decision.reason)
        self.assertEqual(sorted(self.item().acceptance_checks),
                         sorted([FAILING_CHECK, self.smoke]))


class APlanMayNotRestructureThePortfolio(ArchitectureCase):
    def test_new_top_level_items_are_refused_in_v1(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan(
            proposed_acceptance_checks=[self.smoke],
            new_work_items=[{"outcome": "a whole new feature"}]))

        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIn("new work item", decision.reason)
        self.assertEqual(len(self.portfolio().items), 1)

    def test_a_dependency_on_something_absent_is_refused(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke],
                                 dependencies=["W404"]))

        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIn("W404", decision.reason)
        self.assertEqual(self.item().depends_on, [])

    def test_a_dependency_that_would_close_a_cycle_is_refused(self):
        self.init([{"outcome": "first", "acceptance_checks": []},
                   {"outcome": "second", "acceptance_checks": [self.smoke],
                    "depends_on": ["W001"]}])
        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke],
                                 dependencies=["W002"]))

        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIn("cycle", decision.reason)

    def test_a_dependency_on_a_real_item_is_applied(self):
        self.init([{"outcome": "first", "acceptance_checks": [self.smoke]},
                   {"outcome": "second", "acceptance_checks": []}])
        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke],
                                 dependencies=["W001"]),
                            work_item_id="W002")

        self.assertEqual(decision.outcome, A.APPLIED, decision.reason)
        self.assertEqual(self.item("W002").depends_on, ["W001"])

    def test_raising_the_side_effect_class_needs_a_person(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke],
                                 side_effect_class="EXTERNAL_IRREVERSIBLE"))

        self.assertEqual(decision.outcome, A.NEEDS_HUMAN_APPROVAL,
                         decision.reason)
        self.assertEqual(self.item().acceptance_checks, [])


# ---------------------------------------------------------------------------
# What the architect returns, and what happens when it is not a plan
# ---------------------------------------------------------------------------

class AProposalThatIsNotAPlan(ArchitectureCase):
    def setUp(self):
        super().setUp()
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])

    def _refused(self, payload):
        decision = self.ask(payload)
        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIsNone(decision.plan_id)
        self.assertEqual(self.item().acceptance_checks, [])
        return decision

    def test_prose_is_refused(self):
        self._refused("Sure! I think you should just run the tests.")

    def test_a_missing_field_is_refused(self):
        self._refused({"objective": "no checks key here"})

    def test_a_string_where_a_list_belongs_is_refused(self):
        decision = self._refused({"objective": "o",
                                  "proposed_acceptance_checks": "pytest -q"})
        self.assertIn("must be a list", decision.reason)

    def test_a_non_string_inside_the_list_is_refused(self):
        self._refused({"objective": "o", "proposed_acceptance_checks": [7]})

    def test_a_provider_that_could_not_answer_is_a_decision_not_a_crash(self):
        def broken(_request):
            raise A.PlanRejected("the architect provider timed out")

        decision = A.request_architecture(
            self.data, "W001", project_id=self.project_id, propose=broken)
        self.assertEqual(decision.outcome, A.REJECTED)
        self.assertIn("timed out", decision.reason)

    def test_the_refusal_is_recorded_with_its_reason(self):
        self._refused({"objective": "o"})
        plans = self.store().plans(self.project_id, work_item_id="W001")
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["decision"]["outcome"], A.REJECTED)
        self.assertIsNone(plans[0]["plan"],
                          "a payload that is not a plan must not be stored "
                          "as one")


# ---------------------------------------------------------------------------
# Discovery: the correct answer to an under-evidenced item
# ---------------------------------------------------------------------------

class WhenTheAnswerIsMoreEvidence(ArchitectureCase):
    def test_discovery_without_acceptance_is_its_own_outcome(self):
        self.init([{"outcome": "why is it slow", "acceptance_checks": []}])
        decision = self.ask(plan(
            discovery_steps=[{"kind": "read", "what": "profile the endpoint"}]))

        self.assertEqual(decision.outcome, A.NEEDS_DISCOVERY, decision.reason)
        self.assertIn("discovery", decision.reason)
        self.assertEqual(self.item().acceptance_checks, [],
                         "a discovery plan must not make the item runnable")
        self.assertIsNotNone(decision.plan_id,
                             "the discovery steps must survive as a plan")

    def test_neither_acceptance_nor_discovery_is_a_rejection(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        decision = self.ask(plan())
        self.assertEqual(decision.outcome, A.REJECTED, decision.reason)
        self.assertIn("neither", decision.reason)


# ---------------------------------------------------------------------------
# Asking twice, and dying in the middle
# ---------------------------------------------------------------------------

class TheSameQuestionIsNotPaidForTwice(ArchitectureCase):
    def test_an_identical_world_returns_the_first_decision(self):
        """Asking again about an UNCHANGED world must not pay for a model.

        An applied plan is deliberately not this case: applying changes the
        item's acceptance, so the next request describes a different world and
        is a different question — and by then the item is gradeable and nobody
        asks. What has to dedupe is the outcome that changed nothing.
        """
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        calls = []

        def counting(request):
            calls.append(request.digest)
            return plan(discovery_steps=[{"kind": "read"}])

        first = A.request_architecture(self.data, "W001",
                                       project_id=self.project_id,
                                       propose=counting)
        second = A.request_architecture(self.data, "W001",
                                        project_id=self.project_id,
                                        propose=counting)

        self.assertEqual(first.outcome, A.NEEDS_DISCOVERY, first.reason)
        self.assertEqual(second.outcome, A.NEEDS_DISCOVERY)
        self.assertEqual(len(calls), 1,
                         "the architect was asked twice about one world")
        self.assertEqual(first.plan_id, second.plan_id)

    def test_an_applied_plan_is_not_asked_about_again(self):
        """Not by dedupe — by the item no longer needing an architect."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        calls = []

        def counting(request):
            calls.append(request.digest)
            return plan(proposed_acceptance_checks=[self.smoke])

        A.request_architecture(self.data, "W001", project_id=self.project_id,
                               propose=counting)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.item().needs_architect)

    def test_a_refusal_is_also_remembered(self):
        """Otherwise a loop re-asks a question already answered no."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        calls = []

        def counting(request):
            calls.append(request.digest)
            return plan(proposed_acceptance_checks=["invented --check"])

        A.request_architecture(self.data, "W001", project_id=self.project_id,
                               propose=counting)
        again = A.request_architecture(self.data, "W001",
                                       project_id=self.project_id,
                                       propose=counting)
        self.assertEqual(len(calls), 1)
        self.assertEqual(again.outcome, A.NEEDS_HUMAN_APPROVAL)

    def test_a_crash_before_the_decision_leaves_a_pending_request(self):
        """The request is written first, so this state is reachable and named.

        What must NOT exist is a plan recorded as applied beside an item that
        never changed — the plan, the decision and the item move in one
        transaction.
        """
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        before = self.portfolio().version

        def dies(_request):
            raise KeyboardInterrupt("the process went away mid-call")

        with self.assertRaises(KeyboardInterrupt):
            A.request_architecture(self.data, "W001",
                                   project_id=self.project_id, propose=dies)

        store = self.store()
        pending = store.open_requests(self.project_id)
        self.assertEqual(len(pending), 1, "the question was not recorded")
        self.assertEqual(store.plans(self.project_id), [],
                         "a decision was recorded for a call that never "
                         "returned")
        self.assertEqual(self.portfolio().version, before)
        self.assertEqual(self.item().acceptance_checks, [])

    def test_a_session_opened_after_that_crash_says_so(self):
        from dobby.project import open_session

        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])

        def dies(_request):
            raise KeyboardInterrupt("gone")

        with self.assertRaises(KeyboardInterrupt):
            A.request_architecture(self.data, "W001",
                                   project_id=self.project_id, propose=dies)

        envelope = open_session(self.data)
        self.assertIsNotNone(envelope.pending_request_digest,
                             "a question in flight was invisible to the next "
                             "session")

    def test_resuming_after_the_crash_settles_the_request(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])

        def dies(_request):
            raise KeyboardInterrupt("gone")

        with self.assertRaises(KeyboardInterrupt):
            A.request_architecture(self.data, "W001",
                                   project_id=self.project_id, propose=dies)

        decision = self.ask(plan(proposed_acceptance_checks=[self.smoke]))
        self.assertEqual(decision.outcome, A.APPLIED, decision.reason)
        self.assertEqual(self.store().open_requests(self.project_id), [],
                         "the request stayed open after being answered")


# ---------------------------------------------------------------------------
# Through the loop, which is where it is actually used
# ---------------------------------------------------------------------------

class ThroughTheLoop(ArchitectureCase):
    def test_without_an_architect_the_loop_still_just_stops(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        result = advance(self.data)
        self.assertEqual(result["stopped"], L.NEEDS_ARCHITECT, result)

    def test_an_applied_plan_lets_the_item_run_and_pk2_still_decides(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        result = advance(self.data, propose=lambda _r: plan(
                             proposed_acceptance_checks=[self.smoke]))

        self.assertEqual(result["items_completed"], ["W001"], result)
        step = result["iterations"][0]
        self.assertEqual(step["run_state"], "SUCCEEDED")
        self.assertEqual(step["item_state"], M.DONE)
        self.assertTrue(step["evidence_refs"],
                        "DONE with no promoted artifact behind it")

    def test_an_applied_plan_does_not_make_a_failing_item_pass(self):
        """The architect widened the gate; the gate still decides."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [FAILING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        result = advance(self.data, propose=lambda _r: plan(
                             proposed_acceptance_checks=[FAILING_CHECK,
                                                         self.smoke]))

        self.assertEqual(result["stopped"], L.ITEM_BLOCKED, result)
        self.assertEqual(result["items_completed"], [])
        self.assertEqual(self.item().state, M.BLOCKED)
        # FAILED and not WAITING: the run reached its gate and the gate said no.
        # WAITING would mean it never got that far — which is how this test
        # passed once already, for a reason that had nothing to do with the
        # architect.
        self.assertEqual(result["iterations"][0]["run_state"], "FAILED")

    def test_a_discovery_answer_halts_the_loop_without_running_anything(self):
        self.init([{"outcome": "why is it slow", "acceptance_checks": []}])
        result = advance(self.data, propose=lambda _r: plan(
                             discovery_steps=[{"kind": "read"}]))

        self.assertEqual(result["stopped"], L.NEEDS_DISCOVERY, result)
        self.assertEqual(result["iterations"], [],
                         "a run was started for an item nobody can grade")

    def test_an_unapproved_command_halts_the_loop_and_names_the_reason(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        result = advance(self.data, propose=lambda _r: plan(
                             proposed_acceptance_checks=["curl example.com"]))

        self.assertEqual(result["stopped"], L.NEEDS_HUMAN_APPROVAL, result)
        self.assertIn("never declared", result["detail"])

    def test_a_weakening_plan_halts_the_loop_as_rejected(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [FAILING_CHECK],
                    "uncertainty": M.UNCERTAINTY_ESCALATION}])
        result = advance(self.data, propose=lambda _r: plan(
                             proposed_acceptance_checks=[self.smoke]))

        self.assertEqual(result["stopped"], L.PLAN_REJECTED, result)
        self.assertEqual(self.item().acceptance_checks, [FAILING_CHECK])

    def test_every_architect_stop_reason_is_declared(self):
        for outcome, reason in L._STOP_FOR_OUTCOME.items():
            with self.subTest(outcome=outcome):
                self.assertIn(outcome, A.OUTCOMES)
                self.assertIn(reason, L.STOP_REASONS)


# ---------------------------------------------------------------------------
# The read-only claim, and the request's own identity
# ---------------------------------------------------------------------------

class TheArchitectIsNotGivenWriteAccess(unittest.TestCase):
    def test_the_role_exists_and_prefers_deep_providers(self):
        from dobby.providers.catalog import role_preference
        self.assertTrue(role_preference(A.ARCHITECT_ROLE))

    def test_the_prompt_states_the_allow_list_and_forbids_inventing(self):
        from dobby.project.models import ProjectManifest, WorkItem
        manifest = ProjectManifest(project_id="p", root=".", repo_digest="d",
                                   smoke_checks=("pytest -q",))
        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        acceptance_checks=[])
        prompt = A.build_prompt(
            A.ArchitectureRequest(project_id="p", work_item_id="W001",
                                  trigger=A.MISSING_ACCEPTANCE,
                                  manifest_digest="d", baseline_git_sha="s"),
            item=item, manifest=manifest)
        self.assertIn("pytest -q", prompt)
        self.assertIn("Do NOT invent", prompt)
        self.assertIn("never remove or weaken", prompt.lower())

    def test_run_provider_is_never_given_write_extra(self):
        """The read-only profile is the ABSENCE of that tuple, so assert it.

        Reads the CODE and not the docstring, which mentions `write_extra` by
        name — an earlier version of this assertion failed on its own prose.
        """
        import inspect
        body = inspect.getsource(A.propose_via_provider).split(chr(34) * 3)[-1]
        self.assertNotIn("write_extra", body)
        self.assertNotIn("extra=", body,
                         "passing `extra` is how a CLI is handed edit rights")
        self.assertIn("run_provider(spec", body)


class TheRequestKnowsWhatItAsked(unittest.TestCase):
    def _request(self, **kw):
        base = dict(project_id="p", work_item_id="W001",
                    trigger=A.MISSING_ACCEPTANCE, manifest_digest="d",
                    baseline_git_sha="sha")
        base.update(kw)
        return A.ArchitectureRequest(**base)

    def test_the_clock_and_the_session_are_not_part_of_identity(self):
        one = self._request(session_id="s1", created_at="2020-01-01T00:00:00")
        two = self._request(session_id="s2", created_at="2031-01-01T00:00:00")
        self.assertEqual(one.digest, two.digest)

    def test_a_different_tree_is_a_different_question(self):
        self.assertNotEqual(self._request().digest,
                            self._request(baseline_git_sha="other").digest)

    def test_a_different_contract_is_a_different_question(self):
        self.assertNotEqual(self._request().digest,
                            self._request(manifest_digest="other").digest)

    def test_new_evidence_is_a_different_question(self):
        self.assertNotEqual(self._request().digest,
                            self._request(evidence_refs=("a-1",)).digest)

    def test_an_unknown_trigger_is_refused(self):
        with self.assertRaises(M.ProjectError):
            self._request(trigger="BECAUSE_I_FELT_LIKE_IT")


if __name__ == "__main__":
    unittest.main()
