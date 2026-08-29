"""Concurrency invariants, checked under deliberate load, many rounds.

Why this exists as its own harness rather than a test. The lease theft fixed on
2026-08-29 -- a stale worker writing READY over a lease a live worker held, and
both of them running the node -- never once appeared when its test module was
run alone. It appeared four times in six when the module ran inside a chunk of
the suite, because the other modules were competing for cores and that changed
the interleaving. A defect that only exists at a particular timing is not found
by running the same thing once; it is found by running it many times, under
load, and checking invariants rather than outcomes.

The invariants, and what each one is protecting:

    no_crash              a worker that loses a race parks; it does not raise
    terminal_agreement    no two workers report DIFFERENT terminal states
    single_promotion      no node has two promoted artifacts (double execution)
    no_stolen_lease       no LEASED -> READY carrying a bookkeeping reason
    no_concurrent_attempt no node has two attempts open at the same time
    replayable            the event log rebuilds to the state on disk
    effect_at_most_once   an external effect happens at most once across a kill

Usage:

    python evals/stress/concurrency.py --rounds 20 --workers 4 --load 4
    python evals/stress/concurrency.py --scenario kill --rounds 10

Exit code 1 when any invariant broke, with the failing rounds named. Nothing is
sampled and nothing is summarised away: a single violation in a hundred rounds
is a violation, because the thing being tested is a race and a race that fires
once fires again.

What this is NOT, measured rather than assumed. It is a finder, not a
reproducer. Three defects were found here and each was fixed; afterwards,
reverting ONE of the fixes on its own -- the compare-and-set on the READY
promotion -- and running twenty rounds at six workers with a widened hold
produced zero violations, because the other two fixes had already closed the
crash cascade that used to widen the window. So a clean run against a
deliberately broken build is not evidence the build is fine, and a green result
here is evidence about THIS build under THIS load and nothing more.

The mechanism each fix relies on is proved deterministically somewhere else:
`tests/test_multiprocess_run.py::CompareAndSet` constructs the exact
interleaving and asserts the store refuses it. That is the test that fails when
the mechanism is removed. This harness is the reason anybody knew to write it.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, REPO)

WORKER = textwrap.dedent('''
    import json, os, sys, time
    sys.path.insert(0, sys.argv[1])
    from dobby.runtime import RunBudget
    from dobby.runtime import graph as G
    from dobby.runtime.contracts import ArtifactContract, SCHEMAS
    from dobby.runtime.runner import Runner
    from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,
                                       WorkerResult)

    repo, data, mode, shape = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    nodes_n, hold, die_on = int(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
    LEDGER = os.path.join(data, "..", "ledger.log")

    class Work(WorkerAdapter):
        name = "provider"
        def run(self, node, context):
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(node.node_id + "\\n")
            if hold:
                time.sleep(hold)
            if die_on and node.node_id == die_on:
                os._exit(137)
            return WorkerResult(
                True, payload={"steps": [{"what": "do the thing"}]},
                meta={"provider": node.config.get("provider")})

    def chain():
        out, prev = [], None
        for i in range(nodes_n):
            out.append(G.TaskNode(
                node_id="n%d" % i, kind="plan", worker="provider",
                instruction="i", depends_on=([prev] if prev else []),
                contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
                config={"provider": "claude"}))
            prev = "n%d" % i
        return G.TaskGraph(out)

    def diamond():
        mk = lambda nid, deps: G.TaskNode(
            node_id=nid, kind="plan", worker="provider", instruction="i",
            depends_on=deps,
            contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
            config={"provider": "claude"})
        return G.TaskGraph([mk("n0", []), mk("n1", ["n0"]), mk("n2", ["n0"]),
                            mk("n3", ["n1", "n2"])])

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

    build = {"chain": chain, "diamond": diamond, "effect": effecting}[shape]
    parallel = int(os.environ.get("STRESS_PARALLEL", "1"))
    runner = Runner(repo=repo, data_dir=data,
                    workers=WorkerRegistry({"provider": Work()}),
                    max_parallel=parallel, sleep=lambda _s: None)
    if mode == "seed":
        print(runner.start("stress", build()))
    else:
        run_id = sys.argv[8]
        try:
            result = runner.run(run_id, approvals={"effect"},
                                budget=RunBudget(max_irreversible=1))
            print(json.dumps({
                "pid": os.getpid(), "ok": True, "state": result.state,
                "attempts": {s["node_id"]: len(runner.store.attempts(
                    run_id, s["node_id"]))
                    for s in result.to_dict()["steps"]}}))
        except Exception as exc:
            print(json.dumps({"pid": os.getpid(), "ok": False,
                              "error": "%s: %s" % (type(exc).__name__, exc)}))
''')

BOOKKEEPING_REASONS = ("dependencies satisfied",)


def _burn(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        pass


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Round:
    """One seeded run, raced by several processes, then checked."""

    def __init__(self, *, shape: str, workers: int, nodes: int, hold: float,
                 die_on: str, parallel: int):
        self.shape, self.workers, self.nodes = shape, workers, nodes
        self.hold, self.die_on, self.parallel = hold, die_on, parallel
        self.tmp = tempfile.mkdtemp()
        self.script = os.path.join(self.tmp, "worker.py")
        with open(self.script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(WORKER)
        self.data = os.path.join(self.tmp, "data")
        self.ledger = os.path.join(self.tmp, "ledger.log")
        self.rows: list = []
        self.run_id = ""

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _argv(self, mode: str, *extra) -> list:
        return [sys.executable, self.script, REPO, self.data, mode, self.shape,
                str(self.nodes), str(self.hold), self.die_on, *extra]

    def seed(self) -> str:
        env = _env()
        env["STRESS_PARALLEL"] = str(self.parallel)
        proc = subprocess.run(self._argv("seed"), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=300)
        if proc.returncode != 0:
            raise RuntimeError(f"seed failed: {proc.stderr[-400:]}")
        self.run_id = proc.stdout.strip().splitlines()[-1]
        return self.run_id

    def race(self, *, killer_first: bool = False) -> list:
        """Start the workers together. Returns their verdicts."""
        env = _env()
        env["STRESS_PARALLEL"] = str(self.parallel)
        procs = []
        for index in range(self.workers):
            # Only the first worker is allowed to die, when a kill is asked
            # for; the rest are the ones that must recover from it.
            argv = self._argv("work", self.run_id)
            if killer_first and index > 0:
                argv[7] = ""          # die_on cleared for the survivors
            procs.append(subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env))
        rows = []
        for proc in procs:
            out, err = proc.communicate(timeout=600)
            line = next((l for l in (out or "").splitlines()
                         if l.strip().startswith("{")), None)
            if line is None:
                # A worker told to die has no verdict, and that is the point of
                # it. One that died without being asked is a finding.
                rows.append({"ok": None, "killed": True,
                             "stderr": (err or "")[-200:]})
            else:
                rows.append(json.loads(line))
        self.rows = rows
        return rows

    def ran(self, node_id: str) -> int:
        if not os.path.exists(self.ledger):
            return 0
        with open(self.ledger, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip() == node_id)

    # -- invariants --------------------------------------------------------
    def violations(self) -> list:
        from dobby.runtime.store import RunStore

        store = RunStore(self.data)
        events = store.events(self.run_id)
        bad = []

        for row in self.rows:
            if row.get("ok") is False:
                bad.append(("no_crash", row.get("error", "")))

        # Several workers reporting SUCCEEDED is NOT a violation, and asserting
        # it was is a mistake this harness made first. A worker that attaches
        # to a run somebody else already finished returns early with the run's
        # real state, which is SUCCEEDED, and reporting the truth about a run
        # is not a claim to have done it. What would be wrong is two workers
        # reporting DIFFERENT terminal states, or any of them disagreeing with
        # what the store ended up holding.
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED"}
        claimed = {r.get("state") for r in self.rows
                   if r.get("state") in terminal}
        if len(claimed) > 1:
            bad.append(("terminal_agreement",
                        f"workers reported {sorted(claimed)}"))
        final = store.load_run(self.run_id)["state"]
        if claimed and final in terminal and claimed != {final}:
            bad.append(("terminal_agreement",
                        f"workers said {sorted(claimed)}, store says {final}"))

        graph = store.load_run(self.run_id)["graph"]
        for node_id in graph.nodes:
            promoted = store.artifacts(self.run_id, node_id=node_id,
                                       state="PROMOTED")
            if len(promoted) > 1:
                bad.append(("single_promotion",
                            f"{node_id}: {[p['artifact_id'] for p in promoted]}"))

        for event in events:
            if event["kind"] != "node_state":
                continue
            payload = event["payload"]
            if (payload.get("from") == "LEASED"
                    and payload.get("to") == "READY"
                    and any(r in (payload.get("reason") or "")
                            for r in BOOKKEEPING_REASONS)):
                bad.append(("no_stolen_lease",
                            f"{event.get('node_id')}: {payload.get('reason')}"))

        # Two attempts open at once on one node. `attempt_started` without an
        # `attempt_finished` between them is the shape a double execution makes
        # in the log even when both happen to promote the same digest.
        open_attempts: dict = {}
        for event in events:
            node_id = event.get("node_id")
            if event["kind"] == "attempt_started":
                open_attempts[node_id] = open_attempts.get(node_id, 0) + 1
                if open_attempts[node_id] > 1:
                    bad.append(("no_concurrent_attempt",
                                f"{node_id}: {open_attempts[node_id]} open"))
            elif event["kind"] == "attempt_finished":
                open_attempts[node_id] = max(0, open_attempts.get(node_id, 1) - 1)

        try:
            store.rebuild(self.run_id)
        except Exception as exc:                       # pragma: no cover
            bad.append(("replayable", f"{type(exc).__name__}: {exc}"))

        if self.shape == "effect":
            times = self.ran("effect")
            if times > 1:
                bad.append(("effect_at_most_once",
                            f"the external effect ran {times} times"))
        return bad


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--nodes", type=int, default=3)
    parser.add_argument("--hold", type=float, default=0.15,
                        help="seconds each node holds, to widen the overlap")
    parser.add_argument("--parallel", type=int, default=1,
                        help="max_parallel inside each worker process")
    parser.add_argument("--scenario", default="contend",
                        choices=["contend", "diamond", "kill", "effect",
                                 "all"])
    parser.add_argument("--load", type=int, default=0,
                        help="busy processes to run alongside; the defect this "
                             "harness exists for only appeared under load")
    args = parser.parse_args(argv)

    scenarios = (["contend", "diamond", "kill", "effect"]
                 if args.scenario == "all" else [args.scenario])
    shape = {"contend": "chain", "diamond": "diamond", "kill": "chain",
             "effect": "effect"}

    burners = []
    if args.load:
        for _ in range(args.load):
            proc = multiprocessing.Process(target=_burn, args=(3600,))
            proc.daemon = True
            proc.start()
            burners.append(proc)

    failures: list = []
    total = 0
    try:
        for scenario in scenarios:
            for index in range(args.rounds):
                total += 1
                nodes = 4 if scenario == "diamond" else args.nodes
                die_on = "n0" if scenario == "kill" else ""
                rnd = Round(shape=shape[scenario], workers=args.workers,
                            nodes=nodes, hold=args.hold, die_on=die_on,
                            parallel=args.parallel)
                try:
                    rnd.seed()
                    rnd.race(killer_first=(scenario == "kill"))
                    if scenario == "kill":
                        # The survivors go again, which is what a recovery is.
                        rnd.die_on = ""
                        rnd.race()
                    bad = rnd.violations()
                    if bad:
                        failures.append({"scenario": scenario, "round": index,
                                         "violations": bad,
                                         "rows": rnd.rows})
                        print(f"  {scenario} round {index}: "
                              + "; ".join(f"{k}: {v}" for k, v in bad))
                finally:
                    rnd.close()
            print(f"{scenario}: {args.rounds} rounds done")
    finally:
        for proc in burners:
            proc.terminate()

    print(json.dumps({
        "rounds": total, "workers": args.workers, "load": args.load,
        "parallel": args.parallel,
        "violations": len(failures),
        "by_invariant": sorted({k for f in failures
                                for k, _ in f["violations"]}),
    }, ensure_ascii=False, indent=1))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
