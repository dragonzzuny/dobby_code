"""Tests for provider placement, the circuit breaker, and parallel nodes.

Placement is measured against a FAKE fleet. Asserting against whatever agent
CLIs happen to be installed on the machine running the suite would make these
tests pass or fail for reasons that have nothing to do with the policy — and
CI has none installed at all, which would silently reduce every one of them to
a skip.
"""

import os
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers import detect
from dobby.runtime import (ArtifactContract, ConcurrencyLimiter,
                           ProviderPlacement, RunStore, Runner, TaskGraph,
                           TaskNode, Weights)
from dobby.runtime import graph as G
from dobby.runtime.placement import (CLOSED, COOLDOWN_S, FAILURE_THRESHOLD,
                                     HALF_OPEN, OPEN, Breaker, UNKNOWN_PRIOR)
from dobby.runtime.failures import Failure
from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,
                                   WorkerResult)


def provider_node(node_id="work", kind="execute", **config):
    return TaskNode(node_id=node_id, kind=kind, worker="provider",
                    contract=ArtifactContract(output_schema=dict(type="object")), config=config)


class TheCircuitBreaker(unittest.TestCase):
    def test_it_stays_closed_below_the_threshold(self):
        breaker = Breaker("claude")
        for _ in range(FAILURE_THRESHOLD - 1):
            breaker.record(False)
        self.assertEqual(breaker.state, CLOSED)
        self.assertTrue(breaker.allows())

    def test_it_opens_on_consecutive_failures_and_refuses(self):
        breaker = Breaker("claude")
        for _ in range(FAILURE_THRESHOLD):
            breaker.record(False, now=100.0)
        self.assertEqual(breaker.state, OPEN)
        self.assertFalse(breaker.allows(now=100.0))

    def test_one_success_resets_the_run(self):
        breaker = Breaker("claude")
        breaker.record(False)
        breaker.record(False)
        breaker.record(True)
        breaker.record(False)
        self.assertEqual(breaker.state, CLOSED)
        self.assertEqual(breaker.consecutive_failures, 1)

    def test_it_half_opens_after_the_cooldown_and_lets_one_probe_through(self):
        breaker = Breaker("claude")
        for _ in range(FAILURE_THRESHOLD):
            breaker.record(False, now=0.0)
        self.assertFalse(breaker.allows(now=COOLDOWN_S - 1))
        self.assertTrue(breaker.allows(now=COOLDOWN_S + 1))
        self.assertEqual(breaker.state, HALF_OPEN)


class TheConcurrencyLimiter(unittest.TestCase):
    def test_the_per_provider_cap_binds(self):
        limiter = ConcurrencyLimiter(total=8, per_provider=1)
        self.assertTrue(limiter.acquire("claude"))
        self.assertFalse(limiter.acquire("claude", timeout=0.05))
        limiter.release("claude")
        self.assertTrue(limiter.acquire("claude", timeout=0.05))
        limiter.release("claude")

    def test_the_global_cap_binds_across_providers(self):
        limiter = ConcurrencyLimiter(total=1, per_provider=8)
        self.assertTrue(limiter.acquire("claude"))
        self.assertFalse(limiter.acquire("codex", timeout=0.05))
        limiter.release("claude")

    def test_a_refused_acquire_holds_no_slot(self):
        """Both-or-neither: the global slot must not be kept on a failure."""
        limiter = ConcurrencyLimiter(total=2, per_provider=1)
        self.assertTrue(limiter.acquire("claude"))
        self.assertFalse(limiter.acquire("claude", timeout=0.05))
        # If the failed acquire had kept the global slot, this would fail.
        self.assertTrue(limiter.acquire("codex", timeout=0.05))
        limiter.release("codex")
        limiter.release("claude")

    def test_it_serializes_rather_than_losing_work(self):
        limiter = ConcurrencyLimiter(total=1, per_provider=1)
        order = []
        lock = threading.Lock()

        def work(i):
            self.assertTrue(limiter.acquire("claude", timeout=10))
            with lock:
                order.append(i)
            time.sleep(0.01)
            limiter.release("claude")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(order), list(range(5)))


