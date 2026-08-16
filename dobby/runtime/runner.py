"""The control loop: plan -> execute -> verify -> report, durably.

This is the part that was missing. The harness could already route a task,
fan work out to several providers, isolate mutating agents in worktrees, judge
an output and record a trajectory — every primitive, and no loop that closes
over them. So the connective tissue was a person: run this command, read the
output, decide, run the next one. A person is a fine orchestrator right up to
the moment the work takes longer than their attention, at which point the run
has no state anybody can resume and no record of what it already did.

What the loop guarantees
------------------------
1. **A killed process does not repeat work.** Every state change is committed
   to the store before the next one begins, so `resume` starts from what
   actually happened rather than from what the plan expected.
2. **An unverified artifact is not an input.** A node reads only the PROMOTED
   outputs of its dependencies. This is enforced when the prompt is built, not
   requested in the prompt.
3. **An external effect happens at most once.** The idempotency key is claimed
   before the effect and survives the crash that would otherwise cause the
   second one.
4. **A failure is classified before it is retried.** See `failures.py`; the
   class picks the action, the count only bounds it.

What it does not do yet, stated rather than implied: there is no provider
scoring, no hedging, no parallel node execution. The store records what those
will need. Adding them without the data would be fitting a policy to nothing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from . import graph as G
from .contracts import (Artifact, ArtifactContract, PROMOTED, REJECTED,
                        SCHEMAS, VERIFIED, artifact_path, idempotency_key)
from .failures import (DEFAULT_POLICY, Failure, REPAIR, RETRY_ELSEWHERE,
                       RETRY_SAME, TRANSIENT_PROVIDER, WAIT, backoff_delay)
from .scheduler import BudgetExceeded, RunBudget, Scheduler
from .store import RunStore
from .verify import Verifier, promotable
from .workers import WorkerRegistry


@dataclass
class StepReport:
    """One node's outcome, in the shape a report renders."""

    node_id: str
    kind: str
    state: str
    attempts: int
    worker: str
    artifact_id: str | None = None
    verdict: dict | None = None
    failure: dict | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "kind": self.kind, "state": self.state,
                "attempts": self.attempts, "worker": self.worker,
                "artifact_id": self.artifact_id, "verdict": self.verdict,
                "failure": self.failure, "duration_s": self.duration_s}


@dataclass
class RunResult:
    run_id: str
    state: str
    steps: list[StepReport] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    #: Effects claimed and never confirmed — the visible residue of a crash
    #: between claiming and acting. Reported loudly: this is the one condition
    #: where a human must check the outside world before resuming.
    unconfirmed_effects: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "state": self.state,
                "steps": [s.to_dict() for s in self.steps],
                "deferred": list(self.deferred), "budget": dict(self.budget),
                "unconfirmed_effects": list(self.unconfirmed_effects),
                "notes": list(self.notes)}


