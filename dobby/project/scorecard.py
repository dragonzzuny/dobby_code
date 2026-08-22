"""What the escalation policies actually bought, from what the store recorded.

WHY THIS AND NOT MORE TELEMETRY

The review that prompted this asked for usage telemetry and offline policy
evaluation. Most of the first half already exists and is wired: `spend.py`
records provider time, `runtime/metrics.py` computes cost and agent-seconds per
VERIFIED task, and `runtime/placement.py` already feeds `metrics.scorecard` back
into which provider gets a node. Building a second provider-level telemetry
system would produce two numbers for one thing, and this repository's rule about
two disagreeing measurements is that both become suspect.

What was missing is one level up. The escalations added over the last few days —
asking an architect, spending a replan, compiling a plan into a graph, gating an
isolated merge — are each recorded somewhere, and nothing read them together. So
"was `--architect` worth turning on" and "what did the replan budget buy" had no
answer, and a policy you cannot evaluate is a policy you tune by feel.

EVERYTHING HERE IS JOINED FROM RECORDS, NOT REPORTED BY THE THING BEING GRADED

The architect counts come from `plan_revisions` joined to
`architecture_requests`, which is what actually happened rather than what a
summary said. Whether a run was COMPILED is derived from the node kinds of the
graph the runtime stored, not from the `graph` field `advance` returns — that
field is a report, and the run store is the record. A run that claims to have
been compiled and whose graph is four generic nodes is a disagreement worth
being able to see.

WHAT IT REFUSES TO REPORT

A cost figure this store does not have. `metrics.cost_per_verified_task` returns
a `Measurement` whose `value` is None when nothing recorded a cost, and that None
is carried through rather than replaced with a zero. A zero would read as "free"
and mean "unmeasured", which is the distinction the whole harness exists to keep.
"""

from __future__ import annotations

from . import architecture as A
from .models import BLOCKED, DONE
from .store import ProjectStore

#: Node ids the generic `runner.default_graph` produces. A run whose graph is
#: exactly these was not shaped by a plan, whatever anything else says.
GENERIC_NODES = frozenset({"plan", "execute", "verify", "report"})


def _graph_shape(run_store, run_id: str) -> str:
    """`compiled`, `generic`, or `unreadable` — from the stored graph itself."""
    if not run_id:
        return "none"
    try:
        graph = run_store.load_run(run_id)["graph"]
    except Exception:
        return "unreadable"
    ids = set(graph.nodes)
    if not ids:
        return "unreadable"
    return "generic" if ids <= GENERIC_NODES else "compiled"


def architect_activity(store: ProjectStore, project_id: str) -> dict:
    """Every architect call that reached a decision, by trigger and outcome.

    Budget refusals are counted SEPARATELY from decisions rather than folded in
    with them. They are the policy declining to spend, and merging them into the
    outcome histogram would make a working budget look like a run of rejections.
    """
    # One pass over the event log, not one per plan. The trigger lives on the
    # REQUEST and the outcome on the DECISION, and they are joined by digest.
    trigger_of = {e["payload"].get("digest"): e["payload"].get("trigger",
                                                              "UNKNOWN")
                  for e in store.events(project_id)
                  if e["kind"] == "architecture_requested"}

    by_trigger: dict = {}
    refusals = 0
    for row in store.plans(project_id):
        decision = row.get("decision") or {}
        outcome = decision.get("outcome", "UNKNOWN")
        reason = decision.get("reason", "") or ""
        if A.BUDGET_MARKER in reason:
            refusals += 1
            continue
        trigger = trigger_of.get(decision.get("request_digest", ""), "UNKNOWN")
        bucket = by_trigger.setdefault(trigger, {"calls": 0, "outcomes": {}})
        bucket["calls"] += 1
        bucket["outcomes"][outcome] = bucket["outcomes"].get(outcome, 0) + 1

    return {
        "by_trigger": by_trigger,
        "budget_refusals": refusals,
        "ceiling": A.ARCHITECT_CALL_CEILING,
        "note": ("a budget refusal is the policy declining to spend, not an "
                 "architect rejecting a plan; the two are counted apart"),
    }


def item_outcomes(store: ProjectStore, project_id: str, run_store) -> dict:
    """Per-item: what state it reached, on what shape of run, with what evidence."""
    portfolio = store.load_project(project_id)["portfolio"]
    rows = []
    for item in portfolio.items:
        rows.append({
            "work_item_id": item.work_item_id,
            "state": item.state,
            "planned_by": item.planned_by,
            "graph": _graph_shape(run_store, item.latest_run_id or ""),
            "evidence": len(item.evidence_refs),
            "blocked_reason": item.blocked_reason or "",
        })
    return {
        "items": rows,
        "done": sum(1 for r in rows if r["state"] == DONE),
        "blocked": sum(1 for r in rows if r["state"] == BLOCKED),
        "planned": sum(1 for r in rows if r["planned_by"]),
        "compiled_runs": sum(1 for r in rows if r["graph"] == "compiled"),
        "generic_runs": sum(1 for r in rows if r["graph"] == "generic"),
    }


def policy_scorecard(data_dir: str, project_id: str | None = None, *,
                     limit: int = 500) -> dict:
    """The escalations, what they produced, and what is not measured here.

    Read-only and cheap: it runs no commands, calls no provider, and touches
    nothing. A scorecard that changed the thing it grades would be worthless as
    an input to changing the policy.
    """
    from ..runtime.metrics import report as runtime_report
    from ..runtime.store import RunStore

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    pid = project["project_id"]
    run_store = RunStore(data_dir)

    outcomes = item_outcomes(store, pid, run_store)
    runtime = runtime_report(run_store, limit=limit)

    done = outcomes["done"]
    architect = architect_activity(store, pid)
    calls = sum(b["calls"] for b in architect["by_trigger"].values())

    return {
        "project_id": pid,
        "coverage": project["portfolio"].coverage(),
        "items": outcomes,
        "architect": architect,
        # The ratio the policy question actually turns on: escalations bought
        # per finished item. Reported as a pair rather than a quotient when
        # nothing finished, because n/0 is not "infinite escalation", it is "no
        # denominator yet".
        "escalation_per_done": (round(calls / done, 3) if done else None),
        "escalation_note": ("null means nothing has finished yet, which is not "
                            "the same as no escalation cost"),
        "runtime": {
            "cost_per_verified_task": runtime["cost_per_verified_task"],
            "agent_seconds_per_verified_task":
                runtime["agent_seconds_per_verified_task"],
            "task_success_at_verifier": runtime["task_success_at_verifier"],
            "retry_amplification": runtime["retry_amplification"],
        },
        "unmeasured": sorted(set(runtime["unmeasured"]) | {
            # Named explicitly, because a policy view that quietly omits these
            # invites the reader to assume they were zero.
            "workspace_merges: the merge gate's outcome is returned by the run "
            "and not persisted, so refused merges are not counted here",
            "replan_outcomes: counted under the REPLAN trigger above, but the "
            "retry it licensed is not attributed back to it",
        }),
    }