class FleetFixture(unittest.TestCase):
    """Placement against a fleet this test controls."""

    FLEET = ["alpha", "beta", "gamma"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(os.path.join(self.tmp.name, ".dobby"))
        self._real_available = detect.available_ids
        detect.available_ids = lambda allow_network=False: list(self.FLEET)

    def tearDown(self):
        detect.available_ids = self._real_available
        self.tmp.cleanup()

    def placement(self, card=None, **kwargs):
        return ProviderPlacement(self.store, scorecard=card or {}, **kwargs)


class Candidates(FleetFixture):
    def test_the_avoid_list_removes_the_provider_that_just_failed(self):
        chosen = self.placement().candidates(provider_node(), avoid={"alpha"})
        self.assertNotIn("alpha", chosen)
        self.assertEqual(set(chosen), {"beta", "gamma"})

    def test_declared_preferences_come_first_and_the_rest_stay_behind(self):
        node = provider_node(fallback_providers=["gamma"])
        self.assertEqual(self.placement().candidates(node)[0], "gamma")
        self.assertEqual(set(self.placement().candidates(node)), set(self.FLEET))


class Scoring(FleetFixture):
    def test_with_nothing_measured_it_says_so_rather_than_scoring_noise(self):
        placement = self.placement()
        decision = placement.choose(provider_node())
        self.assertIn(decision.provider, self.FLEET)
        self.assertTrue(decision.provisional)
        self.assertIn("nothing measured", decision.reason)

    def test_a_measured_winner_beats_a_measured_loser(self):
        card = {
            "alpha/execute": {"provider": "alpha", "node_kind": "execute",
                              "attempts": 20, "success_rate": 0.95,
                              "p95_s": 10.0, "consecutive_failures": 0,
                              "failure_classes": {}},
            "beta/execute": {"provider": "beta", "node_kind": "execute",
                             "attempts": 20, "success_rate": 0.20,
                             "p95_s": 10.0, "consecutive_failures": 0,
                             "failure_classes": {}},
            "gamma/execute": {"provider": "gamma", "node_kind": "execute",
                              "attempts": 20, "success_rate": 0.50,
                              "p95_s": 10.0, "consecutive_failures": 0,
                              "failure_classes": {}},
        }
        decision = self.placement(card=card).choose(provider_node())
        self.assertEqual(decision.provider, "alpha")
        self.assertFalse(decision.provisional)

    def test_an_untried_provider_wins_when_every_measured_one_is_bad(self):
        """Trying the untried one IS the exploration; there is no bandit here."""
        card = {
            "alpha/execute": {"provider": "alpha", "node_kind": "execute",
                              "attempts": 20, "success_rate": 0.10,
                              "p95_s": 10.0, "consecutive_failures": 0,
                              "failure_classes": {}},
        }
        decision = self.placement(card=card).choose(provider_node())
        self.assertIn(decision.provider, ("beta", "gamma"))
        self.assertIn("untried", decision.reason)

    def test_a_thin_record_is_marked_provisional(self):
        card = {"alpha/execute": {"provider": "alpha", "node_kind": "execute",
                                  "attempts": 1, "success_rate": 1.0,
                                  "p95_s": 1.0, "consecutive_failures": 0,
                                  "failure_classes": {}}}
        decision = self.placement(card=card).choose(
            provider_node(fallback_providers=["alpha"]))
        self.assertTrue(decision.provisional)

    def test_latency_only_penalises_against_a_measured_worst(self):
        placement = self.placement()
        _, terms = placement.score("alpha", "execute", worst_p95=None)
        self.assertEqual(terms["latency"], 0.0)

    def test_the_unknown_prior_is_optimistic_on_purpose(self):
        placement = self.placement()
        _, terms = placement.score("alpha", "execute")
        self.assertEqual(terms["quality"], UNKNOWN_PRIOR)
        self.assertFalse(terms["quality_measured"])

    def test_weights_are_honoured(self):
        card = {"alpha/execute": {"provider": "alpha", "node_kind": "execute",
                                  "attempts": 20, "success_rate": 1.0,
                                  "p95_s": 100.0, "consecutive_failures": 0,
                                  "failure_classes": {}},
                "beta/execute": {"provider": "beta", "node_kind": "execute",
                                 "attempts": 20, "success_rate": 0.9,
                                 "p95_s": 1.0, "consecutive_failures": 0,
                                 "failure_classes": {}}}
        patient = self.placement(card=card, weights=Weights(latency=0.0))
        impatient = self.placement(card=card, weights=Weights(latency=10.0))
        node = provider_node(fallback_providers=["alpha", "beta"])
        self.assertEqual(patient.choose(node).provider, "alpha")
        self.assertEqual(impatient.choose(node).provider, "beta")


class WhenNothingCanRun(FleetFixture):
    def test_it_returns_no_provider_and_names_the_reason(self):
        placement = self.placement()
        for pid in self.FLEET:
            for _ in range(FAILURE_THRESHOLD):
                placement.record_outcome(pid, False)
        decision = placement.choose(provider_node())
        self.assertIsNone(decision.provider)
        self.assertIn("circuit breaker", decision.reason)

    def test_an_avoid_list_that_empties_the_fleet_says_that_instead(self):
        decision = self.placement().choose(provider_node(),
                                           avoid=set(self.FLEET))
        self.assertIsNone(decision.provider)
        self.assertIn("avoid list", decision.reason)


class Hedging(FleetFixture):
    CARD = {"alpha/execute": {"provider": "alpha", "node_kind": "execute",
                              "attempts": 20, "success_rate": 0.9,
                              "p95_s": 5.0, "consecutive_failures": 0,
                              "failure_classes": {}},
            "beta/execute": {"provider": "beta", "node_kind": "execute",
                             "attempts": 20, "success_rate": 0.8,
                             "p95_s": 5.0, "consecutive_failures": 0,
                             "failure_classes": {}}}

    def test_a_node_with_side_effects_is_never_hedged(self):
        node = provider_node(hedge=True)
        node.contract = ArtifactContract(side_effect_class="EXTERNAL_REVERSIBLE")
        decision = self.placement(card=self.CARD).choose(node)
        self.assertIsNone(decision.hedge_with)

    def test_a_read_only_node_hedges_only_when_asked(self):
        silent = self.placement(card=self.CARD).choose(provider_node())
        self.assertIsNone(silent.hedge_with)
        asked = self.placement(card=self.CARD).choose(
            provider_node(hedge=True, fallback_providers=["alpha", "beta"]))
        self.assertEqual(asked.hedge_with, "beta")

    def test_it_will_not_hedge_without_a_measured_p95_to_be_slow_against(self):
        decision = self.placement().choose(provider_node(hedge=True))
        self.assertIsNone(decision.hedge_with)


class ParallelNodes(unittest.TestCase):
    """A diamond: two independent middles that may run at the same time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def diamond(self):
        def node(node_id, depends_on=()):
            return TaskNode(node_id=node_id, kind=node_id,
                            depends_on=list(depends_on), worker="static",
                            contract=ArtifactContract(output_schema=dict(type="object")),
                            config={"payload": {"id": node_id}})
        return TaskGraph([node("top"), node("left", ["top"]),
                          node("right", ["top"]), node("join",
                                                       ["left", "right"])])

    def test_a_diamond_completes_with_every_node_run_once(self):
        runner = Runner(self.tmp.name, data_dir=self.data, max_parallel=2,
                        sleep=lambda _s: None)
        run_id = runner.start("fan out and join", self.diamond())
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        for node_id in ("top", "left", "right", "join"):
            self.assertEqual(len(runner.store.attempts(run_id, node_id)), 1,
                             f"{node_id} ran more than once")

    def test_the_two_independent_nodes_are_dispatched_together(self):
        """Both must be IN FLIGHT at once, proved by a barrier rather than by
        two timestamps happening to overlap.

        The timestamp version measured a coincidence. These nodes finish in
        microseconds, so whether their spans overlap depends on how quickly the
        pool hands the second one to a thread -- and on a loaded two-core CI
        runner it does not. Measured there: an overlap of -25.5ms, reported as
        "parallelism is off" on a runtime whose parallelism was fine. Red CI
        for a property nobody had broken is worse than no test.

        A two-party barrier cannot be satisfied by sequential execution at any
        speed: the first arrival blocks until the second arrives. If only one
        node is ever in flight, this times out and fails for the reason the old
        assertion was reaching for.
        """
        barrier = threading.Barrier(2, timeout=30)
        arrived = []

        class Meeting(WorkerAdapter):
            name = "static"

            def run(self, node, context):
                if node.node_id in ("left", "right"):
                    arrived.append(node.node_id)
                    barrier.wait()
                return WorkerResult(True, payload={"id": node.node_id})

        runner = Runner(self.tmp.name, data_dir=self.data, max_parallel=2,
                        workers=WorkerRegistry({"static": Meeting()}),
                        sleep=lambda _s: None)
        run_id = runner.start("fan out and join", self.diamond())
        result = runner.run(run_id)

        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(sorted(arrived), ["left", "right"],
                         "both independent nodes have to reach the barrier")

    def test_one_worker_at_a_time_cannot_pass_that_barrier(self):
        """The control. Same graph, `max_parallel=1`, and the barrier is never
        met -- which is what makes the test above evidence rather than a
        formality."""
        barrier = threading.Barrier(2, timeout=3)
        timed_out = []

        class Meeting(WorkerAdapter):
            name = "static"

            def run(self, node, context):
                if node.node_id in ("left", "right"):
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        timed_out.append(node.node_id)
                        return WorkerResult(False, failure=Failure(
                            "NON_RETRYABLE", "the barrier was never met"))
                return WorkerResult(True, payload={"id": node.node_id})

        runner = Runner(self.tmp.name, data_dir=self.data, max_parallel=1,
                        workers=WorkerRegistry({"static": Meeting()}),
                        sleep=lambda _s: None)
        runner.run(runner.start("fan out and join", self.diamond()))
        self.assertTrue(timed_out,
                        "sequential execution met a two-party barrier")

    def test_sequential_is_still_the_default(self):
        runner = Runner(self.tmp.name, data_dir=self.data,
                        sleep=lambda _s: None)
        self.assertEqual(runner.max_parallel, 1)
        run_id = runner.start("fan out and join", self.diamond())
        self.assertEqual(runner.run(run_id).state, G.SUCCEEDED)

    def test_a_budget_ceiling_still_binds_under_parallelism(self):
        from dobby.runtime import RunBudget
        runner = Runner(self.tmp.name, data_dir=self.data, max_parallel=4,
                        sleep=lambda _s: None)
        run_id = runner.start("fan out and join", self.diamond())
        result = runner.run(run_id, budget=RunBudget(max_attempts=2))
        self.assertLessEqual(sum(s.attempts for s in result.steps), 2)
        self.assertEqual(result.state, G.WAITING)


if __name__ == "__main__":
    unittest.main()
