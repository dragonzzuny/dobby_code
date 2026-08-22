"""A declared side effect that grants nothing, and a worker that said ok anyway.

Measured 2026-08-22 before any of this existed: a node with
`side_effect_class=LOCAL_WRITE` and an instruction to create a file returned
`ok: True`, `failure: None`, and created nothing. Two faults met there and they
are tested apart, because fixing one without the other leaves the worse half:

MISSING WIRE — `ProviderWorker` never passed `write_extra`, so the CLI ran under
its default read-only mode. Annoying, visible the moment anything checks.

UNFOUNDED CLAIM — nothing checked, so the layer whose job is to say what happened
said the work was done. That is the fault worth a regression suite: a well-shaped
payload describing work that did not happen passes every downstream check that
reads the payload.

The probe that settled the flag is recorded rather than repeated here: running
claude with `--permission-mode acceptEdits` in a fresh temp directory and an
instruction to create one file left the file on disk. These tests use a stub
provider, so they assert the WIRING and the REFUSALS without spending money —
and the one thing a stub cannot prove, that the flag really grants writes, is
what the probe was for.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers.base import ProviderResult
from dobby.runtime import effects, workers
from dobby.runtime import graph as G
from dobby.runtime.contracts import (LOCAL_WRITE, NONE, SCHEMAS,
                                     ArtifactContract)
from dobby.runtime.failures import (DEFAULT_POLICY, EFFECT_NOT_OBSERVED,
                                    FAIL, PERMISSION_DENIED, REPAIR)


class StubProvider:
    """Stands in for the CLI. Records what argv extras it was handed."""

    def __init__(self, on_call=None, denials=(), text="done"):
        self.calls = []
        self.on_call = on_call
        self.denials = list(denials)
        self.text = text

    def __call__(self, spec, prompt, *, model=None, extra=(), cwd=None,
                 timeout_s=None, **kw):
        self.calls.append({"extra": tuple(extra), "cwd": cwd})
        if self.on_call:
            self.on_call(cwd)
        meta = {"permission_denials": self.denials} if self.denials else {}
        return ProviderResult(provider=spec.id, ok=True, text=self.text,
                              exit_code=0, duration_s=0.1, meta=meta)

    @property
    def last_extra(self):
        return self.calls[-1]["extra"] if self.calls else None


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "existing.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("before\n")

    def tearDown(self):
        self.tmp.cleanup()

    def node(self, *, side_effect=LOCAL_WRITE, expected=(), schema=""):
        return G.TaskNode(
            node_id="execute", kind="execute", worker="provider",
            instruction="do the thing",
            contract=ArtifactContract(
                side_effect_class=side_effect,
                expected_paths=list(expected),
                output_schema=(SCHEMAS[schema] if schema else {})),
            config={"provider": "claude"})

    def run_node(self, node, stub):
        import dobby.providers.run as run_mod
        original = run_mod.run_provider
        run_mod.run_provider = stub
        try:
            return workers.ProviderWorker().run(
                node, {"repo": self.root, "cwd": self.root})
        finally:
            run_mod.run_provider = original

    def touch(self, name):
        def _write(cwd):
            with open(os.path.join(cwd, name), "w", encoding="utf-8") as fh:
                fh.write("written\n")
        return _write


class ReadOnlyStaysReadOnly(WorkerCase):
    def test_a_read_only_node_is_granted_nothing(self):
        stub = StubProvider()
        result = self.run_node(self.node(side_effect=NONE), stub)
        self.assertTrue(result.ok)
        self.assertEqual(stub.last_extra, (),
                         "a read-only node was handed edit rights")

    def test_a_read_only_node_is_not_effect_checked(self):
        """It promises no trace, so demanding one would fail every honest one."""
        stub = StubProvider()
        result = self.run_node(self.node(side_effect=NONE), stub)
        self.assertTrue(result.ok)
        self.assertNotIn("effect_observed", result.meta)


class AWritingNodeIsActuallyGrantedTheRight(WorkerCase):
    def test_the_catalogs_write_extra_reaches_the_call(self):
        """The missing wire. Verified by probe that this flag really grants writes."""
        stub = StubProvider(on_call=self.touch("made.txt"))
        result = self.run_node(self.node(), stub)
        self.assertTrue(result.ok, result.failure)
        self.assertEqual(stub.last_extra, ("--permission-mode", "acceptEdits"))

    def test_a_write_that_happened_succeeds_and_says_what_it_saw(self):
        stub = StubProvider(on_call=self.touch("made.txt"))
        result = self.run_node(self.node(), stub)
        self.assertTrue(result.ok, result.failure)
        self.assertTrue(result.meta["effect_observed"])
        self.assertIn("made.txt", result.meta["effect_detail"])

    def test_a_declared_path_that_appears_satisfies_the_check(self):
        stub = StubProvider(on_call=self.touch("wanted.txt"))
        result = self.run_node(self.node(expected=["wanted.txt"]), stub)
        self.assertTrue(result.ok, result.failure)

    def test_modifying_an_existing_file_counts_as_an_effect(self):
        def rewrite(cwd):
            with open(os.path.join(cwd, "existing.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("after, and longer than before\n")

        stub = StubProvider(on_call=rewrite)
        result = self.run_node(self.node(expected=["existing.txt"]), stub)
        self.assertTrue(result.ok, result.failure)
        self.assertIn("modified", result.meta["effect_detail"])


class AWriteThatDidNotHappenIsNotASuccess(WorkerCase):
    def test_the_measured_defect_is_now_a_failure(self):
        """ok=True with an empty tree was the whole reason for this module."""
        stub = StubProvider()                      # writes nothing
        result = self.run_node(self.node(), stub)
        self.assertFalse(result.ok, "a node that changed nothing reported ok")
        self.assertEqual(result.failure.failure_class, EFFECT_NOT_OBSERVED)

    def test_writing_the_wrong_path_does_not_satisfy_a_declaration(self):
        stub = StubProvider(on_call=self.touch("somewhere-else.txt"))
        result = self.run_node(self.node(expected=["wanted.txt"]), stub)
        self.assertFalse(result.ok)
        self.assertIn("wanted.txt", result.failure.detail)

    def test_a_well_shaped_payload_does_not_rescue_a_missing_effect(self):
        """The check runs BEFORE the schema check, and this pins that order."""
        stub = StubProvider(text='{"summary": "I refactored everything"}')
        result = self.run_node(self.node(schema="report"), stub)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, EFFECT_NOT_OBSERVED)

    def test_the_failure_is_repairable_rather_than_terminal(self):
        """The repair can say 'you changed nothing'; a resend cannot."""
        self.assertEqual(DEFAULT_POLICY[EFFECT_NOT_OBSERVED].action, REPAIR)


class ARefusedPermissionIsItsOwnFailure(WorkerCase):
    def test_a_refusal_that_stopped_the_work_is_a_permission_failure(self):
        stub = StubProvider(denials=[{"tool": "Write"}])   # and writes nothing
        result = self.run_node(self.node(), stub)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, PERMISSION_DENIED)

    def test_a_refusal_that_did_not_stop_the_work_is_not_a_failure(self):
        """Found by the A/B pilot, in nine runs, against the first version.

        claude fixed the file correctly and was refused five unrelated tools on
        the way. The first rule checked denials before the effect and failed the
        node — a verdict with no basis, which is the same defect this module
        exists to prevent, pointing the other way. The effect decides; a denial
        only refines the diagnosis when the effect is missing.
        """
        stub = StubProvider(denials=[{"tool": "Bash"}, {"tool": "WebSearch"}],
                            on_call=self.touch("made.txt"))
        result = self.run_node(self.node(), stub)
        self.assertTrue(result.ok, result.failure)
        self.assertEqual(result.meta["permission_denials"], 2,
                         "the denials were dropped instead of recorded")

    def test_a_provider_with_no_verified_write_flag_is_refused_up_front(self):
        """Running it read-only would produce a call that succeeds and does nothing."""
        node = self.node()
        node.config["provider"] = "gemini"        # write_extra is () in the catalog
        result = self.run_node(node, StubProvider())
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, PERMISSION_DENIED)
        self.assertIn("write_extra is", result.failure.detail)

    def test_a_permission_failure_is_not_retried_into_the_same_wall(self):
        self.assertEqual(DEFAULT_POLICY[PERMISSION_DENIED].action, FAIL)

    def test_denials_on_a_read_only_node_are_not_promoted_to_a_failure(self):
        """A read-only node being refused a write tool is the policy working."""
        stub = StubProvider(denials=[{"tool": "Write"}])
        result = self.run_node(self.node(side_effect=NONE), stub)
        self.assertTrue(result.ok, result.failure)


class TheEffectObserverNamesWhatItSaw(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unchanged_tree_reports_no_effect(self):
        before = effects.snapshot(self.root)
        happened, detail = effects.observed(before,
                                            effects.snapshot(self.root))
        self.assertFalse(happened)
        self.assertIn("nothing under the root changed", detail)

    def test_a_generated_directory_does_not_count_as_an_effect(self):
        """A fingerprint that moved because __pycache__ did reports a phantom."""
        before = effects.snapshot(self.root)
        os.makedirs(os.path.join(self.root, "__pycache__"), exist_ok=True)
        with open(os.path.join(self.root, "__pycache__", "x.pyc"), "wb") as fh:
            fh.write(b"\x00")
        happened, _ = effects.observed(before, effects.snapshot(self.root))
        self.assertFalse(happened)

    def test_it_names_the_paths_rather_than_counting_them(self):
        before = effects.snapshot(self.root)
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
                f.write("x")
        happened, detail = effects.observed(before, effects.snapshot(self.root))
        self.assertTrue(happened)
        self.assertIn("a.txt", detail)
        self.assertIn("b.txt", detail)

    def test_a_removal_is_an_effect(self):
        target = os.path.join(self.root, "doomed.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        before = effects.snapshot(self.root)
        os.remove(target)
        happened, detail = effects.observed(before, effects.snapshot(self.root))
        self.assertTrue(happened)
        self.assertIn("removed", detail)


class AcceptanceStillDecidesPromotion(unittest.TestCase):
    """The effect check is earlier and smaller; it does not replace the gate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_failing_acceptance_check_still_blocks_promotion(self):
        from dobby.project import initialise, loop as L
        from dobby.project.models import BLOCKED
        from dobby.project import ProjectStore
        initialise(self.data, self.root,
                   smoke=('{python} -c "import sys; sys.exit(0)"',),
                   item_specs=[{"outcome": "make the endpoint paginate",
                                "acceptance_checks":
                                    ['{python} -c "import sys; sys.exit(1)"']}])
        result = L.advance(self.data)
        self.assertEqual(result["stopped"], L.ITEM_BLOCKED, result)
        item = ProjectStore(self.data).load_project(None)["portfolio"].get(
            "W001")
        self.assertEqual(item.state, BLOCKED)
        self.assertEqual(item.evidence_refs, [],
                         "an item whose checks failed carries promoted evidence")


if __name__ == "__main__":
    unittest.main()
