"""Lease ownership and external-effect reconciliation.

Two guarantees, both about what happens when a worker stops existing partway
through, and both previously false in a way the happy path could not show:

    1. A node held by a LIVE worker is not recovered out from under it. The
       lease claim was always atomic; recovery was what gave the node away.
    2. An external effect that was claimed and never confirmed is neither
       repeated nor reported as done. It is unknown, and the run says so.

The second is the one worth stating plainly: the runtime records the intent to
perform an effect BEFORE performing it, so a crash in that window leaves a claim
whose outcome nobody observed. There is no local evidence that decides it. Both
automatic answers are wrong, so the correct behaviour is to stop and name the
two ways out.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import process_alive
from dobby.runtime import (ArtifactContract, EXTERNAL_REVERSIBLE, RunStore,
                           Runner, TaskGraph, TaskNode, idempotency_key)
from dobby.runtime import graph as G
from dobby.runtime.store import (EFFECT_CLAIMED, EFFECT_CONFIRMED, StoreError,
                                 lease_is_held, worker_identity)


#: A fixture declares SOMETHING, because a contract that declares nothing is
#: now refused at the gate — `all([])` is True, so a node with no schema, no
#: check, no effect and nothing to ground used to promote whatever it was
#: handed. `{"type": "object"}` is the weakest real claim: the output is an
#: object. Tests that care about a stronger shape still pass their own.
FIXTURE_SCHEMA = {"type": "object"}


def static_node(node_id, payload, *, depends_on=(), side_effect="NONE",
                **config):
    return TaskNode(
        node_id=node_id, kind=node_id, depends_on=list(depends_on),
        worker="static",
        contract=ArtifactContract(output_schema=FIXTURE_SCHEMA,
                                  side_effect_class=side_effect),
        config={"payload": payload, **config})


def a_dead_pid() -> int:
    """A PID that is definitely not running: one we just watched exit.

    Reaped before returning — a zombie child would still count as alive on
    POSIX, and the point of this helper is a PID that does not.
    """
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait()
    pid = proc.pid
    proc.__exit__(None, None, None)      # release the Windows process handle
    return pid


class ProcessLiveness(unittest.TestCase):
    """`process_alive` is the evidence the lease rules stand on."""

    def test_this_process_is_alive(self):
        self.assertIs(process_alive(os.getpid()), True)

    def test_a_process_that_has_exited_is_not_alive(self):
        self.assertIs(process_alive(a_dead_pid()), False)

    def test_a_nonsense_pid_is_unknown_rather_than_a_guess(self):
        self.assertIsNone(process_alive(0))
        self.assertIsNone(process_alive(-1))


class LeaseOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(os.path.join(self.tmp.name, ".dobby"))
        self.run_id = self.store.create_run(
            "t", TaskGraph([static_node("a", {})]))
        self.store.set_node_state(self.run_id, "a", G.READY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_lease_records_who_holds_it_and_until_when(self):
        before = time.time()
        self.assertTrue(self.store.lease_node(self.run_id, "a",
                                              holder=worker_identity(),
                                              ttl_s=60))
        lease = self.store.node_lease(self.run_id, "a")
        self.assertEqual(lease["owner"], worker_identity())
        self.assertGreater(lease["expires"], before)
        self.assertTrue(lease["held"])

    def test_a_lease_held_by_a_dead_process_is_not_held(self):
        holder = f"{worker_identity().rpartition('/')[0]}/{a_dead_pid()}"
        self.store.lease_node(self.run_id, "a", holder=holder, ttl_s=600)
        self.assertFalse(self.store.node_lease(self.run_id, "a")["held"])

    def test_an_expired_lease_is_not_held_however_alive_its_owner(self):
        self.assertFalse(lease_is_held(worker_identity(),
                                       expires=time.time() - 1))

    def test_an_owner_this_runtime_did_not_write_is_not_held(self):
        # Whatever "ghost" is, it is not evidence of a running worker.
        self.assertFalse(lease_is_held("ghost", expires=time.time() + 600))
        self.assertFalse(lease_is_held("", expires=time.time() + 600))

    def test_an_unexpired_lease_on_another_host_is_treated_as_held(self):
        # Its PID cannot be probed from here, so the lease itself is the only
        # evidence there is — and it points at the holder.
        self.assertTrue(lease_is_held(f"some-other-box/{os.getpid()}",
                                      expires=time.time() + 600))

    def test_leaving_the_lease_clears_the_owner(self):
        self.store.lease_node(self.run_id, "a", holder=worker_identity())
        self.store.set_node_state(self.run_id, "a", G.NODE_RUNNING)
        self.assertEqual(self.store.node_lease(self.run_id, "a")["owner"],
                         worker_identity())
        self.store.set_node_state(self.run_id, "a", G.READY, enforce=False)
        lease = self.store.node_lease(self.run_id, "a")
        self.assertEqual(lease["owner"], "")
        self.assertFalse(lease["held"])

    def test_the_lease_survives_verification(self):
        """VERIFYING is a working state: the attempt is still open there.

        A lease released when the node enters VERIFYING leaves a window in which
        another worker sees an open attempt with no holder and recovers a node
        that is mid-acceptance-check.
        """
        self.store.lease_node(self.run_id, "a", holder=worker_identity())
        for state in (G.NODE_RUNNING, G.VERIFYING):
            self.store.set_node_state(self.run_id, "a", state)
            lease = self.store.node_lease(self.run_id, "a")
            self.assertEqual(lease["owner"], worker_identity(), state)
            self.assertTrue(lease["held"], state)
        self.assertTrue(self.store.renew_lease(self.run_id, "a",
                                               holder=worker_identity()))
        self.store.set_node_state(self.run_id, "a", G.NODE_SUCCEEDED)
        self.assertEqual(self.store.node_lease(self.run_id, "a")["owner"], "")

    def test_only_the_holder_can_renew(self):
        self.store.lease_node(self.run_id, "a", holder="host/1", ttl_s=60)
        self.assertFalse(self.store.renew_lease(self.run_id, "a",
                                                holder="host/2"))
        self.assertTrue(self.store.renew_lease(self.run_id, "a",
                                               holder="host/1", ttl_s=600))

    def test_a_store_written_before_the_lease_columns_still_opens(self):
        """The ALTER path. A v2 store has neither column."""
        import sqlite3
        path = os.path.join(self.tmp.name, "old", "state", "runtime",
                            "runs.sqlite3")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE nodes (run_id TEXT NOT NULL, "
                         "node_id TEXT NOT NULL, spec TEXT NOT NULL, "
                         "state TEXT NOT NULL, attempts INTEGER NOT NULL "
                         "DEFAULT 0, updated TEXT NOT NULL, "
                         "PRIMARY KEY (run_id, node_id))")
            conn.execute("INSERT INTO nodes VALUES('r','a','{}','READY',0,'x')")
            conn.commit()
        finally:
            conn.close()

        store = RunStore(os.path.join(self.tmp.name, "old"))
        lease = store.node_lease("r", "a")
        self.assertEqual(lease["owner"], "")
        self.assertFalse(lease["held"])


class RecoveryRespectsALiveHolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self):
        return Runner(self.tmp.name,
                      data_dir=os.path.join(self.tmp.name, ".dobby"),
                      sleep=lambda _s: None)

    def _mid_attempt(self, holder):
        """A run whose only node is leased by `holder`, attempt open."""
        runner = self.runner()
        run_id = runner.start("t", TaskGraph([static_node("a", {"ok": True})]))
        runner.store.set_node_state(run_id, "a", G.READY)
        runner.store.lease_node(run_id, "a", holder=holder, ttl_s=600)
        runner.store.start_attempt(run_id, "a", 1, worker="static")
        return runner, run_id

    def test_a_node_held_by_a_live_worker_is_left_alone(self):
        runner, run_id = self._mid_attempt(worker_identity())

        result = self.runner().run(run_id)

        rows = runner.store.attempts(run_id, "a")
        self.assertEqual(len(rows), 1,
                         f"a second worker started attempt 2 on a node a live "
                         f"worker holds: {[dict(r) for r in rows]}")
        self.assertIsNone(rows[0]["finished"])
        self.assertTrue(any("still held by" in n for n in result.notes),
                        result.notes)

    def test_a_node_held_by_a_dead_worker_is_recovered(self):
        holder = f"{worker_identity().rpartition('/')[0]}/{a_dead_pid()}"
        runner, run_id = self._mid_attempt(holder)

        result = self.runner().run(run_id)

        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        rows = runner.store.attempts(run_id, "a")
        self.assertEqual([r["outcome"] for r in rows],
                         [G.RETRYABLE_FAILURE, G.FINISHED])
        self.assertTrue(any("interrupted" in n for n in result.notes),
                        result.notes)

    def test_an_expired_lease_is_recovered_even_from_a_live_owner(self):
        """The bound on PID reuse, and on a holder that hangs forever."""
        runner, run_id = self._mid_attempt(worker_identity())
        with runner.store._tx() as conn:                    # noqa: SLF001
            conn.execute("UPDATE nodes SET lease_expires=? WHERE run_id=?",
                         (time.time() - 1, run_id))

        result = self.runner().run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())


_WORKER_A = r'''
import sys
sys.path.insert(0, {repo!r})
from dobby.runtime import ArtifactContract, Runner, TaskGraph, TaskNode

graph = TaskGraph([TaskNode(node_id="a", kind="a", depends_on=[],
                            worker="command", contract=ArtifactContract(output_schema=dict(type="object")),
                            config={{"command": {command!r}}})])
runner = Runner({repo_dir!r}, data_dir={data!r})
run_id = runner.start("two workers", graph)
with open({idfile!r}, "w") as handle:
    handle.write(run_id)
runner.run(run_id)
'''


class TwoRealWorkers(unittest.TestCase):
    """The guarantee, with an actual second process rather than a stand-in.

    Scoped to the node lease on purpose. Two workers driving the same run's
    scheduler concurrently also contend for the RUN state, and that arbitration
    is a separate mechanism this change did not touch.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.marker = os.path.join(self.tmp.name, "marker.txt")
        self.release = os.path.join(self.tmp.name, "release.txt")
        self.idfile = os.path.join(self.tmp.name, "run_id.txt")
        # Writes one line, then blocks until the test releases it. The line
        # COUNT is the measurement: a node that ran twice cannot hide from it.
        self.command = (
            f'{sys.executable} -c "import os, time;'
            f' open(r\'{self.marker}\', \'a\', encoding=\'utf-8\')'
            f'.write(\'ran\\n\');'
            f' [time.sleep(0.05) for _ in range(2400)'
            f'  if not os.path.exists(r\'{self.release}\')]"')

    def tearDown(self):
        self.tmp.cleanup()

    def _marker_lines(self):
        if not os.path.exists(self.marker):
            return 0
        with open(self.marker, encoding="utf-8") as handle:
            return len([line for line in handle if line.strip()])

    def test_a_second_worker_does_not_take_a_node_the_first_is_running(self):
        script = _WORKER_A.format(repo=REPO, repo_dir=self.tmp.name,
                                  data=self.data, command=self.command,
                                  idfile=self.idfile)
        extra = {} if os.name == "nt" else {"start_new_session": True}
        with subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **extra) as worker_a:
            try:
                deadline = time.monotonic() + 120
                while self._marker_lines() < 1 and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(self._marker_lines(), 1,
                                 "worker A never started the node")

                with open(self.idfile, encoding="utf-8") as handle:
                    run_id = handle.read().strip()

                # Worker B attaches. Its recovery pass is the code under test:
                # the attempt is open, and its holder is alive.
                worker_b = Runner(self.tmp.name, data_dir=self.data)
                graph = worker_b.store.load_run(run_id)["graph"]
                notes = worker_b._reconcile(run_id, graph)   # noqa: SLF001

                self.assertTrue(any("still held by" in n for n in notes), notes)
                self.assertTrue(
                    worker_b.store.open_attempts(run_id),
                    "worker B closed an attempt belonging to a live worker")
                lease = worker_b.store.node_lease(run_id, "a")
                self.assertTrue(lease["held"], lease)
                self.assertIn(lease["state"],
                              (G.LEASED, G.NODE_RUNNING, G.VERIFYING))
            finally:
                with open(self.release, "w", encoding="utf-8") as handle:
                    handle.write("go")
                worker_a.wait(timeout=180)

        store = RunStore(self.data)
        with open(self.idfile, encoding="utf-8") as handle:
            run_id = handle.read().strip()
        self.assertEqual(worker_a.returncode, 0)
        self.assertEqual(self._marker_lines(), 1,
                         "the node ran more than once across two workers")
        self.assertEqual(len(store.attempts(run_id, "a")), 1)
        self.assertEqual(store.load_run(run_id)["state"], G.SUCCEEDED)


