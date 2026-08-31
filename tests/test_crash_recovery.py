"""A worker killed mid-node, and what the next one is allowed to assume.

`test_runtime_lease.py` covers this with a simulated dead owner: a lease row
written with a PID that is not running. That is the right unit test and it
cannot produce the state a real kill leaves behind -- an open attempt, a held
lease, a claimed effect, and no `finally` having run, because `os._exit` does
not unwind.

These tests kill real subprocesses with `os._exit(137)` and then ask a fresh
process what it does with the wreckage. Four paths, and the last two are the
ones that decide whether this is safe to point at anything outside the repo:

  1. killed mid-node, no external effect     -> recovered and retried
  2. killed after an irreversible effect     -> NOT repeated, NOT reported done
  3. operator confirms it happened           -> finishes without repeating it
  4. operator releases it as never happening -> runs it again, deliberately

Measured while writing this, and worth recording because it nearly went in as a
defect report: the effect ledger read 2 after the confirm-and-resume, which
looks exactly like an at-most-once violation. It was not. The probe wrote a
line for EVERY node and the second line was the downstream node, not a repeat.
The count was right and the question was wrong.
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
from dobby.runtime.store import RunStore  # noqa: E402

WORKER = textwrap.dedent('''
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from dobby.runtime import RunBudget
    from dobby.runtime import graph as G
    from dobby.runtime.contracts import ArtifactContract, SCHEMAS
    from dobby.runtime.runner import Runner
    from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,
                                       WorkerResult)

    repo, data, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    LEDGER = os.path.join(data, "..", "ledger.log")

    class Worker(WorkerAdapter):
        """Records every node it runs, and dies on one if told to."""
        name = "provider"
        def __init__(self, die_on): self.die_on = die_on
        def run(self, node, context):
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(node.node_id + "\\n")
            if node.node_id == self.die_on:
                os._exit(137)          # no finally, no atexit, no lease release
            return WorkerResult(
                True, payload={"steps": [{"what": "do the thing"}]},
                meta={"provider": node.config.get("provider")})

    def plain():
        nodes, prev = [], None
        for i in range(3):
            nodes.append(G.TaskNode(
                node_id="n%d" % i, kind="plan", worker="provider",
                instruction="i", depends_on=([prev] if prev else []),
                contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
                config={"provider": "claude"}))
            prev = "n%d" % i
        return G.TaskGraph(nodes)

    def effecting():
        return G.TaskGraph([
            G.TaskNode(node_id="effect", kind="execute", worker="provider",
                instruction="send it",
                contract=ArtifactContract(
                    output_schema=SCHEMAS["plan"],
                    side_effect_class="EXTERNAL_IRREVERSIBLE"),
                config={"provider": "claude"}),
            G.TaskNode(node_id="after", kind="plan", worker="provider",
                instruction="i", depends_on=["effect"],
                contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
                config={"provider": "claude"})])

    shape = plain if sys.argv[4] == "plain" else effecting
    die_on = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] != "-" else None
    runner = Runner(repo=repo, data_dir=data,
                    workers=WorkerRegistry({"provider": Worker(die_on)}),
                    available_providers={"claude", "codex", "gemini"},
                    sleep=lambda _s: None)

    if mode == "seed":
        print(runner.start("t", shape()))
    else:
        run_id = sys.argv[6]
        result = runner.run(run_id, approvals={"effect"},
                            budget=RunBudget(max_irreversible=1))
        print(json.dumps({
            "state": result.state,
            "attempts": {s["node_id"]: len(runner.store.attempts(run_id,
                                                                 s["node_id"]))
                         for s in result.to_dict()["steps"]},
            "unconfirmed": [u["node_id"]
                            for u in result.to_dict()["unconfirmed_effects"]],
            "notes": result.to_dict()["notes"]}, ensure_ascii=False))
''')


class CrashCase(unittest.TestCase):
    SHAPE = "plain"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "worker.py")
        with open(self.script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WORKER)
        self.data = os.path.join(self.tmp, "data")
        self.ledger = os.path.join(self.tmp, "ledger.log")

    def call(self, mode, *extra, expect_death=False):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, self.script, REPO, self.data, mode, self.SHAPE,
             *extra],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=300)
        if expect_death:
            self.assertNotEqual(proc.returncode, 0,
                                "the worker was supposed to die")
            return None
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip().splitlines()[-1]
        return json.loads(line) if line.startswith("{") else line

    def ran(self, node_id):
        """How many times a node's worker actually executed."""
        if not os.path.exists(self.ledger):
            return 0
        with open(self.ledger, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip() == node_id)


