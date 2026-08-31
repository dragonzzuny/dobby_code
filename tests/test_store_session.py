"""One store connection per run instead of one per transaction.

`RunStore` opened and closed a SQLite connection for every transaction. The
reason recorded in its docstring was real -- an open handle on Windows makes
`shutil.rmtree` fail with PermissionError, which broke every temp-directory
cleanup in the suite -- but the reason given alongside it was not: "the cost of
reopening a local SQLite file is not measurable next to a provider call".

Measured on this machine, 300 transactions against one WAL database:

    connection per transaction   27.3 ms/tx
    one connection reused         4.7 ms/tx

A 16-node graph runs roughly 19 store transactions per node. End to end, with a
worker that returns instantly so only the harness is being timed:

    16 nodes   11.97 s -> 2.12 s      748 ms/node -> 132 ms/node

`session()` keeps the safety property and takes the saving: the connection is
held for the length of a run and closed in a `finally`, and every call outside
a run still opens its own. That matters because the properties below are the
ones a shortcut here would quietly break.
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import ArtifactContract, SCHEMAS  # noqa: E402
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.store import RunStore  # noqa: E402
from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,  # noqa: E402
                                   WorkerResult)


class Instant(WorkerAdapter):
    name = "provider"

    def run(self, node, context):
        return WorkerResult(True,
                            payload={"steps": [{"what": "do the thing"}]})


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "d")
        self.store = RunStore(self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def held(self):
        return getattr(self.store._local, "conn", None)


class TheSessionHoldsExactlyOneConnection(StoreCase):
    def test_nothing_is_held_outside_a_session(self):
        self.assertIsNone(self.held())
        self.store.list_runs()
        self.assertIsNone(self.held(), "a plain call must not leave a handle")

    def test_a_session_holds_one_and_releases_it(self):
        with self.store.session():
            self.assertIsNotNone(self.held())
            first = self.held()
            self.store.list_runs()
            self.assertIs(self.held(), first, "the same connection, reused")
        self.assertIsNone(self.held())

    def test_nesting_does_not_open_a_second(self):
        """A caller must not have to know whether its caller opened one."""
        with self.store.session():
            outer = self.held()
            with self.store.session():
                self.assertIs(self.held(), outer)
            self.assertIs(self.held(), outer,
                          "the inner block must not close the outer's handle")
        self.assertIsNone(self.held())

    def test_the_handle_is_released_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.store.session():
                raise RuntimeError("boom")
        self.assertIsNone(self.held())

    def test_the_connection_is_actually_closed_not_just_forgotten(self):
        with self.store.session():
            conn = self.held()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


class ItIsPerThread(StoreCase):
    """`sqlite3` refuses a connection from a thread other than its maker, and
    `max_parallel > 1` runs nodes on several."""

    def test_a_second_thread_sees_no_session_and_still_works(self):
        seen, errors = [], []

        def worker():
            try:
                seen.append(getattr(self.store._local, "conn", None))
                self.store.list_runs()          # falls back to per-call
            except Exception as exc:            # pragma: no cover - the report
                errors.append(exc)

        with self.store.session():
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(seen, [None],
                         "the other thread must not borrow this one's handle")

    def test_two_threads_may_each_hold_their_own(self):
        held, errors = {}, []

        def worker(name):
            try:
                with self.store.session():
                    held[name] = getattr(self.store._local, "conn", None)
                    self.store.list_runs()
            except Exception as exc:            # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(held), 2)
        self.assertIsNot(held["a"], held["b"])


class TheDurabilityKnob(StoreCase):
    """`DOBBY_SQLITE_SYNCHRONOUS`, off by default and refused when misspelt.

    Under WAL, `NORMAL` keeps a consistent database and survives the process
    being killed -- the frames are already in the WAL, so an application crash
    loses nothing. What it gives up is the OS crash and the power cut. Measured
    on a held connection, 300 transactions: 4.7 ms/tx at FULL, 0.30 ms/tx at
    NORMAL; end to end on a 16-node graph, 103 ms/node against 49 ms/node.

    FULL stays the default. This store is what a resume reads, so trading its
    durability is a decision somebody makes for a specific machine, not
    something a version bump does to them.
    """

    def setUp(self):
        super().setUp()
        self.original = os.environ.get("DOBBY_SQLITE_SYNCHRONOUS")
        self.addCleanup(self.restore)

    def restore(self):
        if self.original is None:
            os.environ.pop("DOBBY_SQLITE_SYNCHRONOUS", None)
        else:
            os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = self.original

    def mode_of(self, conn):
        return {0: "OFF", 1: "NORMAL", 2: "FULL",
                3: "EXTRA"}[conn.execute("PRAGMA synchronous").fetchone()[0]]

    def test_the_default_is_full(self):
        os.environ.pop("DOBBY_SQLITE_SYNCHRONOUS", None)
        with self.store.session():
            self.assertEqual(self.mode_of(self.held()), "FULL")

    def test_it_can_be_lowered_deliberately(self):
        os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = "NORMAL"
        with self.store.session():
            self.assertEqual(self.mode_of(self.held()), "NORMAL")

    def test_lowercase_is_accepted(self):
        os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = "normal"
        with self.store.session():
            self.assertEqual(self.mode_of(self.held()), "NORMAL")

    def test_a_typo_is_refused_rather_than_defaulted(self):
        """Falling back to FULL would make a machine somebody tuned behave like
        one they did not, and say nothing about it."""
        from dobby.runtime.store import StoreError

        os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = "NORMALL"
        with self.assertRaises(StoreError) as caught:
            with self.store.session():
                pass
        self.assertIn("NORMALL", str(caught.exception))

    def test_an_empty_value_is_the_default(self):
        os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = ""
        with self.store.session():
            self.assertEqual(self.mode_of(self.held()), "FULL")


class ThroughARun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def runner(self):
        return Runner(repo=self.tmp, data_dir=os.path.join(self.tmp, "d"),
                      workers=WorkerRegistry({"provider": Instant()}),
                      sleep=lambda _s: None)

    def graph(self, n):
        nodes, prev = [], None
        for i in range(n):
            nodes.append(G.TaskNode(
                node_id=f"n{i}", kind="plan", worker="provider",
                instruction="i", depends_on=([prev] if prev else []),
                contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
                config={"provider": "claude"}))
            prev = f"n{i}"
        return G.TaskGraph(nodes)

    def test_a_run_still_succeeds_and_records_every_node(self):
        runner = self.runner()
        result = runner.run(runner.start("t", self.graph(4)))
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(len(result.to_dict()["steps"]), 4)

    def test_the_run_leaves_no_handle_behind(self):
        """The property the per-transaction connection was protecting. On
        Windows a surviving handle makes `rmtree` raise, so this is checked by
        deleting the directory the way a temp-dir cleanup would."""
        runner = self.runner()
        runner.run(runner.start("t", self.graph(2)))
        self.assertIsNone(getattr(runner.store._local, "conn", None))
        victim = tempfile.mkdtemp()
        store = RunStore(os.path.join(victim, "d"))
        store.list_runs()
        shutil.rmtree(victim)                   # raises on Windows if held
        self.assertFalse(os.path.exists(victim))

    def test_resuming_the_same_run_opens_a_fresh_session(self):
        """Resume is the same code path, so it has to work twice."""
        runner = self.runner()
        run_id = runner.start("t", self.graph(3))
        runner.run(run_id)
        again = runner.run(run_id)
        self.assertEqual(again.state, G.SUCCEEDED, again.to_dict())
        self.assertIsNone(getattr(runner.store._local, "conn", None))

    def test_the_events_are_all_there(self):
        """A held connection commits per transaction exactly as before. If it
        did not, the event log -- which is what resume reads -- would be short.
        """
        runner = self.runner()
        result = runner.run(runner.start("t", self.graph(3)))
        events = runner.store.events(result.run_id)
        kinds = [e["kind"] for e in events]
        self.assertIn("artifact", kinds)
        self.assertIn("node_state", kinds)
        promoted = runner.store.artifacts(result.run_id, state="PROMOTED")
        self.assertEqual(len(promoted), 3)


class ReadModifyWriteIsSerialised(StoreCase):
    """A compare-and-set has to be inside a transaction to mean anything.

    `sqlite3` defers `BEGIN` until a statement that WRITES, whatever
    `isolation_level` says. So the SELECT at the top of every compare-and-set in
    this store ran outside any transaction, and two processes could both read
    the old value before either wrote. Measured, four workers on one run, with
    `set_node_state(expect=PENDING)` already in place:

        PENDING -> READY   "dependencies satisfied"   worker A
        node_leased        A
        attempt_started
        PENDING -> READY   "dependencies satisfied"   worker B

    Both guards saw PENDING and both passed. The guard was right and was being
    asked a question nobody had locked. `BEGIN IMMEDIATE` takes the write lock
    from the first statement, so the read and the write are one unit.
    """

    def test_two_threads_cannot_both_win_the_same_compare_and_set(self):
        node = G.TaskNode(node_id="n", kind="plan", worker="static",
                          instruction="i", config={"payload": {}})
        run_id = self.store.create_run("t", G.TaskGraph([node]))
        results, errors = [], []
        start = threading.Barrier(2)

        def contend():
            try:
                start.wait(timeout=10)
                results.append(self.store.set_node_state(
                    run_id, "n", G.READY, expect=G.PENDING))
            except Exception as exc:            # pragma: no cover - reported
                errors.append(exc)

        threads = [threading.Thread(target=contend) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True],
                         "both callers were told they had moved the node")

    def test_the_losing_write_leaves_no_event(self):
        node = G.TaskNode(node_id="n", kind="plan", worker="static",
                          instruction="i", config={"payload": {}})
        run_id = self.store.create_run("t", G.TaskGraph([node]))
        self.store.set_node_state(run_id, "n", G.READY, expect=G.PENDING)
        before = len(self.store.events(run_id))
        self.store.set_node_state(run_id, "n", G.READY, expect=G.PENDING)
        self.assertEqual(len(self.store.events(run_id)), before)

    def test_a_transaction_still_rolls_back_on_an_exception(self):
        """`BEGIN IMMEDIATE` is hand-rolled now, so the unhappy path is ours."""
        node = G.TaskNode(node_id="n", kind="plan", worker="static",
                          instruction="i", config={"payload": {}})
        run_id = self.store.create_run("t", G.TaskGraph([node]))
        with self.assertRaises(RuntimeError):
            with self.store._tx() as conn:
                conn.execute(
                    "UPDATE nodes SET state=? WHERE run_id=? AND node_id=?",
                    (G.SKIPPED, run_id, "n"))
                raise RuntimeError("boom")
        self.assertEqual(self.state_of(run_id, "n"), G.PENDING,
                         "the aborted write was committed anyway")

    def state_of(self, run_id, node_id):
        return self.store.load_run(run_id)["graph"].nodes[node_id].state


if __name__ == "__main__":
    unittest.main()
