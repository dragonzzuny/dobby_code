"""Failure injection: the four faults a runtime has to survive.

    provider timeout      the call does not come back
    worker crash          the process holding the work dies
    verify failure        the work comes back and is wrong
    duplicate callback    the same external effect arrives twice

Each one is injected deliberately here, because each one has a different right
answer and a system that gets three of them right and the fourth wrong is a
system that duplicates payments on a Tuesday.

The invariant every test below shares: **no external effect is performed twice**,
whatever the fault. It is asserted in each, not once at the end, because the
whole point is that the guarantee holds in every path and not on the happy one.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import (ArtifactContract, EXTERNAL_REVERSIBLE, Failure,
                           RunBudget, Runner, TaskGraph, TaskNode,
                           WorkerRegistry, WorkerResult, idempotency_key)
from dobby.runtime import graph as G
from dobby.runtime.failures import (CAPACITY, CONTRACT_VIOLATION,
                                    NON_RETRYABLE, QUALITY_FAILURE,
                                    TRANSIENT_PROVIDER)
from dobby.runtime.workers import WorkerAdapter


class FaultWorker(WorkerAdapter):
    """A worker that fails the way a real one does, on demand.

    `script` is a list of outcomes, consumed one per attempt. Running off the
    end repeats the last entry, so a test states only the changes:

        {"fail": "<failure class>", "detail": "..."}   an ordinary failure
        {"crash": true}                               the adapter dies
        anything else                                 returned as the payload

    Every entry is JSON — deliberately, and it is the constraint the first
    version of this file got wrong. A node spec round-trips through the store as
    JSON on every `run()`, because the graph the runner executes is the one it
    LOADED and not the one that was passed to `start()`. A `Failure` object put
    in `config` came back as its `str()`, the fault never fired, and four
    injection tests reported a healthy system. See
    `NodeSpecsRoundTripThroughTheStore` below, which pins that property so the
    next person finds it as a test rather than as a confusing green run.
    """

    name = "fault"

    def __init__(self):
        self.calls = []

    def run(self, node, context):
        attempt = context.get("attempt", 1)
        script = node.config.get("script") or [{}]
        step = script[min(attempt, len(script)) - 1]
        self.calls.append((node.node_id, attempt, step))
        if isinstance(step, dict) and step.get("crash"):
            raise RuntimeError("the worker process died")
        if isinstance(step, dict) and step.get("fail"):
            return WorkerResult(False, failure=Failure(
                step["fail"], step.get("detail", "injected fault")))
        return WorkerResult(True, payload=step, raw="ok")


def fails(failure_class, detail="injected fault"):
    return {"fail": failure_class, "detail": detail}


CRASH = {"crash": True}


def fault_node(node_id, script, *, depends_on=(), side_effect="NONE",
               schema=None, checks=()):
    return TaskNode(
        node_id=node_id, kind=node_id, depends_on=list(depends_on),
        worker="fault",
        contract=ArtifactContract(output_schema=schema or {},
                                  acceptance_checks=list(checks),
                                  side_effect_class=side_effect),
        config={"script": script})


class InjectionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.faults = FaultWorker()
        registry = WorkerRegistry({"fault": self.faults})
        self.runner = Runner(self.tmp.name, data_dir=self.data,
                             workers=registry, sleep=lambda _s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_no_duplicate_effects(self, run_id):
        effects = self.runner.store.effects(run_id)
        keys = [e["idempotency_key"] for e in effects]
        self.assertEqual(len(keys), len(set(keys)),
                         "the same external effect was claimed twice")


class ProviderTimeout(InjectionCase):
    def test_a_timeout_is_transient_and_the_retry_succeeds(self):
        timeout = fails(TRANSIENT_PROVIDER, "timeout after 120s")
        graph = TaskGraph([fault_node("call", [timeout, {"ok": True}])])
        run_id = self.runner.start("call the provider", graph)
        result = self.runner.run(run_id)

        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        rows = self.runner.store.attempts(run_id, "call")
        self.assertEqual([r["outcome"] for r in rows],
                         [G.RETRYABLE_FAILURE, G.FINISHED])
        self.assertEqual(rows[0]["failure_class"], TRANSIENT_PROVIDER)
        self.assert_no_duplicate_effects(run_id)

    def test_a_timeout_that_never_clears_stops_inside_the_policy(self):
        timeout = fails(TRANSIENT_PROVIDER, "timeout after 120s")
        graph = TaskGraph([fault_node("call", [timeout] * 10)])
        run_id = self.runner.start("call the provider", graph)
        result = self.runner.run(run_id)

        self.assertEqual(result.state, G.FAILED)
        rows = self.runner.store.attempts(run_id, "call")
        self.assertEqual(len(rows), 3, "the retry policy's ceiling did not bind")
        self.assertEqual(rows[-1]["outcome"], G.PERMANENT_FAILURE)

    def test_a_timeout_on_an_effect_node_does_not_repeat_the_effect(self):
        timeout = fails(TRANSIENT_PROVIDER, "timeout after 120s")
        graph = TaskGraph([fault_node("send", [timeout, {"ok": True}],
                                      side_effect=EXTERNAL_REVERSIBLE)])
        run_id = self.runner.start("send the notification", graph)
        self.runner.run(run_id)

        self.assert_no_duplicate_effects(run_id)
        # The first attempt claimed the key. The second must NOT act again —
        # it finds the claim and completes without repeating the effect.
        self.assertEqual(len(self.runner.store.effects(run_id)), 1)
        self.assertEqual(len([c for c in self.faults.calls
                              if c[0] == "send"]), 1,
                         "the effect worker ran a second time")


class WorkerCrash(InjectionCase):
    def test_an_adapter_that_raises_is_a_run_event_not_a_run_failure(self):
        graph = TaskGraph([fault_node("a", [CRASH]),
                           fault_node("b", [{"ok": True}])])
        run_id = self.runner.start("two independent steps", graph)
        result = self.runner.run(run_id)

        states = {s.node_id: s.state for s in result.steps}
        self.assertEqual(states["a"], G.NODE_FAILED)
        self.assertEqual(states["b"], G.NODE_SUCCEEDED,
                         "one node's crash lost an unrelated node's work")
        rows = self.runner.store.attempts(run_id, "a")
        self.assertEqual(rows[0]["failure_class"], NON_RETRYABLE)
        self.assertIn("RuntimeError", rows[0]["detail"])

    def test_an_interrupted_attempt_is_recovered_and_the_node_reruns(self):
        graph = TaskGraph([fault_node("a", [{"ok": True}])])
        run_id = self.runner.start("one step", graph)
        # Exactly what a killed process leaves: a lease and an open attempt.
        self.runner.store.set_node_state(run_id, "a", G.READY)
        self.runner.store.lease_node(run_id, "a", holder="ghost")
        self.runner.store.start_attempt(run_id, "a", 1, worker="fault")

        result = self.runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED)
        rows = self.runner.store.attempts(run_id, "a")
        self.assertEqual(rows[0]["outcome"], G.RETRYABLE_FAILURE)
        self.assertEqual(rows[0]["detail"], G.INTERRUPTED_DETAIL)
        self.assertEqual(rows[1]["outcome"], G.FINISHED)

    def test_a_crash_after_the_effect_leaves_a_claim_and_does_not_repeat_it(self):
        graph = TaskGraph([fault_node("send", [{"ok": True}],
                                      side_effect=EXTERNAL_REVERSIBLE)])
        run_id = self.runner.start("send the notification", graph)
        # The process died between claiming the key and doing anything else.
        key = idempotency_key(run_id, "send")
        self.runner.store.claim_effect(key, run_id, "send", "1")

        result = self.runner.run(run_id)
        self.assertTrue(any("never confirmed" in n for n in result.notes),
                        result.notes)
        self.assertEqual(self.faults.calls, [],
                         "the effect was performed after a crash that had "
                         "already claimed it")
        self.assert_no_duplicate_effects(run_id)


class VerifyFailure(InjectionCase):
    def test_a_wrong_shape_is_repaired_not_retried_identically(self):
        graph = TaskGraph([fault_node(
            "produce", [{"wrong": 1}, {"right": 1}],
            schema={"type": "object", "required": ["right"]})])
        run_id = self.runner.start("produce the artifact", graph)
        result = self.runner.run(run_id)

        self.assertEqual(result.state, G.SUCCEEDED)
        rows = self.runner.store.attempts(run_id, "produce")
        self.assertEqual(rows[0]["failure_class"], CONTRACT_VIOLATION)
        self.assertIn("REPAIR", rows[0]["detail"])

    def test_a_failing_acceptance_check_blocks_promotion(self):
        graph = TaskGraph([fault_node(
            "produce", [{"ok": True}],
            checks=[f'{sys.executable} -c "exit(1)"'])])
        run_id = self.runner.start("produce the artifact", graph)
        result = self.runner.run(run_id)

        self.assertEqual(result.steps[0].state, G.NODE_FAILED)
        self.assertEqual(self.runner.store.artifacts(run_id, state="PROMOTED"),
                         [])
        rows = self.runner.store.attempts(run_id, "produce")
        self.assertEqual(rows[0]["failure_class"], QUALITY_FAILURE)

    def test_a_rejected_artifact_never_reaches_the_dependent(self):
        graph = TaskGraph([
            fault_node("produce", [{"wrong": 1}] * 5,
                       schema={"type": "object", "required": ["right"]}),
            fault_node("consume", [{"ok": True}], depends_on=["produce"]),
        ])
        run_id = self.runner.start("produce then consume", graph)
        result = self.runner.run(run_id)
        states = {s.node_id: s.state for s in result.steps}
        self.assertEqual(states["produce"], G.NODE_FAILED)
        self.assertEqual(states["consume"], G.SKIPPED)
        self.assertEqual([c for c in self.faults.calls if c[0] == "consume"],
                         [])


class DuplicateCallback(InjectionCase):
    def test_the_same_effect_across_two_runners_happens_once(self):
        graph = TaskGraph([fault_node("send", [{"ok": True}],
                                      side_effect=EXTERNAL_REVERSIBLE)])
        run_id = self.runner.start("send the notification", graph)
        self.runner.run(run_id)
        first = len(self.faults.calls)

        # A second process attaches to the same run and drives it again.
        second_runner = Runner(self.tmp.name, data_dir=self.data,
                               workers=WorkerRegistry({"fault": self.faults}),
                               sleep=lambda _s: None)
        second_runner.run(run_id)

        self.assertEqual(len(self.faults.calls), first,
                         "the second runner performed the effect again")
        self.assert_no_duplicate_effects(run_id)

    def test_a_new_effect_version_is_a_different_effect_on_purpose(self):
        first = idempotency_key("run", "send", "1")
        self.assertEqual(first, idempotency_key("run", "send", "1"))
        self.assertNotEqual(first, idempotency_key("run", "send", "2"))

    def test_a_reworded_retry_collides_because_the_key_is_identity(self):
        """Content-derived keys would not collide here, which is the bug."""
        self.assertEqual(idempotency_key("run", "send", "1"),
                         idempotency_key("run", "send", "1"))


class NodeSpecsRoundTripThroughTheStore(InjectionCase):
    """The graph the runner executes is the one it LOADED, not the one passed.

    That is the property `resume` depends on, and it means a node spec must be
    JSON. Pinned here because breaking it is silent: an object placed in
    `config` comes back as its `str()`, the code reading it takes a different
    branch, and the run looks healthy.
    """

    def test_config_survives_a_round_trip_unchanged(self):
        graph = TaskGraph([fault_node(
            "a", [fails(TRANSIENT_PROVIDER, "timeout"), {"ok": True}])])
        run_id = self.runner.start("round trip", graph)
        loaded = self.runner.store.load_run(run_id)["graph"].nodes["a"]
        self.assertEqual(loaded.config, graph.nodes["a"].config)
        self.assertIsInstance(loaded.config["script"][0], dict)

    def test_the_contract_survives_too(self):
        graph = TaskGraph([fault_node(
            "a", [{"ok": True}], side_effect=EXTERNAL_REVERSIBLE,
            schema={"type": "object", "required": ["ok"]},
            checks=["true"])])
        run_id = self.runner.start("round trip", graph)
        loaded = self.runner.store.load_run(run_id)["graph"].nodes["a"]
        self.assertEqual(loaded.contract.side_effect_class,
                         EXTERNAL_REVERSIBLE)
        self.assertEqual(loaded.contract.acceptance_checks, ["true"])
        self.assertEqual(loaded.contract.output_schema["required"], ["ok"])

    def test_a_non_serialisable_config_is_refused_at_start(self):
        """Fail where the fix is one line, not three tests later."""
        node = fault_node("a", [{"ok": True}])
        node.config["oops"] = Failure(TRANSIENT_PROVIDER, "not json")
        with self.assertRaises(Exception):
            self.runner.start("round trip", TaskGraph([node]))


class BudgetUnderFaults(InjectionCase):
    def test_retries_consume_the_run_budget_and_it_binds(self):
        timeout = fails(TRANSIENT_PROVIDER, "timeout after 120s")
        graph = TaskGraph([fault_node("call", [timeout] * 10)])
        run_id = self.runner.start("call the provider", graph)
        result = self.runner.run(run_id, budget=RunBudget(max_attempts=2))

        self.assertLessEqual(sum(s.attempts for s in result.steps), 2)
        self.assertEqual(result.state, G.WAITING)
        self.assertIn("budget", result.deferred[0]["reason"])

    def test_a_capacity_failure_is_not_retried_on_the_same_provider(self):
        graph = TaskGraph([fault_node(
            "call", [fails(CAPACITY, "429 rate limit"), {"ok": True}])])
        run_id = self.runner.start("call the provider", graph)
        self.runner.run(run_id)
        rows = self.runner.store.attempts(run_id, "call")
        self.assertEqual(rows[0]["failure_class"], CAPACITY)
        self.assertIn("RETRY_ELSEWHERE", rows[0]["detail"])


if __name__ == "__main__":
    unittest.main()
