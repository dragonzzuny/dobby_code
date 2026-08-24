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
from .contracts import (Artifact, ArtifactContract, ContractError,
                        PayloadTampered,
                        PROMOTED, REJECTED,
                        SCHEMAS, VERIFIED, artifact_path,
                        idempotency_key, verify_payload)
from .failures import (DEFAULT_POLICY, Failure, POLICY_BLOCKED, REPAIR,
                       RETRY_ELSEWHERE, RETRY_SAME, TRANSIENT_PROVIDER, WAIT,
                       backoff_delay)
from .placement import ConcurrencyLimiter, ProviderPlacement
from .scheduler import BudgetExceeded, RunBudget, Scheduler
from .store import (EFFECT_CLAIMED, EFFECT_CONFIRMED, RunStore,
                    worker_identity)
from .trace import (AGENT_GENERATION, NODE, RUN, SCHEDULER_DECISION, TOOL_CALL,
                    VERIFIER, Tracer)
from .verify import Verifier, promotable
from .workers import DEFAULT_NODE_TIMEOUT_S, WorkerRegistry

#: Added to a node's own timeout to get its lease TTL. Slack for the verifier
#: and the store writes that follow the worker, so a lease never expires under a
#: node that is still legitimately finishing.
LEASE_MARGIN_S = 300.0


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
                 max_parallel: int = 1,
                 allow_network: bool = False,
                 placement_context=None,
                 override_provider: str | None = None,
                 sleep=time.sleep):
        import threading
        self.repo = os.path.abspath(repo)
        self.data_dir = data_dir or os.path.join(self.repo, ".dobby")
        self.store = RunStore(self.data_dir)
        self.workers = workers or WorkerRegistry()
        self.policy = policy or DEFAULT_POLICY
        #: Nodes run at once. Defaults to 1 — sequential is the right default
        #: for a graph whose steps mostly depend on each other, and raising it
        #: only helps a graph with a genuine fan-out.
        self.max_parallel = max(1, max_parallel)
        self.placement = ProviderPlacement(self.store,
                                           allow_network=allow_network)
        #: What the SCHEDULER knows that a node does not: whether this run is in
        #: an isolated workspace, the operator's provider preferences, and how
        #: many calls each provider has already spent. Without it the role
        #: policy's isolation rule and the Claude cap are unreachable, which is
        #: how a table of intentions stays a table of intentions.
        from ..providers.policy import PlacementContext

        self.placement_context = placement_context or PlacementContext(
            original_root=self.repo)
        #: For reproducing a run or an incident. It may not bypass isolation or
        #: a spent quota — `placement.choose` refuses that explicitly.
        self.override_provider = override_provider
        self.limiter = ConcurrencyLimiter(total=self.max_parallel,
                                          per_provider=max(1, self.max_parallel))
        #: The budget is read-modify-write from several threads once
        #: `max_parallel > 1`. Without this two nodes admitted at the same
        #: instant both see the same remaining count.
        self._budget_lock = threading.Lock()
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

        tracer = Tracer(self.store, run_id)
        deferred: list[dict] = []
        with tracer.span(RUN, f"run:{run_id}", task=state["task"],
                         max_parallel=self.max_parallel) as root:
            for _ in range(max_steps):
                self._skip_unreachable(run_id, task_graph)
                if task_graph.done():
                    break
                decisions, deferred = scheduler.next_nodes(
                    task_graph, limit=self.max_parallel)
                if not decisions:
                    break
                for decision in decisions:
                    tracer.event(SCHEDULER_DECISION,
                                 f"admit:{decision.node_id}",
                                 candidates=[d.node_id for d in decisions],
                                 chosen=decision.node_id,
                                 reason=decision.reason,
                                 deferred=[d["node_id"] for d in deferred])
                self._dispatch(run_id, task_graph, decisions, budget,
                               tracer.child_of(root.span_id))

        self._skip_unreachable(run_id, task_graph)
        final = self._finalize(run_id, task_graph, deferred)
        return self._report(run_id, task_graph, deferred, budget, notes,
                            state=final)

    def _dispatch(self, run_id: str, task_graph: "G.TaskGraph", decisions,
                  budget: RunBudget, tracer) -> None:
        """Run this batch of nodes — one thread each when parallelism is on.

        Threads and not processes for the same reason `providers/fanout.py`
        uses them: every unit of work here is a child process the parent waits
        on, so the GIL is released for essentially the whole call.

        A node that raises does not lose the batch. Its exception is turned into
        a permanent failure for that node by `_execute_node` itself; anything
        escaping that is a bug in the runner, and it is recorded against the run
        rather than allowed to kill the sibling that was about to succeed.
        """
        if len(decisions) == 1 or self.max_parallel == 1:
            self._execute_node(run_id, task_graph,
                               task_graph.nodes[decisions[0].node_id], budget,
                               tracer)
            return

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_parallel) as pool:
            futures = {
                pool.submit(self._execute_node, run_id, task_graph,
                            task_graph.nodes[d.node_id], budget,
                            tracer.child_of(None)): d.node_id
                for d in decisions}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:      # noqa: BLE001 - runner defect
                    node_id = futures[future]
                    self.store.set_node_state(
                        run_id, node_id, G.NODE_FAILED,
                        reason=f"runner error: {type(exc).__name__}: {exc}",
                        enforce=False)
                    task_graph.nodes[node_id].state = G.NODE_FAILED

    # -- crash reconciliation ----------------------------------------------
    def _reconcile(self, run_id: str, task_graph: "G.TaskGraph") -> list[str]:
        """Close attempts whose holder is gone, and free only those nodes.

        An attempt with no finish means one of two things, and they want
        opposite treatment: the process died holding it (recover), or another
        worker is running it right now (leave it alone). The lease record is
        what tells them apart. Recovering indiscriminately — which is what an
        open attempt alone justifies — hands a second worker the node the first
        is still executing, which is the duplicate the lease exists to prevent,
        reintroduced by the code that repairs crashes.

        Nothing is re-run that finished, and nothing is taken from a worker that
        is demonstrably alive.
        """
        notes: list[str] = []
        for row in self.store.open_attempts(run_id):
            lease = self.store.node_lease(run_id, row["node_id"])
            if lease["held"]:
                notes.append(
                    f"{row['node_id']}: attempt {row['attempt']} is open and "
                    f"its lease is still held by {lease['owner']}, which is "
                    f"running. Left alone.")
                continue
            self.store.finish_attempt(
                run_id, row["node_id"], row["attempt"],
                outcome=G.RETRYABLE_FAILURE, failure_class=TRANSIENT_PROVIDER,
                detail=G.INTERRUPTED_DETAIL)
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
                f"confirmed (key {effect['idempotency_key'][:12]}…). Whether it "
                f"reached the outside world is UNKNOWN, so the node is blocked "
                f"rather than repeated or reported done. Resolve it with "
                f"`confirm_effect` (it happened) or `release_effect` (it did "
                f"not), then resume.")
        notes.extend(self._unblock_resolved_effects(run_id, task_graph))
        return notes

    def _unblock_resolved_effects(self, run_id: str,
                                  task_graph: "G.TaskGraph") -> list[str]:
        """Return nodes to READY once the effect that blocked them is resolved.

        Resume is the only moment an operator's decision can take effect, and
        without this it never would: the node parks in BLOCKED_ON_APPROVAL and
        nothing reads the store to notice that `confirm_effect` or
        `release_effect` has since been called.

        The gate is an EVENT, not the effect's current status, and that is the
        whole care in this method. A node blocked because a human has not
        approved an irreversible action also carries an idempotency key, and its
        effect is likewise "not currently CLAIMED" — status alone would unblock
        it and walk straight past the approval. Only a recorded resolution of
        THIS node's effect counts.
        """
        resolved = {event["node_id"] for event in self.store.events(run_id)
                    if event["kind"] in ("effect_confirmed", "effect_released")
                    and event["node_id"]}
        notes: list[str] = []
        for node in task_graph.nodes.values():
            if node.state != G.BLOCKED_ON_APPROVAL:
                continue
            if node.node_id not in resolved:
                continue
            if not node.contract.needs_idempotency_key:
                continue
            key = idempotency_key(run_id, node.node_id,
                                  node.contract.effect_version)
            status = self.store.effect_status(key)
            if status == EFFECT_CLAIMED:
                continue                    # resolved once, claimed again since
            self._set_node(
                run_id, task_graph, node, G.READY,
                reason=("the external effect that blocked this node was "
                        + ("confirmed; the node will finish without repeating "
                           "it" if status == EFFECT_CONFIRMED else
                           "released; the node will perform it")))
            notes.append(
                f"{node.node_id}: unblocked — its external effect was "
                f"{'confirmed' if status == EFFECT_CONFIRMED else 'released'} "
                f"by an operator")
        return notes

    # -- one node ----------------------------------------------------------
    def _execute_node(self, run_id: str, task_graph: "G.TaskGraph", node,
                      budget: RunBudget, tracer) -> None:
        if node.state == G.PENDING:
            self._set_node(run_id, task_graph, node, G.READY,
                           reason="dependencies satisfied")
        ttl = float(node.config.get("timeout_s") or DEFAULT_NODE_TIMEOUT_S)
        if not self.store.lease_node(run_id, node.node_id,
                                     holder=worker_identity(),
                                     ttl_s=ttl + LEASE_MARGIN_S):
            # Another process took it. Reload so this one is not deciding from
            # a stale graph, and give back the budget slot the scheduler
            # reserved — this run did not do the work.
            with self._budget_lock:
                budget.refund(node)
            node.state = self.store.load_run(run_id)["graph"].nodes[
                node.node_id].state
            return
        node.state = G.LEASED

        attempt = self.store.next_attempt_number(run_id, node.node_id)
        self.store.start_attempt(run_id, node.node_id, attempt,
                                 worker=node.worker)
        node.attempts = attempt
        # The budget was already taken by `Scheduler.reserve` when this node was
        # admitted. Charging again here would double-count every attempt.
        self._set_node(run_id, task_graph, node, G.NODE_RUNNING,
                       reason=f"attempt {attempt}")

        with tracer.span(NODE, f"node:{node.node_id}", node_id=node.node_id,
                         attempt=attempt, node_kind=node.kind,
                         worker=node.worker) as node_span:
            self._run_attempt(run_id, task_graph, node, budget, attempt,
                              tracer.child_of(node_span.span_id), node_span)

    def _run_attempt(self, run_id: str, task_graph: "G.TaskGraph", node,
                     budget: RunBudget, attempt: int, tracer, node_span) -> None:
        started = time.monotonic()
        effect_state, claimed_key = self._claim_effect(run_id, node, attempt)
        if effect_state == EFFECT_CONFIRMED:
            # The effect is already done, and CONFIRMED means observed rather
            # than assumed. The node is finished by definition: re-running it
            # would be the duplicate this key exists to prevent.
            self.store.finish_attempt(
                run_id, node.node_id, attempt, outcome=G.FINISHED,
                detail="external effect already performed under this "
                       "idempotency key; node satisfied without repeating it")
            self._set_node(run_id, task_graph, node, G.VERIFYING,
                           reason="effect already applied")
            self._set_node(run_id, task_graph, node, G.NODE_SUCCEEDED,
                           reason="idempotent no-op")
            node_span.attributes["outcome"] = "idempotent_no_op"
            return
        if effect_state == EFFECT_CLAIMED:
            # Claimed by an earlier attempt that never came back to confirm it.
            # The claim is written BEFORE the effect, so this says the effect
            # may have happened and may not — and there is nothing on this
            # machine that can decide which. Both automatic answers are wrong:
            # repeating it risks a second real-world effect, and calling it done
            # reports a success nobody observed. So it blocks, and a human or an
            # effect-provider lookup resolves it.
            node_span.attributes["outcome"] = "effect_unreconciled"
            self._fail_attempt(
                run_id, task_graph, node, attempt,
                Failure(POLICY_BLOCKED,
                        "an external effect was claimed by an earlier attempt "
                        "and never confirmed; whether it reached the outside "
                        "world is unknown. Confirm it or release it, then "
                        "resume.",
                        {"idempotency_key": claimed_key,
                         "effect_class": node.contract.side_effect_class,
                         "resolve_with": ["RunStore.confirm_effect",
                                          "RunStore.release_effect"]}),
                round(time.monotonic() - started, 2))
            return

        placement = self._place(node, tracer)
        if placement is not None and placement.provider is None:
            self._fail_attempt(
                run_id, task_graph, node, attempt,
                Failure("CAPACITY", placement.reason,
                        {"candidates": placement.candidates}),
                round(time.monotonic() - started, 2))
            return

        try:
            inputs = self._promoted_inputs(run_id, task_graph, node)
        except PayloadTampered as exc:
            # NON_RETRYABLE and not a repair: the digest says the file is not
            # what the gate graded, and nothing here can know which of the two
            # versions was meant. Running the node on either one would be
            # choosing, silently. A person resolves this.
            self._fail_attempt(
                run_id, task_graph, node, attempt,
                Failure("NON_RETRYABLE", str(exc),
                        {"artifact": getattr(exc, "artifact_id", "")}),
                round(time.monotonic() - started, 2))
            return
        context = {"repo": self.repo, "attempt": attempt,
                   # The scheduler's view of the workspace, which is what
                   # licenses a headless tool grant. A node cannot know this
                   # about itself.
                   "isolated": self.placement_context.isolated,
                   "inputs": inputs,
                   "run_id": run_id}
        provider = node.config.get("provider") if node.worker == "provider" \
            else None
        span_kind = AGENT_GENERATION if provider else TOOL_CALL
        span_attrs = ({"provider": provider, "model": node.config.get("model"),
                       "node_kind": node.kind}
                      if provider else
                      {"tool": node.config.get("command", node.worker),
                       "effect_class": node.contract.side_effect_class,
                       "node_kind": node.kind})

        acquired = provider is not None and self.limiter.acquire(
            provider, timeout=node.config.get("queue_timeout_s", 300))
        if provider is not None and not acquired:
            self._fail_attempt(
                run_id, task_graph, node, attempt,
                Failure("CAPACITY",
                        f"waited past the queue timeout for a {provider} slot"),
                round(time.monotonic() - started, 2))
            return
        try:
            with tracer.span(span_kind, f"{node.worker}:{node.node_id}",
                             node_id=node.node_id, attempt=attempt,
                             **span_attrs) as work_span:
                try:
                    result = self.workers.get(node.worker).run(node, context)
                except Exception as exc:  # noqa: BLE001 - adapter bug is a run event
                    result = None
                    failure = Failure("NON_RETRYABLE",
                                      f"worker {node.worker!r} raised "
                                      f"{type(exc).__name__}: {exc}")
                else:
                    failure = result.failure
                    work_span.attributes.update(result.meta or {})
                if result is None or not result.ok:
                    work_span.end("ERROR",
                                  failure_class=failure.failure_class
                                  if failure else "UNKNOWN")
        finally:
            if acquired and provider is not None:
                self.limiter.release(provider)

        duration = round(time.monotonic() - started, 2)
        if result is None or not result.ok:
            if provider:
                self.placement.record_outcome(provider, False)
            self._fail_attempt(run_id, task_graph, node, attempt, failure,
                               duration)
            return

        if claimed_key:
            self.store.confirm_effect(claimed_key,
                                      result_digest=str(len(result.raw)))
            tracer.event(TOOL_CALL, "effect.confirmed", key=claimed_key,
                         tool=node.worker,
                         effect_class=node.contract.side_effect_class)

        # -- the gate ------------------------------------------------------
        self._set_node(run_id, task_graph, node, G.VERIFYING,
                       reason="worker returned; running acceptance checks")
        verifier = Verifier(self.repo,
                            log_dir=os.path.join(self.data_dir, "state",
                                                 "runtime", run_id, "logs"))
        with tracer.span(VERIFIER, f"verify:{node.node_id}",
                         node_id=node.node_id, attempt=attempt,
                         checks=len(node.contract.acceptance_checks),
                         passed=False) as gate_span:
            verdict = verifier.verify(node.contract, result.payload,
                                      node_id=node.node_id)
            gate_span.attributes["passed"] = verdict.passed
            gate_span.attributes["failed_requirements"] = \
                verdict.failed_requirements[:10]
            gate_span.attributes["not_run"] = verdict.not_run
        if provider:
            self.placement.record_outcome(provider, verdict.passed)
        artifact = self._store_artifact(run_id, node, result, verdict)

        if not promotable(node.contract, verdict):
            artifact.transition(REJECTED)
            self._write_artifact_file(run_id, artifact)
            self.store.put_artifact(artifact)
            self._fail_attempt(run_id, task_graph, node, attempt,
                               verdict.failure, duration,
                               verdict=verdict.to_dict())
            return

        # Each step recorded. The two transitions used to happen on one line
        # and only the destination reached the store, so a promotion had no
        # verified state behind it that anything could read.
        artifact.transition(VERIFIED)
        self.store.put_artifact(artifact)
        artifact.transition(PROMOTED)
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
    def _place(self, node, tracer):
        """Choose a provider for a provider-worker node, and record the argument.

        Returns None for nodes that do not use a provider, so a command node
        never pays for a scorecard read.

        The chosen provider is written back onto `node.config` for this attempt.
        That is deliberate and it is the only mutation of the node the runner
        makes: the next attempt re-places from scratch, because the reason to
        re-place — a provider that just failed, a breaker that just tripped — is
        exactly what the last attempt discovered.
        """
        if node.worker != "provider":
            return None
        avoid = set(node.config.get("avoid_providers") or ())
        placement = self.placement.choose(
            node, avoid=avoid, context=self.placement_context,
            override=self.override_provider)
        tracer.event(SCHEDULER_DECISION, f"place:{node.node_id}",
                     candidates=placement.candidates,
                     chosen=placement.provider or "(none)",
                     reason=placement.reason,
                     provisional=placement.provisional,
                     scores=placement.scores,
                     node_kind=node.kind,
                     provider_role=placement.provider_role,
                     isolated=placement.isolated,
                     eligible=placement.eligible,
                     rejected=placement.rejected,
                     selection_basis=placement.selection_basis,
                     claude_cap_remaining=placement.claude_cap_remaining)
        if placement.provider:
            node.config = dict(node.config)
            # `provider` is what the worker reads; `selected_provider` is what
            # the record says was CHOSEN. Keeping both means a trace can show a
            # selection that a later edit overwrote.
            node.config["provider"] = placement.provider
            node.config["selected_provider"] = placement.provider
            self.placement_context.provider_calls[placement.provider] = (
                self.placement_context.spent(placement.provider) + 1)
            if placement.hedge_with:
                node.config["hedge_with"] = placement.hedge_with
        return placement

    def _claim_effect(self, run_id: str, node, attempt: int):
        """`(state, key)` — what this node's external effect has already done.

        `state` is one of: None (this node has no external effect, or it is
        being claimed now for the first time), EFFECT_CONFIRMED (it happened),
        EFFECT_CLAIMED (an earlier attempt recorded the intent and never came
        back). The caller must branch on all three; the whole point of the
        middle one is that it is not the other two.
        """
        if not node.contract.needs_idempotency_key:
            return None, None
        key = idempotency_key(run_id, node.node_id,
                              node.contract.effect_version)
        if self.store.claim_effect(key, run_id, node.node_id,
                                   node.contract.effect_version):
            return None, key
        return self.store.effect_status(key), key

    def _promoted_inputs(self, run_id: str, task_graph: "G.TaskGraph",
                         node) -> dict:
        """The payloads of dependencies, and ONLY the promoted ones."""
        inputs: dict = {}
        for dep in node.depends_on:
            rows = self.store.artifacts(run_id, node_id=dep, state=PROMOTED)
            if not rows:
                continue
            if len(rows) > 1:
                # A node reaches SUCCEEDED once and SUCCEEDED is terminal, so a
                # second promoted artifact for one node means an invariant
                # broke somewhere else. Named rather than resolved: the old code
                # took `rows[-1]`, and that ordering is `created` to the second
                # then artifact_id as a STRING, where `produce-10` sorts before
                # `produce-2`. Silently picking under a broken invariant is how
                # the wrong evidence reaches the next step.
                raise ContractError(
                    f"node {dep!r} has {len(rows)} promoted artifacts "
                    f"({[r['artifact_id'] for r in rows]}); a node succeeds "
                    f"once, so this is a contradiction and not a choice to make "
                    f"here")
            latest = rows[-1]
            payload = self._read_payload(latest["path"])
            if payload is None:
                continue
            # The digest was recorded when the gate passed and was never read
            # again, so an edited artifact file reached the next node wearing a
            # PROMOTED label. Checked here, at the one place a payload becomes
            # an INPUT: a mismatch is refused rather than repaired, because
            # nothing here can know which of the two versions was meant.
            verify_payload(payload, latest["digest"],
                           artifact_id=latest["artifact_id"])
            dep_node = task_graph.nodes[dep]
            if dep_node.contract.ungraded:
                # Deliberately ungraded, and the consumer is told. Same
                # treatment as `advisory`: a control condition's output is a
                # real product of a real step and it is not evidence of
                # anything, and the difference has to travel with it.
                inputs[dep] = {"ungraded": True,
                               "not_verification": (
                                   "this node declared that it grades nothing, "
                                   "so nothing it produced was checked. It may "
                                   "inform this step and may not be cited as "
                                   "proof that anything passed"),
                               "payload": payload}
            elif dep_node.contract.advisory:
                # Labelled where it travels. A model's opinion is a real product
                # of a real step and it is not evidence; the consumer sees which
                # one it was handed instead of having to know.
                inputs[dep] = {"advisory": True,
                               "not_verification": (
                                   "a model judgment. It may inform this step "
                                   "and may not be cited as proof that "
                                   "anything passed"),
                               "payload": payload}
            else:
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
                      "advisory": node.contract.advisory,
                      "meta": result.meta})
        self._write_artifact_file(run_id, artifact)
        # Recorded as PROPOSED before anything grades it. The store now refuses
        # a state that was not walked to, so the lifecycle has to be persisted
        # rather than only its outcome — and the event log gains the two rows
        # that were previously invisible, which is what makes "PROPOSED ->
        # VERIFIED -> PROMOTED" a thing the log can be asked about.
        self.store.put_artifact(artifact)
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