class KilledMidNode(CrashCase):
    def test_a_fresh_worker_recovers_and_finishes(self):
        run_id = self.call("seed", "-")
        self.call("run", "n1", run_id, expect_death=True)

        state = RunStore(self.data).load_run(run_id)
        self.assertEqual(state["graph"].nodes["n1"].state, G.NODE_RUNNING,
                         "the kill should leave the node mid-flight")

        result = self.call("run", "-", run_id)
        self.assertEqual(result["state"], G.SUCCEEDED, result)
        self.assertEqual(result["attempts"]["n1"], 2,
                         "the interrupted attempt plus the recovery")
        self.assertEqual(result["attempts"]["n0"], 1,
                         "an already-finished node must not be redone")

    def test_the_recovery_is_reported_and_not_silent(self):
        run_id = self.call("seed", "-")
        self.call("run", "n1", run_id, expect_death=True)
        result = self.call("run", "-", run_id)
        self.assertTrue(any("interrupted" in n for n in result["notes"]),
                        result["notes"])


class KilledAfterAnIrreversibleEffect(CrashCase):
    SHAPE = "effecting"

    def crash(self):
        run_id = self.call("seed", "-")
        self.call("run", "effect", run_id, expect_death=True)
        self.assertEqual(self.ran("effect"), 1)
        return run_id

    def unconfirmed(self, run_id):
        return RunStore(self.data).unconfirmed_effects(run_id)

    def test_recovery_neither_repeats_it_nor_calls_it_done(self):
        """The property that decides whether this may point at anything real."""
        run_id = self.crash()
        result = self.call("run", "-", run_id)
        self.assertEqual(self.ran("effect"), 1, "the effect was repeated")
        self.assertNotEqual(result["state"], G.SUCCEEDED)
        self.assertEqual(result["unconfirmed"], ["effect"])

    def test_the_block_names_the_key_and_both_ways_out(self):
        run_id = self.crash()
        result = self.call("run", "-", run_id)
        note = " ".join(result["notes"])
        self.assertIn("UNKNOWN", note)
        self.assertIn("confirm_effect", note)
        self.assertIn("release_effect", note)

    def test_confirming_finishes_the_run_without_repeating_the_effect(self):
        run_id = self.crash()
        self.call("run", "-", run_id)
        store = RunStore(self.data)
        store.confirm_effect(self.unconfirmed(run_id)[0]["idempotency_key"],
                             result_digest="sent-ok")

        result = self.call("run", "-", run_id)
        self.assertEqual(result["state"], G.SUCCEEDED, result)
        self.assertEqual(self.ran("effect"), 1,
                         "confirmed means it happened; doing it again is the "
                         "whole failure this machinery prevents")
        self.assertEqual(self.ran("after"), 1, "the run did finish")

    def test_releasing_it_runs_the_node_again_on_purpose(self):
        run_id = self.crash()
        self.call("run", "-", run_id)
        store = RunStore(self.data)
        store.release_effect(self.unconfirmed(run_id)[0]["idempotency_key"],
                             reason="checked: it never reached the outside")

        result = self.call("run", "-", run_id)
        self.assertEqual(result["state"], G.SUCCEEDED, result)
        self.assertEqual(self.ran("effect"), 2,
                         "released means it did not happen, so it must")

    def test_releasing_demands_a_reason(self):
        """Declaring that an external effect never happened is a claim somebody
        makes, so the API will not let it be made silently."""
        run_id = self.crash()
        self.call("run", "-", run_id)
        store = RunStore(self.data)
        key = self.unconfirmed(run_id)[0]["idempotency_key"]
        with self.assertRaises(TypeError):
            store.release_effect(key)


if __name__ == "__main__":
    unittest.main()
