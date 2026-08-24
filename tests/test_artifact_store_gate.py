"""The store is the OTHER door into artifact state, and it used to be unlocked.

`Artifact.transition` refuses an illegal move on an in-memory object. But
`Runner._promoted_inputs` does not read that object — it reads the artifacts
TABLE, filtered on `state=PROMOTED`. So the rule that decides what a later node
may consume was being enforced in the one place that does not decide it, and
`RunStore.put_artifact` wrote whatever `artifact.state` it was handed.

Demonstrated during an audit of this repository, on one artifact id:

    put_artifact(state=PROMOTED)   ->  ['PROMOTED']
    put_artifact(state=REJECTED)   ->  ['REJECTED']

while `_ARTIFACT_TRANSITIONS[PROMOTED]` is the empty set. The runner never did
that, so nothing was broken. It was a claim that was not true: "an unverified
artifact cannot become an input" held because of what the runner happened to
call, not because anything refused.

These tests are the refusal. They exercise the store directly, which is exactly
the path the runner does not take.
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import (PROMOTED, PROPOSED,  # noqa: E402
                                     REJECTED, VERIFIED, Artifact,
                                     ArtifactContract, ContractError,
                                     PayloadTampered, check_artifact_write,
                                     digest, verify_payload)
from dobby.runtime.store import RunStore  # noqa: E402


def artifact(state=PROPOSED, payload=None, artifact_id="n-1", run_id=""):
    return Artifact(artifact_id=artifact_id, run_id=run_id, node_id="n",
                    kind="k", payload=payload if payload is not None
                    else {"x": 1}, state=state)


class TheRuleAlone(unittest.TestCase):
    """`check_artifact_write` is pure, so the rule is testable without a store."""

    def test_an_artifact_enters_only_as_proposed(self):
        check_artifact_write(None, PROPOSED)
        for state in (VERIFIED, PROMOTED, REJECTED):
            with self.assertRaises(ContractError, msg=state):
                check_artifact_write(None, state)

    def test_the_table_is_walked_and_not_skipped(self):
        check_artifact_write(PROPOSED, VERIFIED)
        check_artifact_write(VERIFIED, PROMOTED)
        check_artifact_write(PROPOSED, REJECTED)
        with self.assertRaises(ContractError):
            check_artifact_write(PROPOSED, PROMOTED)

    def test_a_terminal_state_is_terminal(self):
        for terminal in (PROMOTED, REJECTED):
            for target in (PROPOSED, VERIFIED, PROMOTED, REJECTED):
                if target == terminal:
                    continue
                with self.assertRaises(ContractError, msg=f"{terminal}->{target}"):
                    check_artifact_write(terminal, target)

    def test_a_rewrite_may_not_swap_the_payload_underneath_the_state(self):
        """The same hole one level down: label holds, contents change."""
        check_artifact_write(PROMOTED, PROMOTED,
                             previous_digest="aa", digest_="aa")
        with self.assertRaises(ContractError):
            check_artifact_write(PROMOTED, PROMOTED,
                                 previous_digest="aa", digest_="bb")

    def test_an_unknown_state_is_refused_before_anything_else(self):
        with self.assertRaises(ContractError):
            check_artifact_write(None, "APPROVED_BY_VIBES")


class TheStoreDoor(unittest.TestCase):
    """The same rule, through the API a caller would actually reach for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore(self.tmp.name)
        graph = G.TaskGraph([G.TaskNode(
            node_id="n", kind="k", worker="static", instruction="i",
            contract=ArtifactContract(output_schema=dict(type="object")))])
        self.run_id = self.store.create_run("t", graph)

    def states(self):
        return [row["state"] for row in self.store.artifacts(self.run_id)]

    def test_the_store_api_cannot_insert_an_unverified_artifact_as_promoted(self):
        """The headline. This is the bypass the audit found.

        Nothing here runs a verifier, so nothing has graded this payload. It
        must not be possible to record it in the state that makes it an input.
        """
        with self.assertRaises(ContractError) as caught:
            self.store.put_artifact(artifact(PROMOTED, run_id=self.run_id))
        self.assertIn("enters the store as PROPOSED", str(caught.exception))
        self.assertEqual(self.states(), [],
                         "a refused write must leave no row behind")

    def test_nor_as_verified(self):
        with self.assertRaises(ContractError):
            self.store.put_artifact(artifact(VERIFIED, run_id=self.run_id))
        self.assertEqual(self.states(), [])

    def test_a_promoted_artifact_cannot_be_walked_back(self):
        for state in (PROPOSED, VERIFIED, PROMOTED):
            self.store.put_artifact(artifact(state, run_id=self.run_id))
        self.assertEqual(self.states(), [PROMOTED])
        with self.assertRaises(ContractError):
            self.store.put_artifact(artifact(REJECTED, run_id=self.run_id))
        self.assertEqual(self.states(), [PROMOTED])

    def test_the_payload_cannot_be_swapped_under_a_promoted_label(self):
        for state in (PROPOSED, VERIFIED, PROMOTED):
            self.store.put_artifact(artifact(state, run_id=self.run_id))
        with self.assertRaises(ContractError) as caught:
            self.store.put_artifact(artifact(PROMOTED, payload={"x": 999},
                                             run_id=self.run_id))
        self.assertIn("different payload", str(caught.exception))

    def test_the_legitimate_walk_is_still_allowed(self):
        """The gate must not be so strict that the runner cannot pass it."""
        self.store.put_artifact(artifact(PROPOSED, run_id=self.run_id))
        self.store.put_artifact(artifact(VERIFIED, run_id=self.run_id))
        self.store.put_artifact(artifact(PROMOTED, run_id=self.run_id),
                                path="somewhere.json")
        self.assertEqual(self.states(), [PROMOTED])
        rows = self.store.artifacts(self.run_id, state=PROMOTED)
        self.assertEqual(rows[0]["path"], "somewhere.json")

    def test_there_is_no_force_parameter(self):
        """A gate with an override is the override."""
        import inspect

        params = inspect.signature(RunStore.put_artifact).parameters
        self.assertNotIn("force", params)
        self.assertEqual(sorted(params), ["artifact", "path", "self"])


