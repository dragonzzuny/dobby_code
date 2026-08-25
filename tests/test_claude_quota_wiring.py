"""The Claude quota ledger, connected to something.

`runtime/claude_quota.py` was 392 lines that nothing imported. It was not
half-built — it has reservations taken under a lock, a refused/overrun
distinction it refuses to merge, and an `unmeasured` state for a call whose
envelope did not parse. It had simply never been wired.

The reason it had not been was measurable rather than a matter of taste:
`settle` needs a usage envelope, and `ProviderWorker` called `run_provider`
WITHOUT `collect_usage`. No usage ever reached the runner. A ledger switched on
before that was fixed would have recorded every single call as unmeasured and,
with `fail_closed_on_unmeasured_usage` at its default, closed the lane on the
first one. Wiring the ledger without plumbing the tokens would have produced a
cap that looked enforced and refused everything.

Two properties these tests exist to hold:

- OFF by default. A ledger that appeared without being asked for would change
  what every existing run costs and how it fails.
- A refusal is CAPACITY, so the existing policy table moves the work to another
  provider. No new failure class: "no allowance left" is the same shape as
  "rate limited", and the table already knows what to do with that.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.claude_quota import (ClaudeQuotaConfig,  # noqa: E402
                                        ClaudeQuotaExceeded,
                                        ClaudeQuotaLedger, estimate_for)
from dobby.runtime.contracts import ArtifactContract  # noqa: E402
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.workers import WorkerAdapter, WorkerRegistry  # noqa: E402
from dobby.runtime.workers import WorkerResult  # noqa: E402

SCHEMA = {"type": "object"}


class FakeClaude(WorkerAdapter):
    """Answers like a provider worker, including the usage envelope.

    Records what `context` it was handed, because whether the runner asked for
    usage at all is one of the things under test.
    """

    def __init__(self, usage=None):
        self.usage = usage
        self.seen: list = []

    def run(self, node, context):
        self.seen.append(dict(context))
        meta = {"provider": "claude"}
        if context.get("collect_usage"):
            meta["usage"] = self.usage
        return WorkerResult(True, payload={"done": True}, meta=meta)


def claude_node(node_id="call", **config):
    return G.TaskNode(
        node_id=node_id, kind="implement", worker="provider",
        instruction="do the thing",
        contract=ArtifactContract(output_schema=SCHEMA),
        config={"provider": "claude", **config})


class TheLedgerAlone(unittest.TestCase):
    """No runner: the reservation arithmetic, on its own."""

    def ledger(self, **kwargs):
        return ClaudeQuotaLedger(config=ClaudeQuotaConfig(**kwargs))

    def test_a_reservation_is_counted_as_spent_until_it_settles(self):
        ledger = self.ledger(max_calls=2)
        before = ledger.remaining()["calls"]
        reservation = ledger.reserve(
            node_id="n", role="implement",
            estimate=estimate_for("implement", config=ledger.config))
        self.assertEqual(ledger.remaining()["calls"], before - 1)
        ledger.settle(reservation, usage={"output_tokens": 10})
        self.assertEqual(ledger.remaining()["calls"], before - 1)

    def test_a_cancelled_reservation_gives_the_allowance_back(self):
        ledger = self.ledger(max_calls=1)
        reservation = ledger.reserve(
            node_id="n", role="implement",
            estimate=estimate_for("implement", config=ledger.config))
        self.assertEqual(ledger.remaining()["calls"], 0)
        ledger.cancel(reservation)
        self.assertEqual(ledger.remaining()["calls"], 1)

    def test_the_call_cap_refuses_rather_than_overspending(self):
        ledger = self.ledger(max_calls=1)
        estimate = estimate_for("implement", config=ledger.config)
        first = ledger.reserve(node_id="a", role="implement", estimate=estimate)
        ledger.settle(first, usage={"output_tokens": 1})
        with self.assertRaises(ClaudeQuotaExceeded):
            ledger.reserve(node_id="b", role="implement", estimate=estimate)

    def test_unmeasured_usage_is_not_zero_usage(self):
        """A call nobody could count still spent real tokens."""
        ledger = self.ledger(max_calls=5)
        reservation = ledger.reserve(
            node_id="n", role="implement",
            estimate=estimate_for("implement", config=ledger.config))
        ledger.settle(reservation, usage=None)
        self.assertEqual(ledger.unmeasured_calls, 1)
        self.assertEqual(ledger.state, "EXHAUSTED",
                         "fail_closed_on_unmeasured_usage defaults True")

    def test_a_cap_below_the_minimum_estimate_refuses_everything(self):
        """And says so. `min_billable_tokens` x `fallback_multiplier` is the
        floor a first reservation asks for, so a cap under it admits nothing —
        which is correct, and worth having the message spell out."""
        ledger = self.ledger(max_calls=5, max_billable_tokens=1_000)
        with self.assertRaises(ClaudeQuotaExceeded) as caught:
            ledger.reserve(node_id="n", role="implement",
                           estimate=estimate_for("implement",
                                                 config=ledger.config))
        self.assertIn("exceeds the cap", str(caught.exception))

    def test_refused_and_overrun_are_never_merged(self):
        # High enough to admit the 6,000-token floor estimate, low enough that
        # a real million-token call blows through it.
        ledger = self.ledger(max_calls=5, max_billable_tokens=10_000)
        reservation = ledger.reserve(
            node_id="n", role="implement",
            estimate=estimate_for("implement", config=ledger.config))
        ledger.settle(reservation, usage={"input_tokens": 500_000,
                                          "output_tokens": 500_000})
        self.assertTrue(ledger.overrun, "it ran and cost more than estimated")
        with self.assertRaises(ClaudeQuotaExceeded):
            ledger.reserve(node_id="m", role="implement",
                           estimate=estimate_for("implement",
                                                 config=ledger.config))


class TheWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")

    def runner(self, worker, *, quota=None, provider="claude"):
        """`override_provider` because PLACEMENT may move the work.

        A node configured `provider: "claude"` does not necessarily run on
        claude: `runner._place` re-decides per node and rewrites
        `node.config["provider"]`. Measured here — a node asking for claude ran
        on codex, and the claude ledger correctly metered nothing. That is the
        system working; it just makes an unpinned node the wrong instrument for
        testing a claude-specific cap.
        """
        return Runner(repo=self.tmp.name, data_dir=self.data,
                      workers=WorkerRegistry({"provider": worker}),
                      override_provider=provider,
                      quota=quota, sleep=lambda _s: None)

    def test_it_is_off_unless_asked_for(self):
        worker = FakeClaude()
        runner = self.runner(worker)
        self.assertIsNone(runner.quota)
        run_id = runner.start("t", G.TaskGraph([claude_node()]))
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertFalse(worker.seen[0]["collect_usage"],
                         "usage collection changes the CLI's argv and must not "
                         "happen for a ledger nobody switched on")

    def test_switching_it_on_asks_the_worker_for_usage(self):
        """The plumbing that was missing: no usage, nothing to settle from."""
        worker = FakeClaude(usage={"output_tokens": 100})
        runner = self.runner(worker, quota=ClaudeQuotaConfig(max_calls=5))
        run_id = runner.start("t", G.TaskGraph([claude_node()]))
        runner.run(run_id)
        self.assertTrue(worker.seen[0]["collect_usage"])

    def test_a_settled_call_moves_the_ledger(self):
        worker = FakeClaude(usage={"output_tokens": 100})
        runner = self.runner(worker, quota=ClaudeQuotaConfig(max_calls=5))
        before = runner.quota.remaining()["calls"]
        run_id = runner.start("t", G.TaskGraph([claude_node()]))
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(runner.quota.remaining()["calls"], before - 1)
        self.assertEqual(runner.quota.calls_settled, 1)

    def test_an_exhausted_lane_refuses_the_node_before_it_runs(self):
        """Refused means nothing ran. That is the point of admitting first."""
        worker = FakeClaude(usage={"output_tokens": 1})
        runner = self.runner(worker, quota=ClaudeQuotaConfig(max_calls=1))
        first = runner.start("a", G.TaskGraph([claude_node()]))
        runner.run(first)
        self.assertEqual(len(worker.seen), 1)

        second = runner.start("b", G.TaskGraph([claude_node()]))
        result = runner.run(second)
        self.assertEqual(result.state, G.FAILED)
        self.assertEqual(len(worker.seen), 1,
                         "the second node must not have been launched")

    def test_the_refusal_is_capacity_so_the_work_can_move_elsewhere(self):
        """No new failure class: the policy table already routes CAPACITY."""
        from dobby.runtime.failures import (CAPACITY, DEFAULT_POLICY,
                                            RETRY_ELSEWHERE)

        worker = FakeClaude(usage={"output_tokens": 1})
        runner = self.runner(worker, quota=ClaudeQuotaConfig(max_calls=1))
        runner.run(runner.start("a", G.TaskGraph([claude_node()])))
        run_id = runner.start("b", G.TaskGraph([claude_node()]))
        runner.run(run_id)

        attempt = runner.store.attempts(run_id, "call")[-1]
        self.assertEqual(attempt["failure_class"], CAPACITY)
        self.assertEqual(DEFAULT_POLICY[CAPACITY].action, RETRY_ELSEWHERE)
        self.assertTrue(DEFAULT_POLICY[CAPACITY].avoid_last_provider)

    def test_a_reservation_is_released_even_when_the_worker_raises(self):
        class Exploding(WorkerAdapter):
            def run(self, node, context):
                raise RuntimeError("boom")

        runner = self.runner(Exploding(),
                             quota=ClaudeQuotaConfig(max_calls=3))
        before = runner.quota.remaining()["calls"]
        runner.run(runner.start("t", G.TaskGraph([claude_node()])))
        self.assertEqual(runner.quota.reservations, {},
                         "a held reservation would leak the allowance")
        self.assertLessEqual(runner.quota.remaining()["calls"], before)

    def test_a_non_claude_provider_is_not_metered_by_the_claude_ledger(self):
        worker = FakeClaude(usage={"output_tokens": 1})
        runner = self.runner(worker, quota=ClaudeQuotaConfig(max_calls=1),
                             provider="codex")
        node = G.TaskNode(node_id="call", kind="implement", worker="provider",
                          instruction="i",
                          contract=ArtifactContract(output_schema=SCHEMA),
                          config={"provider": "codex"})
        for _ in range(3):
            runner.run(runner.start("t", G.TaskGraph([node])))
        self.assertEqual(runner.quota.calls_settled, 0)
        self.assertEqual(len(worker.seen), 3,
                         "the claude cap must not bound another provider")


if __name__ == "__main__":
    unittest.main()
