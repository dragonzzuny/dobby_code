"""'Verified' was one word for four different claims.

`declares_nothing` turned the bottom of the ladder into a refusal: a contract
with no schema, no check, no effect and nothing to ground cannot promote. Every
rung above it read the same, and the gap that leaves is not hypothetical.

In `reports/RESULTS_three_arm_regression.md` the dobby arm on
`django__django-11138` localised all three gold files, wrote a patch, broke four
`timezones` tests, and PROMOTED. Nothing was bypassed. Its declared acceptance
check was `evals/swebench/check_syntax.py`, whose docstring opens "deliberately
weak" -- every changed python file still parses. The gate enforced exactly what
was declared, and what was declared was rung 2 while the defect was on rung 4.

The fix is not a ban on weak checks. That arm could not run django's suite
because Docker is absent from the machine, and refusing it would have deleted
the experiment rather than caught the bug. The fix is that the rung travels with
the artifact, the way `advisory` and `ungraded` already do.

Two design decisions worth stating, because both could have gone the other way:

- `checks_at` is DECLARED, never inferred. `pytest -q` and a no-op are the same
  shape to this process. Guessing from the text would put a model's inference
  where a person's statement belongs, and a guess that flatters is what the
  ladder exists to catch.
- An undeclared check counts as EXISTENCE. It ran and exited zero; that is the
  most anyone can say about it from here.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import (ArtifactContract, ContractError,  # noqa: E402
                                     LOCAL_WRITE, SCHEMAS,
                                     V_BEHAVIOR, V_CONTRACT, V_EXISTENCE,
                                     V_NONE, V_STRUCTURE,
                                     VERIFICATION_LEVELS, level_name)
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.verify import Verifier, promotable  # noqa: E402
from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,  # noqa: E402
                                   WorkerResult)

#: A shell check that passes and proves almost nothing. Stands in for
#: `evals/swebench/check_syntax.py`, which is the real one.
NOOP_CHECK = "python -c \"import sys; sys.exit(0)\""


class TheLadder(unittest.TestCase):
    def test_the_rungs_are_ordered_and_named(self):
        self.assertLess(V_NONE, V_EXISTENCE)
        self.assertLess(V_EXISTENCE, V_STRUCTURE)
        self.assertLess(V_STRUCTURE, V_CONTRACT)
        self.assertLess(V_CONTRACT, V_BEHAVIOR)
        self.assertEqual(level_name(V_BEHAVIOR), "BEHAVIOR")
        self.assertEqual(len(VERIFICATION_LEVELS), 5)

    def test_a_vacuous_contract_is_the_bottom_rung(self):
        self.assertEqual(ArtifactContract().declared_level, V_NONE)

    def test_a_schema_is_structure(self):
        self.assertEqual(
            ArtifactContract(output_schema={"type": "object"}).declared_level,
            V_STRUCTURE)

    def test_grounding_is_contract_because_it_recomputes(self):
        """A schema says the field is an int. Grounding says it is THE int."""
        self.assertEqual(
            ArtifactContract(grounding={"claims_at": "claims"}).declared_level,
            V_CONTRACT)

    def test_a_side_effect_is_existence_weak_but_not_vacuous(self):
        self.assertEqual(
            ArtifactContract(side_effect_class=LOCAL_WRITE).declared_level,
            V_EXISTENCE)

    def test_an_undeclared_shell_check_counts_as_its_floor(self):
        self.assertEqual(
            ArtifactContract(acceptance_checks=[NOOP_CHECK]).declared_level,
            V_EXISTENCE)

    def test_and_a_declared_one_counts_as_declared(self):
        self.assertEqual(
            ArtifactContract(acceptance_checks=["pytest -q"],
                             checks_at=V_BEHAVIOR).declared_level,
            V_BEHAVIOR)

    def test_the_level_is_the_highest_rung_not_the_sum(self):
        self.assertEqual(
            ArtifactContract(output_schema={"type": "object"},
                             acceptance_checks=["pytest -q"],
                             checks_at=V_BEHAVIOR).declared_level,
            V_BEHAVIOR)

    def test_a_level_outside_the_ladder_is_refused(self):
        with self.assertRaises(ContractError):
            ArtifactContract(acceptance_checks=["x"], checks_at=9)


class TheFloorIsCheckedWhenItIsWritten(unittest.TestCase):
    def test_asking_for_more_than_you_declare_fails_at_construction(self):
        """Not at promotion. A contract that can never be satisfied is a
        definition defect, and a run that fails on it reports the wrong thing.
        """
        with self.assertRaises(ContractError) as caught:
            ArtifactContract(acceptance_checks=[NOOP_CHECK],
                             requires_level=V_BEHAVIOR)
        self.assertIn("BEHAVIOR", str(caught.exception))
        self.assertIn("EXISTENCE", str(caught.exception))

    def test_the_same_contract_is_fine_once_the_check_is_declared(self):
        contract = ArtifactContract(acceptance_checks=["pytest -q"],
                                    checks_at=V_BEHAVIOR,
                                    requires_level=V_BEHAVIOR)
        self.assertEqual(contract.declared_level, V_BEHAVIOR)

    def test_no_floor_is_the_default_so_nothing_existing_changes(self):
        self.assertEqual(ArtifactContract().requires_level, V_NONE)


class TheElevenOneThreeEightShape(unittest.TestCase):
    """The exact configuration that promoted a patch breaking four tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def syntax_only(self, **extra):
        """What the SWE-bench D arm declared: one weak shell check, undeclared."""
        return ArtifactContract(output_schema={"type": "object"},
                                acceptance_checks=[NOOP_CHECK], **extra)

    def test_it_still_passes_because_the_check_really_did_pass(self):
        contract = self.syntax_only()
        verdict = Verifier(repo=self.tmp.name).verify(contract, {"done": True})
        self.assertTrue(verdict.passed, verdict.failed_requirements)
        self.assertTrue(promotable(contract, verdict))

    def test_but_it_now_says_which_rung(self):
        contract = self.syntax_only()
        verdict = Verifier(repo=self.tmp.name).verify(contract, {"done": True})
        self.assertEqual(verdict.level, V_STRUCTURE)
        self.assertEqual(verdict.level_label, "STRUCTURE")

    def test_a_consumer_that_needs_behaviour_cannot_declare_it_here(self):
        """Which is the catch: the mismatch surfaces where the contract is
        WRITTEN, before any provider is paid.
        """
        with self.assertRaises(ContractError):
            self.syntax_only(requires_level=V_BEHAVIOR)

    def test_declaring_the_real_check_lets_the_floor_hold(self):
        contract = ArtifactContract(output_schema={"type": "object"},
                                    acceptance_checks=[NOOP_CHECK],
                                    checks_at=V_BEHAVIOR,
                                    requires_level=V_BEHAVIOR)
        verdict = Verifier(repo=self.tmp.name).verify(contract, {"done": True})
        self.assertEqual(verdict.level, V_BEHAVIOR)
        self.assertTrue(promotable(contract, verdict))

    def test_the_rung_reached_is_none_when_a_check_could_not_run(self):
        """A rung is what was CLIMBED. `not_run` already blocks promotion; this
        stops the artifact from also carrying a level it never reached.

        Driven with a TIMEOUT rather than a bogus binary. Measured: a name the
        system cannot resolve still comes back with an exit code, because the
        check runs under `shell=True` and the shell reports the failure itself.
        `not_run` is `exit_code is None`, which is a timeout or an OSError --
        the two cases where nothing produced a verdict at all.
        """
        contract = ArtifactContract(
            output_schema={"type": "object"},
            acceptance_checks=["python -c \"import time; time.sleep(30)\""],
            checks_at=V_BEHAVIOR)
        verdict = Verifier(repo=self.tmp.name,
                           timeout_s=1).verify(contract, {"done": True})
        self.assertEqual(verdict.not_run, contract.acceptance_checks)
        self.assertEqual(verdict.level, V_NONE)
        self.assertFalse(promotable(contract, verdict))

    def test_and_the_same_check_inside_its_timeout_reaches_its_rung(self):
        """The control: same command, enough time, so the difference measured
        above is the timeout and not the command."""
        contract = ArtifactContract(
            output_schema={"type": "object"},
            acceptance_checks=["python -c \"import time; time.sleep(0)\""],
            checks_at=V_BEHAVIOR)
        verdict = Verifier(repo=self.tmp.name,
                           timeout_s=60).verify(contract, {"done": True})
        self.assertEqual(verdict.not_run, [])
        self.assertEqual(verdict.level, V_BEHAVIOR)


