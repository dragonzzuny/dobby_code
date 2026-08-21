"""Carrying a failure back to the architect, and the order that matters.

`REPLAN` was a declared trigger with no producer for as long as it existed. The
work here is small; the properties worth pinning are about ORDER and IDENTITY,
because both are ways this could be built and be wrong:

ORDER — the deterministic repair runs first. `derive_repair` costs nothing and is
right whenever the artifact was the problem, so a replan tried before it would
spend a model call to rediscover what a command already knew. The test that
matters is the one asserting the architect is NOT called when a repair exists.

IDENTITY — the same failure twice must dedupe to the first answer, and a
different failure must be a different question. Without the failure context in
the digest, a loop would re-ask about an unchanged failure until the budget ran
out and call that persistence.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import architecture as A
from dobby.project import reattempt as R
from dobby.project import replan as RP
from dobby.project.models import BLOCKED, DONE, OPEN, WorkItem

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_CHECK = '{python} -c "import sys; sys.exit(1)"'


def blocked_item(**kw) -> WorkItem:
    base = dict(work_item_id="W001", project_id="p", title="paginate",
                outcome="make it paginate", acceptance_checks=["pytest -q"],
                state=BLOCKED,
                blocked_reason="the run ended FAILED, not SUCCEEDED")
    base.update(kw)
    return WorkItem(**base)


class TheTriggerNamesTheSituation(unittest.TestCase):
    def test_a_blocked_item_with_a_reason_is_a_replan(self):
        self.assertEqual(A.trigger_for(blocked_item()), A.REPLAN)

    def test_blocked_outranks_missing_acceptance(self):
        """An attempted item is not waiting for a definition of done."""
        self.assertEqual(A.trigger_for(blocked_item(acceptance_checks=[])),
                         A.REPLAN)

    def test_a_blocked_item_with_no_recorded_reason_is_not_replannable(self):
        """Asking anyway spends the budget on an empty question."""
        item = blocked_item(blocked_reason="")
        self.assertFalse(RP.blocked_needs_replan(item))
        self.assertNotEqual(A.trigger_for(item), A.REPLAN)

    def test_an_unattempted_item_keeps_its_old_trigger(self):
        self.assertEqual(
            A.trigger_for(WorkItem(work_item_id="W", project_id="p",
                                   title="t")),
            A.MISSING_ACCEPTANCE)


class TheFailureIsPartOfTheQuestion(unittest.TestCase):
    def request(self, context=()):
        return A.ArchitectureRequest(
            project_id="p", work_item_id="W001", trigger=A.REPLAN,
            manifest_digest="d", baseline_git_sha="s",
            failure_context=tuple(context))

    def test_a_different_failure_is_a_different_question(self):
        self.assertNotEqual(self.request(("verify: pytest failed",)).digest,
                            self.request(("verify: import error",)).digest)

    def test_the_same_failure_twice_is_one_question(self):
        """Re-asking about an unchanged failure is what decision_for prevents."""
        self.assertEqual(self.request(("verify: pytest failed",)).digest,
                         self.request(("verify: pytest failed",)).digest)

    def test_the_prompt_shows_the_runtime_record_and_asks_for_something_else(self):
        manifest = type("M", (), {"smoke_checks": ("pytest -q",), "root": "."})()
        prompt = A.build_prompt(self.request(("verify (verify) contract: pytest "
                                              "exited 1",)),
                                item=blocked_item(), manifest=manifest)
        self.assertIn("What failed last time", prompt)
        self.assertIn("pytest exited 1", prompt)
        self.assertIn("propose a different one", prompt)

    def test_an_unfailed_request_shows_no_failure_section(self):
        manifest = type("M", (), {"smoke_checks": ("pytest -q",), "root": "."})()
        prompt = A.build_prompt(
            A.ArchitectureRequest(project_id="p", work_item_id="W001",
                                  trigger=A.MISSING_ACCEPTANCE,
                                  manifest_digest="d", baseline_git_sha="s"),
            item=blocked_item(), manifest=manifest)
        self.assertNotIn("What failed last time", prompt)


class TheFailureContextComesFromTheRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unreadable_run_yields_nothing_rather_than_raising(self):
        """The caller reads empty as "nothing new to tell the architect"."""
        self.assertEqual(RP.failure_context(self.data, "no-such-run"), ())

    def test_a_real_failed_run_names_the_node_that_failed(self):
        from dobby.project import loop as L
        initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                   item_specs=[{"outcome": "will not verify",
                                "acceptance_checks": [FAILING_CHECK]}])
        result = L.advance(self.data)
        self.assertEqual(result["stopped"], L.ITEM_BLOCKED, result)
        run_id = result["iterations"][0]["run_id"]

        context = RP.failure_context(self.data, run_id)
        self.assertTrue(context, "a failed run produced no failure context")
        self.assertTrue(any("verify" in line for line in context), context)

    def test_the_detail_is_capped(self):
        from dobby.runtime import graph as G

        class Node:
            kind = "execute"
            state = G.NODE_FAILED
            last_failure = {"kind": "contract", "message": "x" * 5000}

        class Graph:
            nodes = {"execute": Node()}

            def topological_order(self):
                return ["execute"]

        class Store:
            def __init__(self, _data):
                pass

            def load_run(self, _run_id):
                return {"graph": Graph()}

        import dobby.runtime.store as store_mod
        original = store_mod.RunStore
        store_mod.RunStore = Store
        try:
            context = RP.failure_context(self.data, "r", detail_cap=50)
        finally:
            store_mod.RunStore = original
        self.assertLess(len(context[0]), 200, context[0])


class TheRepairIsTriedBeforeTheModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.artifact = os.path.join(self.root, "lit.json").replace("\\", "/")
        self.asked = []
        self.saved_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = REPO

    def tearDown(self):
        if self.saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.saved_pythonpath
        self.tmp.cleanup()

    def propose(self, request):
        """A plan that WIDENS the definition of done, which is all it may do.

        The failing check stays. An architect may add to the definition of done
        and may never narrow it, so a replan cannot rescue a broken
        implementation by dropping the check that caught it — the first version
        of this fixture proposed only the passing check and was rejected for
        exactly that, which is the rule working.
        """
        self.asked.append(request.trigger)
        return {"objective": "try it another way",
                "proposed_acceptance_checks": [FAILING_CHECK, PASSING_SMOKE]}

    def init(self, checks):
        initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                   item_specs=[{"outcome": "do the thing",
                                "acceptance_checks": checks}])

    def item(self):
        return ProjectStore(self.data).load_project(None)["portfolio"].get(
            "W001")

    def test_an_artifact_failure_is_repaired_without_asking_a_model(self):
        """The whole ordering argument: a command already knew this."""
        import json
        with open(self.artifact, "w", encoding="utf-8") as fh:
            json.dump({"sources": []}, fh)
        check = (f'{{python}} -m dobby.cli project check --kind literature '
                 f'--file "{self.artifact}" --min 5')
        self.init([check])

        result = R.persevere(self.data, max_attempts=2, replan=True,
                             replan_propose=self.propose)
        self.assertEqual(self.asked, [],
                         "the architect was paid for a failure a check had "
                         "already explained")
        self.assertGreater(result["repairs_applied"], 0)

    def test_a_failure_with_nothing_to_repair_reaches_the_architect(self):
        """Asked once per attempt here, and both asks are legitimate.

        The first plan WIDENED the item's acceptance, so by the second attempt
        the item's contract genuinely differs and the question really is a new
        one. The test below covers the other case — a plan that changed nothing
        must NOT be paid for twice.
        """
        self.init([FAILING_CHECK])
        result = R.persevere(self.data, max_attempts=2, replan=True,
                             replan_propose=self.propose)
        self.assertEqual(self.asked, [A.REPLAN, A.REPLAN], result)
        self.assertEqual(result["replans_applied"], 2, result)

    def test_a_replan_that_changed_nothing_is_not_paid_for_twice(self):
        """The dedupe has to survive the harness annotating the item about it."""
        self.init([FAILING_CHECK])

        def unchanged(request):
            self.asked.append(request.trigger)
            return {"objective": "same shape, no widening",
                    "proposed_acceptance_checks": [FAILING_CHECK]}

        R.persevere(self.data, max_attempts=3, replan=True,
                    replan_propose=unchanged)
        self.assertEqual(self.asked, [A.REPLAN],
                         "the architect was paid again for a question whose "
                         "only change was this harness's own note to itself")

    def test_an_applied_replan_reopens_the_item_and_records_why(self):
        self.init([FAILING_CHECK])
        R.persevere(self.data, max_attempts=2, replan=True,
                    replan_propose=self.propose)
        item = self.item()
        self.assertIn(R.REPLAN_MARKER.strip(), item.outcome)
        self.assertTrue(item.outcome.startswith("do the thing"))

    def test_without_the_flag_the_old_dead_end_is_unchanged(self):
        self.init([FAILING_CHECK])
        result = R.persevere(self.data, max_attempts=2)
        self.assertEqual(result["stopped"], R.NO_REPAIR_DERIVED)
        self.assertEqual(self.asked, [])

    def test_an_architect_that_refuses_is_an_explicit_stop_not_a_retry(self):
        self.init([FAILING_CHECK])

        def refusing(request):
            self.asked.append(request.trigger)
            return {"objective": "x",
                    "proposed_acceptance_checks": ["invented --check"]}

        result = R.persevere(self.data, max_attempts=3, replan=True,
                             replan_propose=refusing)
        self.assertEqual(result["stopped"], R.REPLAN_NOT_APPLIED, result)
        self.assertEqual(result["replans_applied"], 0)
        self.assertIn("REJECTED", result["detail"])

    def test_the_replan_is_reported_in_the_attempt_record(self):
        self.init([FAILING_CHECK])
        result = R.persevere(self.data, max_attempts=2, replan=True,
                             replan_propose=self.propose)
        recorded = [a for a in result["attempts"] if a.get("replan")]
        self.assertTrue(recorded, result["attempts"])
        self.assertEqual(recorded[0]["replan"]["outcome"], A.APPLIED)

    def test_the_contract_digest_covers_every_field_the_prompt_shows(self):
        """The guard that replaced `version` as a catch-all.

        `version` bumped on every write, including ones that change nothing an
        architect sees, so it made almost every touched item a new question. The
        real property is narrower and this asserts it directly: a field
        `build_prompt` reads must be one the contract digest covers, or a future
        edit reaches the model without reaching identity.
        """
        import inspect
        import re
        source = inspect.getsource(A.build_prompt)
        referenced = set(re.findall(r"item\.([a-z_]+)", source))
        # Neither is shown to the architect; both are read for structure only.
        referenced -= {"work_item_id"}
        missing = referenced - set(WorkItem.CONTRACT_FIELDS)
        self.assertEqual(missing, set(),
                         f"build_prompt shows the architect {sorted(missing)}, "
                         f"which architect_contract_digest does not cover: an "
                         f"edit to those would change the question without "
                         f"changing its identity")

    def test_a_bare_version_bump_is_not_a_new_question(self):
        base = WorkItem(work_item_id="W", project_id="p", title="t",
                        outcome="o")
        bumped = WorkItem(work_item_id="W", project_id="p", title="t",
                          outcome="o", version=9)
        self.assertEqual(base.architect_contract_digest,
                         bumped.architect_contract_digest)

    def test_a_harness_annotation_is_not_a_new_question(self):
        base = WorkItem(work_item_id="W", project_id="p", title="t",
                        outcome="do the thing")
        noted = WorkItem(work_item_id="W", project_id="p", title="t",
                         outcome="do the thing" + R.REPLAN_MARKER + "plan x")
        self.assertEqual(base.architect_contract_digest,
                         noted.architect_contract_digest)

    def test_a_real_rewrite_still_is(self):
        base = WorkItem(work_item_id="W", project_id="p", title="t",
                        outcome="do the thing")
        rewritten = WorkItem(work_item_id="W", project_id="p", title="t",
                             outcome="do a different thing")
        self.assertNotEqual(base.architect_contract_digest,
                            rewritten.architect_contract_digest)

    def test_every_stop_reason_is_a_declared_one(self):
        self.init([FAILING_CHECK])
        result = R.persevere(self.data, max_attempts=2)
        from dobby.project import loop as L
        self.assertIn(result["stopped"], R.STOP_REASONS + L.STOP_REASONS)


if __name__ == "__main__":
    unittest.main()
