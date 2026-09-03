"""Reading the store must not cost more the longer the store has been used.

Both metric walks opened a SQLite connection per call, and several of them call
`attempts` once per accumulated run. Measured on this machine, chain graphs of
three nodes:

    accumulated   scorecard   report    a NEW run
             10       0.23s    0.60s        0.36s
            300       8.22s   22.03s        3.13s

The last column is the one that matters. `scorecard` is not a reporting tool:
`placement.ProviderPlacement` reads it to CHOOSE a provider, so the walk happens
INSIDE a run. Bookkeeping about runs already finished was making new runs nine
times slower. A harness that slows down with age has a shelf life.

Nothing about the data grew superlinearly -- new runs and `list_runs` stayed
flat over the same range. What grew was the number of round trips to it: 120
runs produced 246 store transactions in one `report`, each opening and closing
its own connection at ~27ms. `RunStore.session()` already existed for exactly
this and `Runner.run` already used it; these two callers did not.

After: scorecard 1.87s, report 2.46s, a new run 0.35s, and the returned values
are identical.

THE ASSERTIONS ARE ON ROUND TRIPS, NOT SECONDS. A wall-clock threshold measured
here is a flake on a slower machine, and the thing a later edit would remove by
accident is the session. Counting transactions says the same thing and says it
the same way everywhere.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import RunBudget  # noqa: E402
from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import ArtifactContract, SCHEMAS  # noqa: E402
from dobby.runtime.metrics import report, scorecard  # noqa: E402
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.store import RunStore  # noqa: E402
from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,  # noqa: E402
                                   WorkerResult)

#: Three providers, because a fleet of one turns any RETRY_ELSEWHERE into a
#: CAPACITY failure and the run dies for a reason that has nothing to do with
#: what is being measured. Learned by measuring it wrong first.
FLEET = {"claude", "codex", "gemini"}


class Instant(WorkerAdapter):
    name = "provider"

    def run(self, node, context):
        return WorkerResult(True, payload={"steps": [{"what": "do the thing"}]})


def chain(n):
    nodes, previous = [], None
    for i in range(n):
        nodes.append(G.TaskNode(
            node_id=f"n{i}", kind="plan", worker="provider", instruction="i",
            depends_on=([previous] if previous else []),
            contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
            config={"provider": "claude"}))
        previous = f"n{i}"
    return G.TaskGraph(nodes)


class CountingStore(RunStore):
    """A store that records how many transactions it opened.

    Counted at `connect`, which is the thing that costs: a held session calls
    it once and a per-call caller calls it once per query.
    """

    def __init__(self, *args, **kwargs):
        self.connects = 0
        super().__init__(*args, **kwargs)


def counting(data_dir):
    """A `CountingStore` whose `connect` is instrumented."""
    from dobby.runtime import store as store_module

    original = store_module.connect
    made = CountingStore(data_dir)

    def counted(path):
        made.connects += 1
        return original(path)

    store_module.connect = counted
    made._restore = lambda: setattr(store_module, "connect", original)
    made.connects = 0                      # ignore the schema setup above
    return made


class ScaleCase(unittest.TestCase):
    RUNS = 12
    NODES = 3

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "d")
        runner = Runner(repo=cls.tmp, data_dir=cls.data,
                        workers=WorkerRegistry({"provider": Instant()}),
                        available_providers=FLEET, sleep=lambda _s: None)
        for _ in range(cls.RUNS):
            runner.run(runner.start("t", chain(cls.NODES)),
                       budget=RunBudget(max_attempts=cls.NODES * 3))
        cls.accumulated = len(RunStore(cls.data).list_runs(limit=10000))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def connects_for(self, call):
        store = counting(self.data)
        try:
            call(store)
        finally:
            store._restore()
        return store.connects


class OneConnectionPerWalk(ScaleCase):
    def test_the_store_really_did_accumulate_runs(self):
        """The premise. Without runs to walk, both counts are one either way."""
        self.assertGreaterEqual(self.accumulated, self.RUNS)

    def test_report_opens_one_connection(self):
        self.assertEqual(self.connects_for(lambda s: report(s)), 1)

    def test_scorecard_opens_one_connection(self):
        self.assertEqual(self.connects_for(lambda s: scorecard(s)), 1)

    def test_without_a_session_the_same_walk_costs_many(self):
        """The control, so the numbers above are evidence and not a formality.

        `_report` is the body without the session. If this ever stops costing
        more than one, the counter is broken and the tests above prove nothing.
        """
        from dobby.runtime.metrics import _report

        many = self.connects_for(lambda s: _report(s, limit=500))
        self.assertGreater(many, 1, "the connection counter is not counting")
        self.assertGreater(many, self.accumulated,
                           "at least one connection per accumulated run")

    def test_a_plain_query_still_opens_its_own(self):
        """Outside a session nothing changed: `dobby runtime status` must not
        leave a handle behind for `rmtree` to trip over."""
        self.assertEqual(self.connects_for(lambda s: s.list_runs(limit=5)), 1)


class TheAnswersAreUnchanged(ScaleCase):
    """A faster walk that returns something else is not the same walk."""

    def test_report_returns_what_it_did_before(self):
        from dobby.runtime.metrics import _report

        store = RunStore(self.data)
        self.assertEqual(report(store), _report(store, limit=500))

    def test_scorecard_returns_what_it_did_before(self):
        from dobby.runtime.metrics import _scorecard

        store = RunStore(self.data)
        self.assertEqual(scorecard(store),
                         _scorecard(store, limit=500, window=50))

    def test_report_still_names_its_gaps(self):
        out = report(RunStore(self.data))
        self.assertIn("unmeasured", out)
        self.assertIn("span_write_failures", out)


class TheSessionIsReleased(ScaleCase):
    """A walk that held its connection open would put the Windows `rmtree`
    defect back, in the module that runs on every placement decision."""

    def test_report_leaves_no_handle(self):
        store = RunStore(self.data)
        report(store)
        self.assertIsNone(getattr(store._local, "conn", None))

    def test_scorecard_leaves_no_handle(self):
        store = RunStore(self.data)
        scorecard(store)
        self.assertIsNone(getattr(store._local, "conn", None))

    def test_a_raising_walk_still_releases_it(self):
        """The `finally` in `session()`, exercised through the metric.

        Raised from a metric rather than by passing a bad `limit`: that reaches
        the SQL layer and comes back as `sqlite3.IntegrityError`, which tests
        sqlite's argument handling and not this module's cleanup.
        """
        from dobby.runtime import metrics as metrics_module

        store = RunStore(self.data)
        original = metrics_module.task_success_at_verifier

        def explode(*args, **kwargs):
            raise RuntimeError("boom")

        metrics_module.task_success_at_verifier = explode
        try:
            with self.assertRaises(RuntimeError):
                report(store)
        finally:
            metrics_module.task_success_at_verifier = original
        self.assertIsNone(getattr(store._local, "conn", None))


if __name__ == "__main__":
    unittest.main()