class ThroughTheRunner(unittest.TestCase):
    class Ok(WorkerAdapter):
        name = "provider"

        def run(self, node, context):
            return WorkerResult(True, payload={
                "summary": "고쳤다. 테스트 세 개가 깨졌고 원인은 전부 같았다. "
                           "이유는 로그에 다 있었는데 아무도 안 봤다. "
                           "지금은 통과한다.",
                "not_established": []})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_with(self, contract):
        runner = Runner(repo=self.tmp.name,
                        data_dir=os.path.join(self.tmp.name, "d"),
                        workers=WorkerRegistry({"provider": self.Ok()}),
                        sleep=lambda _s: None)
        node = G.TaskNode(node_id="n", kind="report", worker="provider",
                          instruction="i", contract=contract,
                          config={"provider": "claude"})
        result = runner.run(runner.start("t", G.TaskGraph([node])))
        return result, runner

    def step(self, result):
        return next(s for s in result.to_dict()["steps"] if s["node_id"] == "n")

    def test_a_promoted_artifact_records_the_rung_it_reached(self):
        result, _ = self.run_with(
            ArtifactContract(output_schema=SCHEMAS["report"]))
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(self.step(result)["verified_at"], "STRUCTURE")

    def test_a_stronger_contract_reports_a_higher_rung(self):
        """Two SUCCEEDED steps that used to be indistinguishable."""
        result, _ = self.run_with(ArtifactContract(
            output_schema=SCHEMAS["report"],
            acceptance_checks=[NOOP_CHECK], checks_at=V_BEHAVIOR))
        self.assertEqual(self.step(result)["verified_at"], "BEHAVIOR")

    def test_a_second_process_reads_the_same_rung_off_disk(self):
        """Kept on the artifact file, not in this process's memory.

        A fresh `Runner` over the same data directory is what a resume is, and
        it must report the rung the first one recorded. This is why
        `_level_of` reads the file instead of the runner keeping a dict: a
        level that lived in memory would be absent from every resumed run and
        present in every fresh one, which is the worst of both.
        """
        result, first = self.run_with(
            ArtifactContract(output_schema=SCHEMAS["report"]))
        self.assertEqual(self.step(result)["verified_at"], "STRUCTURE")

        second = Runner(repo=self.tmp.name,
                        data_dir=os.path.join(self.tmp.name, "d"),
                        workers=WorkerRegistry({"provider": self.Ok()}),
                        sleep=lambda _s: None)
        rows = second.store.artifacts(result.run_id, node_id="n",
                                      state="PROMOTED")
        self.assertEqual(Runner._level_of(rows[-1]), "STRUCTURE")

    def test_an_unreadable_artifact_reports_none_rather_than_a_rung(self):
        """Not "no level" -- "this process could not tell you"."""
        self.assertIsNone(Runner._level_of({"path": ""}))
        self.assertIsNone(Runner._level_of(
            {"path": os.path.join(self.tmp.name, "gone.json")}))

    def test_the_attempt_detail_carries_it_too(self):
        result, runner = self.run_with(
            ArtifactContract(output_schema=SCHEMAS["report"]))
        detail = runner.store.attempts(result.run_id, "n")[-1]["detail"]
        self.assertIn("verified at STRUCTURE", detail)


