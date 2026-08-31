"""Several OS processes driving one run, which is what the leases are for.

Every existing concurrency test is in-process: threads against one `RunStore`
object, one connection pool, one Python heap. That covers the scheduler and the
lease table and it cannot cover the thing an operator actually does, which is
start more than one worker against a shared project directory.

Measured before this file existed, four processes on one run:

    {"ok": false, "error": "GraphError: illegal run transition WAITING -> WAITING"}
    {"ok": true,  "state": "WAITING"}
    {"ok": true,  "state": "WAITING"}
    {"ok": false, "error": "GraphError: illegal run transition WAITING -> SUCCEEDED"}

Two of four died. The node leases were doing their job -- no node ran twice --
and the RUN state layer underneath them assumed a single writer:

- a worker parking an already-parked run was told `WAITING -> WAITING` is
  illegal, when it is not a transition at all but the same fact reported twice;
- a worker that finished the last node while another had parked the run was
  refused `WAITING -> SUCCEEDED`, because the table made every path to a
  terminal state go through RUNNING first. True of one process, false of
  several.

These tests spawn real subprocesses. They are slower than the rest of the suite
and there is no in-process substitute: the defect lives in what two SQLite
connections in two address spaces see of each other.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402

#: One worker. Seeds a run, or attaches to one and drives it.
WORKER = textwrap.dedent('''
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    from dobby.runtime import graph as G
    from dobby.runtime.contracts import ArtifactContract, SCHEMAS
    from dobby.runtime.runner import Runner
    from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,
                                       WorkerResult)

    class Slow(WorkerAdapter):
        """Holds each node long enough for the processes to overlap."""
        name = "provider"
        def run(self, node, context):
            time.sleep(0.15)
            return WorkerResult(True,
                                payload={"steps": [{"what": "do the thing"}]})

    repo, data, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    nodes, prev = [], None
    for i in range(int(sys.argv[4])):
        nodes.append(G.TaskNode(
            node_id="n%d" % i, kind="plan", worker="provider",
            instruction="i", depends_on=([prev] if prev else []),
            contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
            config={"provider": "claude"}))
        prev = "n%d" % i

    runner = Runner(repo=repo, data_dir=data,
                    workers=WorkerRegistry({"provider": Slow()}),
                    available_providers={"claude", "codex", "gemini"},
                    sleep=lambda _s: None)
    if mode == "seed":
        print(runner.start("shared task", G.TaskGraph(nodes)))
    else:
        run_id = sys.argv[5]
        try:
            result = runner.run(run_id)
            attempts = {s["node_id"]: len(runner.store.attempts(run_id,
                                                                s["node_id"]))
                        for s in result.to_dict()["steps"]}
            print(json.dumps({"pid": os.getpid(), "ok": True,
                              "state": result.state, "attempts": attempts}))
        except Exception as exc:
            print(json.dumps({"pid": os.getpid(), "ok": False,
                              "error": "%s: %s" % (type(exc).__name__, exc)}))
''')


class MultiProcessCase(unittest.TestCase):
    #: Kept small on purpose. Each process pays a full interpreter start, and
    #: what is under test is contention, not throughput.
    WORKERS = 3
    NODES = 3

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "worker.py")
        with open(self.script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WORKER)
        self.data = os.path.join(self.tmp, "data")

    def env(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def seed(self):
        proc = subprocess.run(
            [sys.executable, self.script, REPO, self.data, "seed",
             str(self.NODES)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=self.env(), timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()[-1]

    def race(self, run_id):
        """Start every worker, then collect. Returns the parsed rows."""
        procs = [subprocess.Popen(
            [sys.executable, self.script, REPO, self.data, "work",
             str(self.NODES), run_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=self.env())
            for _ in range(self.WORKERS)]
        rows = []
        for proc in procs:
            out, err = proc.communicate(timeout=300)
            line = next((l for l in (out or "").splitlines()
                         if l.strip().startswith("{")), None)
            self.assertIsNotNone(line, f"no verdict from a worker: {err}")
            rows.append(json.loads(line))
        return rows


class OneRunManyProcesses(MultiProcessCase):
    def setUp(self):
        super().setUp()
        self.rows = self.race(self.seed())

    def test_no_worker_dies(self):
        """The whole defect. Two of four used to raise GraphError."""
        died = [r for r in self.rows if not r["ok"]]
        self.assertEqual(died, [], f"{len(died)} of {len(self.rows)} crashed")

    def test_exactly_one_worker_finishes_the_run(self):
        finished = [r for r in self.rows if r.get("state") == G.SUCCEEDED]
        self.assertEqual(len(finished), 1, self.rows)

    def test_the_losers_park_rather_than_fail(self):
        """A node somebody else holds is not this worker's failure."""
        for row in self.rows:
            if row.get("state") != G.SUCCEEDED:
                self.assertEqual(row["state"], G.WAITING, row)

    def test_no_node_runs_twice(self):
        """The lease's own promise, across address spaces this time."""
        repeated = [(row["pid"], node, count) for row in self.rows
                    for node, count in (row.get("attempts") or {}).items()
                    if count > 1]
        self.assertEqual(repeated, [])

    def test_the_winner_ran_every_node_once(self):
        winner = next(r for r in self.rows if r.get("state") == G.SUCCEEDED)
        self.assertEqual(sorted(winner["attempts"]),
                         [f"n{i}" for i in range(self.NODES)])
        self.assertEqual(set(winner["attempts"].values()), {1})


