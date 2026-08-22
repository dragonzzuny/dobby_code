"""What the escalations bought, joined from records rather than from reports.

The provider half of this already existed — `spend.py`, `runtime/metrics.py`,
and `placement.py` feeding the scorecard back into routing. Adding a second
provider-level telemetry system would have produced two numbers for one thing,
which this repository treats as making both suspect. So what is tested here is
the level that was missing: the escalation decisions, counted from the store.

Two properties carry the weight.

DERIVED, NOT REPORTED. Whether a run was compiled comes from the node ids of the
graph the RUNTIME stored, not from the `graph` field `advance` returns. That
field is a report; the run store is the record, and a scorecard that trusted the
report could not show the two disagreeing.

UNMEASURED IS NOT ZERO. A cost nothing recorded stays None. A zero would read as
"free" and mean "nobody looked", and that distinction is most of what this
harness is for.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import architecture as A
from dobby.project import loop as L
from dobby.project import scorecard as S

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_CHECK = '{python} -c "import sys; sys.exit(1)"'


class ScorecardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, items):
        report = initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                            item_specs=items)
        self.project_id = report["project_id"]

    def card(self):
        return S.policy_scorecard(self.data, self.project_id)

    def ask(self, payload, work_item_id="W001", **kw):
        return A.request_architecture(self.data, work_item_id,
                                      project_id=self.project_id,
                                      propose=lambda _r: payload, **kw)


class ItCountsWhatActuallyHappened(ScorecardCase):
    def test_a_fresh_project_reports_no_escalation_and_no_denominator(self):
        """None, not zero: nothing finished is not the same as nothing spent."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        card = self.card()
        self.assertEqual(card["architect"]["by_trigger"], {})
        self.assertIsNone(card["escalation_per_done"])
        self.assertIn("not the same as no escalation cost",
                      card["escalation_note"])

    def test_an_architect_call_is_counted_under_its_trigger(self):
        self.init([{"outcome": "make it gradeable", "acceptance_checks": []}])
        self.ask({"objective": "make it gradeable", "proposed_acceptance_checks": [PASSING_SMOKE]})
        card = self.card()
        self.assertEqual(
            card["architect"]["by_trigger"][A.MISSING_ACCEPTANCE]["calls"], 1)
        self.assertEqual(
            card["architect"]["by_trigger"][A.MISSING_ACCEPTANCE]
            ["outcomes"][A.APPLIED], 1)

    def test_each_kind_of_answer_lands_in_its_own_bucket(self):
        """An invented command is NEEDS_HUMAN_APPROVAL, not REJECTED.

        The distinction belongs to the architecture boundary, not to this
        module: a command the manifest never declared is something a person can
        authorise, while dropping an existing check is refused outright. A
        scorecard that merged the two would hide which kind of plan the
        architect keeps producing, and those need different fixes.
        """
        self.init([{"outcome": "make the endpoint paginate",
                    "acceptance_checks": []},
                   {"outcome": "make the endpoint stream",
                    "acceptance_checks": []}])
        self.ask({"objective": "make it gradeable",
                  "proposed_acceptance_checks": [PASSING_SMOKE]})
        self.ask({"objective": "invent a command",
                  "proposed_acceptance_checks": ["invented --check"]},
                 work_item_id="W002")
        outcomes = self.card()["architect"]["by_trigger"][
            A.MISSING_ACCEPTANCE]["outcomes"]
        self.assertEqual(outcomes.get(A.APPLIED), 1, outcomes)
        self.assertEqual(outcomes.get(A.NEEDS_HUMAN_APPROVAL), 1, outcomes)

    def test_a_dropped_check_lands_as_a_rejection(self):
        self.init([{"outcome": "make the endpoint paginate",
                    "acceptance_checks": [PASSING_SMOKE]}])
        self.ask({"objective": "quietly narrow the definition of done",
                  "proposed_acceptance_checks": []},
                 trigger=A.HIGH_UNCERTAINTY)
        outcomes = self.card()["architect"]["by_trigger"][
            A.HIGH_UNCERTAINTY]["outcomes"]
        self.assertEqual(outcomes.get(A.REJECTED), 1, outcomes)

    def test_a_budget_refusal_is_not_counted_as_an_architect_rejection(self):
        """Merging them would make a working budget look like a run of failures."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        store = ProjectStore(self.data)
        for n in range(A.ARCHITECT_CALL_CEILING + 1):
            self.ask({"objective": "make it gradeable",
                      "proposed_acceptance_checks": [],
                      "discovery_steps": [{"kind": "read", "n": n}]})
            project = store.load_project(self.project_id)
            item = project["portfolio"].get("W001")
            item.evidence_refs = list(item.evidence_refs) + [f"e-{n}"]
            store.update_item(item,
                              expected_version=project["portfolio"].version,
                              reason="nudge")

        card = self.card()
        self.assertEqual(card["architect"]["budget_refusals"], 1, card)
        self.assertEqual(card["architect"]["ceiling"], A.ARCHITECT_CALL_CEILING)
        outcomes = card["architect"]["by_trigger"][A.MISSING_ACCEPTANCE][
            "outcomes"]
        self.assertNotIn(A.NEEDS_HUMAN_APPROVAL, outcomes,
                         "the budget's own refusal was filed as a decision")

    def test_escalation_per_done_is_a_ratio_once_something_finished(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        self.ask({"objective": "make it gradeable",
                  "proposed_acceptance_checks": [PASSING_SMOKE]})
        L.advance(self.data)
        card = self.card()
        self.assertEqual(card["items"]["done"], 1, card["items"])
        self.assertEqual(card["escalation_per_done"], 1.0)


class TheShapeOfARunIsDerivedNotBelieved(ScorecardCase):
    def test_a_generic_run_is_identified_from_its_stored_graph(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        L.advance(self.data)
        card = self.card()
        self.assertEqual(card["items"]["generic_runs"], 1, card["items"])
        self.assertEqual(card["items"]["compiled_runs"], 0)

    def test_a_compiled_run_is_identified_from_its_stored_graph(self):
        """Not from `advance`'s `graph` field, which is a report about itself."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": []}])
        self.ask({"objective": "edit it",
                  "proposed_acceptance_checks": [PASSING_SMOKE],
                  "side_effect_class": "LOCAL_WRITE",
                  "execution_steps": [{"role": "implement",
                                       "objective": "edit it",
                                       "write_set": ["app.py"]}]})
        L.advance(self.data, compile_plans=True)
        card = self.card()
        self.assertEqual(card["items"]["compiled_runs"], 1, card["items"])
        self.assertEqual(card["items"]["generic_runs"], 0)

    def test_an_item_that_never_ran_has_no_run_shape(self):
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        row = self.card()["items"]["items"][0]
        self.assertEqual(row["graph"], "none")

    def test_a_blocked_item_carries_its_reason_into_the_card(self):
        self.init([{"outcome": "will not verify",
                    "acceptance_checks": [FAILING_CHECK]}])
        L.advance(self.data)
        row = self.card()["items"]["items"][0]
        self.assertEqual(row["state"], "BLOCKED")
        self.assertIn("FAILED", row["blocked_reason"])


class ItSaysWhatItDidNotMeasure(ScorecardCase):
    def test_an_unrecorded_cost_stays_null(self):
        """A zero would read as free and mean nobody looked."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        L.advance(self.data)
        cost = self.card()["runtime"]["cost_per_verified_task"]
        self.assertIn("value", cost)
        self.assertIsNone(cost["value"])

    def test_the_merge_gate_is_named_as_uncounted(self):
        """A policy view that omitted it would invite the reader to assume zero."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        unmeasured = " ".join(self.card()["unmeasured"])
        self.assertIn("workspace_merges", unmeasured)

    def test_it_changes_nothing(self):
        """A scorecard that moved the thing it grades is useless as an input."""
        self.init([{"outcome": "make the endpoint paginate", "acceptance_checks": [PASSING_SMOKE]}])
        L.advance(self.data)
        store = ProjectStore(self.data)
        before = store.load_project(self.project_id)["portfolio"].version
        events_before = len(store.events(self.project_id))

        self.card()
        self.card()

        after = ProjectStore(self.data).load_project(self.project_id)
        self.assertEqual(after["portfolio"].version, before)
        self.assertEqual(len(ProjectStore(self.data).events(self.project_id)),
                         events_before)


if __name__ == "__main__":
    unittest.main()