class TheRunnerStillWalksIt(unittest.TestCase):
    """The enforcement is only worth having if the real path still passes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")

    def runner(self):
        from dobby.runtime.runner import Runner

        return Runner(repo=self.tmp.name, data_dir=self.data)

    @staticmethod
    def lifecycles(runner, run_id) -> dict:
        """Every artifact id mapped to the states it was recorded in, in order."""
        seen: dict = {}
        for event in runner.store.events(run_id):
            if event["kind"] != "artifact":
                continue
            seen.setdefault(event["payload"]["artifact_id"], []).append(
                event["payload"]["state"])
        return seen

    def static_graph(self, payload, schema=None):
        return G.TaskGraph([G.TaskNode(
            node_id="produce", kind="k", worker="static",
            instruction="i", config={"payload": payload},
            contract=ArtifactContract(
                output_schema=schema or dict(type="object")))])

    def test_a_real_run_records_every_state_it_passed_through(self):
        """PROPOSED and VERIFIED used to never reach the store at all.

        Both transitions happened on one line and only the destination was
        written, so a promotion had no verified state behind it that anything
        could read. The log can now be asked what happened.
        """
        runner = self.runner()
        run_id = runner.start("t", self.static_graph({"value": 1}))
        runner.run(run_id)
        self.assertEqual(self.lifecycles(runner, run_id),
                         {"produce-1": [PROPOSED, VERIFIED, PROMOTED]})
        self.assertEqual(
            [r["state"] for r in runner.store.artifacts(run_id)], [PROMOTED])

    def test_a_rejected_run_records_proposed_then_rejected(self):
        runner = self.runner()
        run_id = runner.start("t", self.static_graph(
            {"wrong": "shape"},
            schema={"type": "object", "required": ["right"]}))
        runner.run(run_id)
        # A schema failure is CONTRACT_VIOLATION -> REPAIR, max_attempts 2, and
        # each attempt gets its own artifact id. So the assertion is per
        # ARTIFACT: every one of them walked PROPOSED -> REJECTED, and none
        # reached a state that would make it an input.
        lifecycles = self.lifecycles(runner, run_id)
        self.assertTrue(lifecycles)
        for artifact_id, states in lifecycles.items():
            self.assertEqual(states, [PROPOSED, REJECTED], artifact_id)
        self.assertEqual(runner.store.artifacts(run_id, state=PROMOTED), [])


if __name__ == "__main__":
    unittest.main()


class ThePayloadUnderTheState(unittest.TestCase):
    """The same door one layer down: the state is guarded, the CONTENT was not.

    `check_artifact_write` stops PROMOTED being forged in the table. It says
    nothing about the FILE the row points at, and `Runner._read_payload` loaded
    that file and handed its contents to the next node without ever comparing
    them to the digest recorded when the gate passed. Demonstrated on a real
    run before the fix: the row said PROMOTED with the digest of
    `{"value": 41}` while the consumer received
    `{"value": 999999, "injected": ...}`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")

    def runner(self):
        from dobby.runtime.runner import Runner

        return Runner(repo=self.tmp.name, data_dir=self.data)

    @staticmethod
    def pair_graph():
        return G.TaskGraph([
            G.TaskNode(node_id="produce", kind="k", worker="static",
                       instruction="i", config={"payload": {"value": 41}},
                       contract=ArtifactContract(output_schema=dict(type="object"))),
            G.TaskNode(node_id="consume", kind="k", worker="static",
                       instruction="i", depends_on=["produce"],
                       config={"payload": {"ok": True}},
                       contract=ArtifactContract(output_schema=dict(type="object")))])

    def tamper(self, path, payload):
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        doc["payload"] = payload
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)

    def test_the_rule_alone(self):
        good = digest({"value": 41})
        verify_payload({"value": 41}, good)
        with self.assertRaises(PayloadTampered):
            verify_payload({"value": 999999}, good, artifact_id="produce-1")

    def test_an_edited_artifact_file_does_not_reach_the_next_node(self):
        """The headline for this layer."""
        runner = self.runner()
        graph = self.pair_graph()
        run_id = runner.start("t", graph)
        runner.run(run_id, max_steps=1)          # produce only
        row = runner.store.artifacts(run_id, node_id="produce",
                                     state=PROMOTED)[0]
        self.tamper(row["path"], {"value": 999999, "injected": "never graded"})

        result = runner.run(run_id)
        states = {s.node_id: s.state for s in result.steps}
        self.assertEqual(states["produce"], G.NODE_SUCCEEDED)
        self.assertEqual(states["consume"], G.NODE_FAILED)
        self.assertEqual(result.state, G.FAILED)

    def test_tampering_is_a_classified_failure_and_not_a_crash(self):
        """A run reports; it does not take the process down with it."""
        runner = self.runner()
        graph = self.pair_graph()
        run_id = runner.start("t", graph)
        runner.run(run_id, max_steps=1)
        row = runner.store.artifacts(run_id, node_id="produce",
                                     state=PROMOTED)[0]
        self.tamper(row["path"], {"value": 999999})
        runner.run(run_id)

        last = runner.store.attempts(run_id, "consume")[-1]
        self.assertEqual(last["failure_class"], "NON_RETRYABLE")
        self.assertIn("does not match the digest", last["detail"])

    def test_a_retry_is_not_attempted_because_it_could_not_help(self):
        """Nothing here can know which of the two versions was meant."""
        runner = self.runner()
        graph = self.pair_graph()
        run_id = runner.start("t", graph)
        runner.run(run_id, max_steps=1)
        row = runner.store.artifacts(run_id, node_id="produce",
                                     state=PROMOTED)[0]
        self.tamper(row["path"], {"value": 999999})
        runner.run(run_id)
        self.assertEqual(len(runner.store.attempts(run_id, "consume")), 1)

    def test_an_untouched_artifact_still_reaches_the_next_node(self):
        """The check must not break the path it guards."""
        runner = self.runner()
        graph = self.pair_graph()
        run_id = runner.start("t", graph)
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(
            runner._promoted_inputs(run_id, graph, graph.nodes["consume"]),
            {"produce": {"value": 41}})