class TheLeaseIsNotStealable(MultiProcessCase):
    """A live worker's lease must survive another worker's bookkeeping.

    Found by running this file under load, where it failed four times in six.
    `_execute_node` promoted a node from PENDING to READY out of its OWN copy
    of the graph, and `LEASED -> READY` is a legal transition because that is
    how a lost lease is recovered. The event log of a failing round:

        PENDING -> READY   "dependencies satisfied"   (worker A)
        node_leased        A
        attempt_started
        LEASED  -> READY   "dependencies satisfied"   (worker B, stale memory)
        node_leased        B
        attempt_started
        LEASED  -> RUNNING "attempt 2"

    B's node still said PENDING, so B wrote READY over a lease A was holding
    and took it. Both ran the node; `n0` ended with two promoted artifacts, and
    the invariant that caught it was `_promoted_inputs` refusing a second
    promotion one layer downstream -- correctly, and far too late to prevent
    the second provider call from being paid for.

    `set_node_state(expect=...)` makes that promotion a compare-and-set in the
    same transaction as the read, so a worker that loses the race says so
    instead of overwriting somebody. Measured after: ten rounds of three
    processes, zero crashes, zero repeated attempts, one SUCCEEDED per round.
    """

    ROUNDS = 3

    def test_no_node_is_ever_promoted_twice(self):
        """The invariant the theft broke, checked at the store."""
        from dobby.runtime.store import RunStore

        for _ in range(self.ROUNDS):
            self.setUp()
            run_id = self.seed()
            self.race(run_id)
            store = RunStore(self.data)
            for node_id in (f"n{i}" for i in range(self.NODES)):
                promoted = store.artifacts(run_id, node_id=node_id,
                                           state="PROMOTED")
                self.assertLessEqual(
                    len(promoted), 1,
                    f"{node_id} promoted {len(promoted)} artifacts: "
                    f"{[p['artifact_id'] for p in promoted]}")

    def test_a_lease_is_never_written_over_by_a_bookkeeping_move(self):
        """`LEASED -> READY` for `dependencies satisfied` is the theft. Lease
        RECOVERY may still do it, and says a different reason."""
        from dobby.runtime.store import RunStore

        for _ in range(self.ROUNDS):
            self.setUp()
            run_id = self.seed()
            self.race(run_id)
            store = RunStore(self.data)
            stolen = [e for e in store.events(run_id)
                      if e["kind"] == "node_state"
                      and e["payload"].get("from") == "LEASED"
                      and e["payload"].get("to") == "READY"
                      and "dependencies" in (e["payload"].get("reason") or "")]
            self.assertEqual(stolen, [], "a live lease was overwritten")