class Runner:
    """Executes a `TaskGraph` against a `RunStore` until it cannot proceed."""

    def __init__(self, repo: str, *, data_dir: str | None = None,
                 workers: WorkerRegistry | None = None,
                 policy: dict | None = None,
                 sleep=time.sleep):
        self.repo = os.path.abspath(repo)
        self.data_dir = data_dir or os.path.join(self.repo, ".dobby")
        self.store = RunStore(self.data_dir)
        self.workers = workers or WorkerRegistry()
        self.policy = policy or DEFAULT_POLICY
        #: Injected so tests do not pay real backoff seconds. A retry policy
        #: nobody tests because the test is slow is a retry policy nobody tests.
        self._sleep = sleep

    # -- starting ----------------------------------------------------------
    def start(self, task: str, task_graph: "G.TaskGraph", *,
              budget: RunBudget | None = None, route: dict | None = None,
              run_id: str | None = None) -> str:
        budget = budget or RunBudget()
        return self.store.create_run(
            task, task_graph, run_id=run_id, budget=budget.to_dict(),
            route=route or {}, repo=self.repo)

    # -- the loop ----------------------------------------------------------
    def run(self, run_id: str, *, budget: RunBudget | None = None,
            approvals: set | None = None, max_steps: int = 100) -> RunResult:
        """Drive `run_id` forward until it finishes, blocks, or runs out.

        Safe to call on a run that is already partly done: that IS resume. There
        is no separate resume path on purpose — a recovery route that differs
        from the normal one is a route that only executes after a crash, which
        is when it is least affordable for it to be wrong.
        """
        state = self.store.load_run(run_id)
        task_graph: G.TaskGraph = state["graph"]
        notes = self._reconcile(run_id, task_graph)

        budget = budget or RunBudget.from_dict(state["budget"])
        # Attempts already recorded count against the ceiling. Reading them from
        # the store rather than from the serialized budget is what makes the
        # ceiling survive a crash.
        budget.attempts_spent = len(self.store.attempts(run_id))
        scheduler = Scheduler(budget, approvals=approvals)

        if state["state"] in G.RUN_TERMINAL:
            return self._report(run_id, task_graph, [], budget,
                                notes + [f"run is already {state['state']}"])
        if state["state"] != G.RUNNING:
            self.store.set_run_state(run_id, G.RUNNING,
                                     reason="runner attached")

        deferred: list[dict] = []
        for _ in range(max_steps):
            self._skip_unreachable(run_id, task_graph)
            if task_graph.done():
                break
            decisions, deferred = scheduler.next_nodes(task_graph, limit=1)
            if not decisions:
                break
            self._execute_node(run_id, task_graph,
                               task_graph.nodes[decisions[0].node_id], budget)

        self._skip_unreachable(run_id, task_graph)
        final = self._finalize(run_id, task_graph, deferred)
        return self._report(run_id, task_graph, deferred, budget, notes,
                            state=final)

    # -- crash reconciliation ----------------------------------------------
    def _reconcile(self, run_id: str, task_graph: "G.TaskGraph") -> list[str]:
        """Close attempts that started and never finished, and free their nodes.

        This is the whole of crash recovery, and it is three lines of intent:
        an attempt with no finish means the process died holding it; the node
        goes back to READY; the attempt is recorded as a retryable failure so
        the count is honest. Nothing is re-run that finished.
        """
        notes: list[str] = []
        for row in self.store.open_attempts(run_id):
            self.store.finish_attempt(
                run_id, row["node_id"], row["attempt"],
                outcome=G.RETRYABLE_FAILURE, failure_class=TRANSIENT_PROVIDER,
                detail="the process holding this attempt exited before it "
                       "finished; recovered on resume")
            node = task_graph.nodes.get(row["node_id"])
            if node is not None and node.state not in G.NODE_TERMINAL:
                self._set_node(run_id, task_graph, node, G.READY,
                               reason="lease recovered after an interrupted "
                                      "attempt", enforce=False)
            notes.append(
                f"{row['node_id']}: attempt {row['attempt']} was interrupted "
                f"and has been recovered")
        for effect in self.store.unconfirmed_effects(run_id):
            notes.append(
                f"{effect['node_id']}: an external effect was claimed and never "
                f"confirmed (key {effect['idempotency_key'][:12]}…). It will NOT "
                f"be attempted again. Check the outside world before deciding "
                f"the run succeeded.")
        return notes

    # -- one node ----------------------------------------------------------
    def _execute_node(self, run_id: str, task_graph: "G.TaskGraph", node,
                      budget: RunBudget) -> None:
        if node.state == G.PENDING:
            self._set_node(run_id, task_graph, node, G.READY,
                           reason="dependencies satisfied")
        if not self.store.lease_node(run_id, node.node_id, holder=str(os.getpid())):
            # Another process took it. Reload so this one is not deciding from
            # a stale graph.
            node.state = self.store.load_run(run_id)["graph"].nodes[
                node.node_id].state
            return
        node.state = G.LEASED

        attempt = self.store.next_attempt_number(run_id, node.node_id)
        self.store.start_attempt(run_id, node.node_id, attempt,
                                 worker=node.worker)
        node.attempts = attempt
        self._set_node(run_id, task_graph, node, G.NODE_RUNNING,
                       reason=f"attempt {attempt}")
        budget.charge(node)

        started = time.monotonic()
        claimed_key = self._claim_effect(run_id, node, attempt)
        if claimed_key is False:
            # The effect is already done. The node is finished by definition:
            # re-running it would be the duplicate this key exists to prevent.
            self.store.finish_attempt(
                run_id, node.node_id, attempt, outcome=G.FINISHED,
                detail="external effect already performed under this "
                       "idempotency key; node satisfied without repeating it")
            self._set_node(run_id, task_graph, node, G.VERIFYING,
                           reason="effect already applied")
            self._set_node(run_id, task_graph, node, G.NODE_SUCCEEDED,
                           reason="idempotent no-op")
            return

        context = {"repo": self.repo, "attempt": attempt,
                   "inputs": self._promoted_inputs(run_id, task_graph, node),
                   "run_id": run_id}
        try:
            result = self.workers.get(node.worker).run(node, context)
        except Exception as exc:  # noqa: BLE001 - an adapter bug is a run event
            result = None
            failure = Failure("NON_RETRYABLE",
                              f"worker {node.worker!r} raised "
                              f"{type(exc).__name__}: {exc}")
        else:
            failure = result.failure

        duration = round(time.monotonic() - started, 2)
        if result is None or not result.ok:
            self._fail_attempt(run_id, task_graph, node, attempt, failure,
                               duration)
            return

        if claimed_key:
            self.store.confirm_effect(claimed_key,
                                      result_digest=str(len(result.raw)))

        # -- the gate ------------------------------------------------------
        self._set_node(run_id, task_graph, node, G.VERIFYING,
                       reason="worker returned; running acceptance checks")
        verifier = Verifier(self.repo,
                            log_dir=os.path.join(self.data_dir, "state",
                                                 "runtime", run_id, "logs"))
        verdict = verifier.verify(node.contract, result.payload,
                                  node_id=node.node_id)
        artifact = self._store_artifact(run_id, node, result, verdict)

        if not promotable(node.contract, verdict):
            artifact.transition(REJECTED)
            self._write_artifact_file(run_id, artifact)
            self.store.put_artifact(artifact)
            self._fail_attempt(run_id, task_graph, node, attempt,
                               verdict.failure, duration,
                               verdict=verdict.to_dict())
            return

        artifact.transition(VERIFIED).transition(PROMOTED)
        # Rewritten AFTER the transition. The file is what a later node reads,
        # and a file that says PROPOSED while the store says PROMOTED is two
        # answers to the one question this whole gate exists to answer.
        path = self._write_artifact_file(run_id, artifact)
        self.store.put_artifact(artifact, path=path)
        self.store.finish_attempt(run_id, node.node_id, attempt,
                                  outcome=G.FINISHED,
                                  detail=f"verified in {duration}s")
        self._set_node(run_id, task_graph, node, G.NODE_SUCCEEDED,
                       reason="every acceptance check passed")

    # -- failure handling ---------------------------------------------------
    def _fail_attempt(self, run_id: str, task_graph: "G.TaskGraph", node,
                      attempt: int, failure: Failure | None, duration: float,
                      *, verdict: dict | None = None) -> None:
        failure = failure or Failure("NON_RETRYABLE",
                                     "the worker failed without a class")
        rule = failure.rule(self.policy)
        node.last_failure = failure.to_dict()
        if verdict:
            node.last_failure["verdict"] = verdict

        retryable = (rule.action in (RETRY_SAME, RETRY_ELSEWHERE, REPAIR)
                     and attempt < rule.max_attempts)
        outcome = G.RETRYABLE_FAILURE if retryable else G.PERMANENT_FAILURE
        self.store.finish_attempt(
            run_id, node.node_id, attempt, outcome=outcome,
            failure_class=failure.failure_class,
            detail=f"{failure.detail} [{rule.action}, {duration}s]")

        if rule.action == WAIT:
            self._set_node(run_id, task_graph, node, G.READY,
                           reason="retry after wait", enforce=False)
            self._set_node(run_id, task_graph, node, G.BLOCKED_ON_APPROVAL,
                           reason=failure.detail)
            return
        if not retryable:
            self._set_node(run_id, task_graph, node, G.NODE_FAILED,
                           reason=(f"{failure.failure_class}: {failure.detail} "
                                   f"({rule.rationale})"))
            return

        delay = backoff_delay(rule, attempt)
        if delay:
            self._sleep(delay)
        if rule.action == REPAIR:
            # The next attempt is not a rerun: the failure text goes into the
            # instruction, which is the difference between repairing and
            # rolling the dice again.
            node.config = dict(node.config)
            node.config["repair_context"] = failure.to_dict()
        if rule.avoid_last_provider and node.config.get("provider"):
            node.config = dict(node.config)
            node.config.setdefault("avoid_providers", [])
            node.config["avoid_providers"] = sorted(
                set(node.config["avoid_providers"]) | {node.config["provider"]})
        self._set_node(run_id, task_graph, node, G.READY,
                       reason=f"{rule.action} after {failure.failure_class}",
                       enforce=False)

    # -- helpers -----------------------------------------------------------
    def _claim_effect(self, run_id: str, node, attempt: int):
        """Returns a key (claimed), False (already applied), or None (no effect)."""
        if not node.contract.needs_idempotency_key:
            return None
        key = idempotency_key(run_id, node.node_id,
                              node.contract.effect_version)
        if self.store.claim_effect(key, run_id, node.node_id,
                                   node.contract.effect_version):
            return key
        return False

    def _promoted_inputs(self, run_id: str, task_graph: "G.TaskGraph",
                         node) -> dict:
        """The payloads of dependencies, and ONLY the promoted ones."""
        inputs: dict = {}
        for dep in node.depends_on:
            rows = self.store.artifacts(run_id, node_id=dep, state=PROMOTED)
            if not rows:
                continue
            latest = rows[-1]
            payload = self._read_payload(latest["path"])
            if payload is not None:
                inputs[dep] = payload
                node.contract.input_refs = sorted(
                    set(node.contract.input_refs) | {latest["artifact_id"]})
        return inputs

    @staticmethod
    def _read_payload(path: str):
        if not path or not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("payload")

    def _store_artifact(self, run_id: str, node, result, verdict) -> Artifact:
        artifact = Artifact(
            artifact_id=f"{node.node_id}-{node.attempts or 1}",
            run_id=run_id, node_id=node.node_id,
            kind=node.config.get("schema") or node.kind,
            payload=result.payload,
            # `input_refs` is written HERE and not left on the in-memory
            # contract. The node spec in the store is the immutable definition,
            # so a field the runner fills would vanish on resume — and
            # "reproducible inputs" would be a property of one process's memory.
            evidence={"verdict": verdict.to_dict(), "worker": node.worker,
                      "input_refs": list(node.contract.input_refs),
                      "meta": result.meta})
        self._write_artifact_file(run_id, artifact)
        return artifact

    def _write_artifact_file(self, run_id: str, artifact: Artifact) -> str:
        path = artifact_path(self.data_dir, run_id, artifact.artifact_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(artifact.to_dict(), handle, ensure_ascii=False, indent=1,
                      default=str)
        return path

    def _set_node(self, run_id: str, task_graph: "G.TaskGraph", node,
                  to_state: str, *, reason: str = "",
                  enforce: bool = True) -> None:
        self.store.set_node_state(run_id, node.node_id, to_state, reason=reason,
                                  enforce=enforce)
        node.state = to_state

    def _skip_unreachable(self, run_id: str, task_graph: "G.TaskGraph") -> None:
        for node_id in task_graph.unreachable():
            node = task_graph.nodes[node_id]
            self._set_node(run_id, task_graph, node, G.SKIPPED,
                           reason=task_graph.blocking_reason(node_id),
                           enforce=False)

    def _finalize(self, run_id: str, task_graph: "G.TaskGraph",
                  deferred: list[dict]) -> str:
        current = self.store.load_run(run_id)["state"]
        if current in G.RUN_TERMINAL:
            return current
        if task_graph.succeeded():
            self.store.set_run_state(run_id, G.SUCCEEDED,
                                     reason="every node verified")
            return G.SUCCEEDED
        if task_graph.done():
            self.store.set_run_state(run_id, G.FAILED,
                                     reason="the graph finished with failures")
            return G.FAILED
        blocked = any(n.state == G.BLOCKED_ON_APPROVAL
                      for n in task_graph.nodes.values())
        if blocked or deferred:
            self.store.set_run_state(
                run_id, G.WAITING,
                reason="approval required" if blocked else
                       "; ".join(d["reason"] for d in deferred)[:200])
            return G.WAITING
        # Runnable work left, nothing deferring it: the only way here is the
        # step limit. Left in RUNNING it would read as a run still in progress
        # inside a process that has already returned.
        self.store.set_run_state(
            run_id, G.WAITING,
            reason="step limit reached with work still runnable; resume to "
                   "continue")
        return G.WAITING

    def _report(self, run_id: str, task_graph: "G.TaskGraph",
                deferred: list[dict], budget: RunBudget, notes: list[str],
                *, state: str | None = None) -> RunResult:
        steps = []
        for node_id in task_graph.topological_order():
            node = task_graph.nodes[node_id]
            rows = self.store.artifacts(run_id, node_id=node_id)
            promoted = [r for r in rows if r["state"] == PROMOTED]
            steps.append(StepReport(
                node_id=node_id, kind=node.kind, state=node.state,
                attempts=node.attempts, worker=node.worker,
                artifact_id=promoted[-1]["artifact_id"] if promoted else None,
                failure=node.last_failure))
        return RunResult(
            run_id=run_id, state=state or self.store.load_run(run_id)["state"],
            steps=steps, deferred=deferred, budget=budget.to_dict(),
            unconfirmed_effects=self.store.unconfirmed_effects(run_id),
            notes=notes)


# -- the default graph -------------------------------------------------------

def default_graph(task: str, *, provider: str | None = None,
                  execute_command: str | None = None,
                  acceptance_checks: list[str] | None = None,
                  static: bool = False) -> "G.TaskGraph":
    """plan -> execute -> verify -> report.

    Four nodes, because they are the four claims a run makes and each one can be
    wrong on its own: this is what I intend to do, this is what I did, this is
    the evidence it worked, this is what remains unestablished.

    `verify` is a NODE and not only a gate. The per-node gate proves each
    artifact satisfies its own contract; this one runs the project's own checks
    against the tree afterwards, which is the only thing that catches a set of
    individually valid steps that do not compose.
    """
    if not (static or provider or execute_command):
        raise ValueError(
            "default_graph needs somebody to do the work: pass provider= for "
            "an agent CLI, execute_command= for a deterministic step, or "
            "static=True for a dry run that exercises the kernel only")
    # `static` also covers the case where a command drives execution but the
    # narrative nodes have no provider to write them: a plan node with no worker
    # is a node that fails for a reason that has nothing to do with the task.
    worker = "provider" if provider else "static"
    checks = acceptance_checks or []

    plan = G.TaskNode(
        node_id="plan", kind="plan", worker=worker,
        instruction=(f"Plan the work for this task, in steps that can each be "
                     f"verified:\n\n{task}"),
        contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
        config=_worker_config(worker, provider, schema="plan",
                             payload={"steps": [{"what": task}]}))

    execute = G.TaskNode(
        node_id="execute", kind="execute", depends_on=["plan"],
        worker="command" if execute_command else worker,
        instruction=(f"Carry out the plan for:\n\n{task}"),
        contract=ArtifactContract(
            output_schema=(SCHEMAS["test_report"] if execute_command else {}),
            side_effect_class="LOCAL_WRITE"),
        config=({"command": execute_command} if execute_command
                else _worker_config(worker, provider,
                                    payload={"summary": "executed"})))

    # The verify node runs NO command of its own. Its checks live in its
    # contract, so the same gate that guards every other node's artifact is the
    # one that runs them — a verification step with its own private execution
    # path is a step whose failures are shaped differently from everyone else's.
    verify = G.TaskNode(
        node_id="verify", kind="verify", depends_on=["execute"],
        worker="static",
        instruction="The project's own checks, run by the gate.",
        contract=ArtifactContract(output_schema=SCHEMAS["test_report"],
                                  acceptance_checks=checks),
        config={"payload": {"command": "; ".join(checks) or "(none declared)",
                            "exit_code": 0}})

    report = G.TaskNode(
        node_id="report", kind="report", depends_on=["verify"],
        worker=worker,
        instruction=("Report what was done, with the evidence, and state "
                     "plainly what remains unestablished."),
        contract=ArtifactContract(output_schema=SCHEMAS["report"]),
        config=_worker_config(worker, provider, schema="report",
                              payload={"summary": f"completed: {task}",
                                       "not_established": []}))

    return G.TaskGraph([plan, execute, verify, report])


def _worker_config(worker: str, provider: str | None, *,
                   schema: str | None = None, payload: dict | None = None
                   ) -> dict:
    if worker == "provider":
        config = {"provider": provider}
        if schema:
            config["schema"] = schema
        return config
    return {"payload": payload or {}}