class TheProjectLayerCanDeclareItToo(unittest.TestCase):
    """Where the work actually happens, and where it could not be said.

    The rung could be declared in `default_graph` and on the CLI, which is to
    say everywhere except the layer the SWE-bench arm and every real project
    run through. `WorkItem` had `acceptance_checks` and no way to say what they
    reach, so every project item sat silently at the floor -- a declaration
    path that exists but not at the point of use is the `claude_quota` shape:
    present, importable, and reaching nothing.
    """

    def item(self, **kw):
        from dobby.project.models import WorkItem

        base = dict(work_item_id="W001", project_id="p", title="paginate",
                    outcome="make it paginate",
                    acceptance_checks=["python -m unittest discover -s tests"])
        base.update(kw)
        return WorkItem(**base)

    def test_an_item_defaults_to_the_floor(self):
        self.assertEqual(self.item().checks_at, V_EXISTENCE)

    def test_an_item_may_declare_what_its_checks_reach(self):
        self.assertEqual(self.item(checks_at=V_BEHAVIOR).checks_at, V_BEHAVIOR)

    def test_claiming_a_rung_with_no_check_to_reach_it_is_refused(self):
        """The vacuous-contract shape one layer up: nothing to run, and a label
        saying it was run thoroughly."""
        from dobby.project.models import ProjectError

        with self.assertRaises(ProjectError) as caught:
            self.item(acceptance_checks=[], checks_at=V_BEHAVIOR)
        self.assertIn("no acceptance check", str(caught.exception))

    def test_a_level_outside_the_ladder_is_refused(self):
        from dobby.project.models import ProjectError

        with self.assertRaises(ProjectError):
            self.item(checks_at=99)

    def test_the_declaration_reaches_the_compiled_verify_contract(self):
        """The half that makes it more than a stored field."""
        import tempfile

        from dobby.project import architecture as A, workorder as W
        from dobby.project.models import ProjectManifest
        from dobby.runtime.contracts import LOCAL_WRITE

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            manifest = ProjectManifest(project_id="p", root=root,
                                       repo_digest="d",
                                       smoke_checks=("pytest -q",))
            plan = A.PlanSpec(
                plan_id="pl-1", work_item_id="W001", objective="paginate it",
                side_effect_class=LOCAL_WRITE,
                execution_steps=({"role": "implement", "objective": "add it",
                                  "write_set": ["app.py"],
                                  "read_set": ["app.py"]},))
            for level in (V_EXISTENCE, V_BEHAVIOR):
                graph = W.compile_graph(plan, item=self.item(checks_at=level),
                                        manifest=manifest, static=True)
                self.assertEqual(
                    graph.nodes["verify"].contract.checks_at, level)


if __name__ == "__main__":
    unittest.main()