class CompareAndSet(unittest.TestCase):
    """`set_node_state(expect=...)` on its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        from dobby.runtime.store import RunStore

        self.store = RunStore(os.path.join(self.tmp, "d"))
        node = G.TaskNode(node_id="n", kind="plan", worker="static",
                          instruction="i", config={"payload": {}})
        self.run_id = self.store.create_run("t", G.TaskGraph([node]))

    def state(self):
        return self.store.load_run(self.run_id)["graph"].nodes["n"].state

    def test_it_moves_when_the_expectation_holds(self):
        self.assertTrue(self.store.set_node_state(
            self.run_id, "n", G.READY, expect=G.PENDING))
        self.assertEqual(self.state(), G.READY)

    def test_it_refuses_and_writes_nothing_when_it_does_not(self):
        self.store.set_node_state(self.run_id, "n", G.READY, expect=G.PENDING)
        self.store.lease_node(self.run_id, "n", holder="somebody/1")
        self.assertEqual(self.state(), G.LEASED)

        before = len(self.store.events(self.run_id))
        self.assertFalse(self.store.set_node_state(
            self.run_id, "n", G.READY, expect=G.PENDING))
        self.assertEqual(self.state(), G.LEASED, "the lease was overwritten")
        self.assertEqual(len(self.store.events(self.run_id)), before,
                         "a refused write must not leave an event")

    def test_without_an_expectation_it_behaves_as_before(self):
        self.assertTrue(self.store.set_node_state(self.run_id, "n", G.READY))
        self.assertEqual(self.state(), G.READY)

    def test_a_self_transition_reports_success_and_writes_no_event(self):
        self.store.set_node_state(self.run_id, "n", G.READY)
        before = len(self.store.events(self.run_id))
        self.assertTrue(self.store.set_node_state(self.run_id, "n", G.READY))
        self.assertEqual(len(self.store.events(self.run_id)), before)


class TheRulesThatMadeItPossible(unittest.TestCase):
    """Stated separately, because each is a decision and not a detail."""

    def test_a_started_run_may_reach_a_terminal_state_from_where_it_is(self):
        from dobby.runtime.graph import check_run_transition

        for start in (G.RUNNING, G.WAITING, G.RECOVERING):
            for end in (G.SUCCEEDED, G.FAILED, G.CANCELLED):
                check_run_transition(start, end)      # raises if illegal

    def test_a_queued_run_may_not_succeed_without_running(self):
        """Not widened. Queued means nothing ran, so SUCCEEDED there would be a
        claim of work nobody did."""
        from dobby.runtime.graph import GraphError, check_run_transition

        with self.assertRaises(GraphError):
            check_run_transition(G.QUEUED, G.SUCCEEDED)

    def test_terminal_states_are_still_final(self):
        from dobby.runtime.graph import GraphError, check_run_transition

        for terminal in (G.SUCCEEDED, G.FAILED, G.CANCELLED):
            with self.assertRaises(GraphError):
                check_run_transition(terminal, G.RUNNING)

    def test_a_self_transition_is_a_no_op_and_writes_no_event(self):
        """An event log is what a resume replays, and a row saying
        WAITING -> WAITING records a change that did not occur."""
        from dobby.runtime.store import RunStore

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = RunStore(os.path.join(tmp, "d"))
        node = G.TaskNode(node_id="n", kind="plan", worker="static",
                          instruction="i", config={"payload": {}})
        run_id = store.create_run("t", G.TaskGraph([node]))
        store.set_run_state(run_id, G.RUNNING, reason="first")
        before = len([e for e in store.events(run_id)
                      if e["kind"] == "run_state"])
        store.set_run_state(run_id, G.RUNNING, reason="again")
        after = [e for e in store.events(run_id) if e["kind"] == "run_state"]
        self.assertEqual(len(after), before)
        self.assertEqual(store.load_run(run_id)["state"], G.RUNNING)


if __name__ == "__main__":
    unittest.main()