class EffectReconciliation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self):
        return Runner(self.tmp.name,
                      data_dir=os.path.join(self.tmp.name, ".dobby"),
                      sleep=lambda _s: None)

    def _crashed_after_claiming(self):
        """A run that recorded the intent to act and never said what happened."""
        runner = self.runner()
        run_id = runner.start("t", TaskGraph([
            static_node("send", {"ok": True},
                        side_effect=EXTERNAL_REVERSIBLE)]))
        key = idempotency_key(run_id, "send")
        runner.store.claim_effect(key, run_id, "send", "1")
        return runner, run_id, key

    def test_status_distinguishes_claimed_from_confirmed(self):
        runner, run_id, key = self._crashed_after_claiming()
        self.assertEqual(runner.store.effect_status(key), EFFECT_CLAIMED)
        runner.store.confirm_effect(key, result_digest="42")
        self.assertEqual(runner.store.effect_status(key), EFFECT_CONFIRMED)
        self.assertIsNone(runner.store.effect_status("never-claimed"))

    def test_an_unconfirmed_effect_blocks_instead_of_reporting_success(self):
        runner, run_id, _key = self._crashed_after_claiming()

        result = self.runner().run(run_id)

        self.assertNotEqual(result.steps[0].state, G.NODE_SUCCEEDED,
                            result.to_dict())
        self.assertEqual(result.steps[0].state, G.BLOCKED_ON_APPROVAL)
        self.assertEqual(result.state, G.WAITING)
        self.assertEqual(result.steps[0].failure["class"], "POLICY_BLOCKED")

    def test_the_blocked_run_names_both_ways_out(self):
        runner, run_id, _key = self._crashed_after_claiming()
        result = self.runner().run(run_id)
        resolve = result.steps[0].failure["evidence"]["resolve_with"]
        self.assertEqual(sorted(resolve),
                         ["RunStore.confirm_effect", "RunStore.release_effect"])
        self.assertEqual(len(result.unconfirmed_effects), 1)

    def test_confirming_it_finishes_the_node_without_repeating_the_effect(self):
        """The operator checked: it DID happen."""
        runner, run_id, key = self._crashed_after_claiming()
        self.runner().run(run_id)

        runner.store.confirm_effect(key, result_digest="observed-externally")
        result = self.runner().run(run_id)

        self.assertEqual(result.steps[0].state, G.NODE_SUCCEEDED,
                         result.to_dict())
        self.assertEqual(len(runner.store.effects(run_id)), 1)
        self.assertTrue(any("idempotent no-op" in str(e["payload"])
                            for e in runner.store.events(run_id)
                            if e["kind"] == "node_state"))

    def test_releasing_it_lets_the_node_perform_the_effect_after_all(self):
        """The operator checked: it did NOT happen."""
        runner, run_id, key = self._crashed_after_claiming()
        self.runner().run(run_id)

        self.assertTrue(runner.store.release_effect(key, reason="checked the "
                                                                "outbox; no "
                                                                "message"))
        self.assertIsNone(runner.store.effect_status(key))

        result = self.runner().run(run_id)
        self.assertEqual(result.steps[0].state, G.NODE_SUCCEEDED,
                         result.to_dict())
        # Claimed again, and this time carried through to confirmed.
        self.assertEqual(runner.store.effect_status(key), EFFECT_CONFIRMED)
        self.assertEqual(result.unconfirmed_effects, [])

    def test_a_confirmed_effect_cannot_be_released(self):
        runner, _run_id, key = self._crashed_after_claiming()
        runner.store.confirm_effect(key, result_digest="1")
        with self.assertRaises(StoreError) as caught:
            runner.store.release_effect(key, reason="I would like a second one")
        self.assertIn("CONFIRMED", str(caught.exception))

    def test_releasing_something_never_claimed_is_false_not_an_error(self):
        runner, _run_id, _key = self._crashed_after_claiming()
        self.assertFalse(runner.store.release_effect("nope", reason="x"))


if __name__ == "__main__":
    unittest.main()
