"""Carrying a failure back to the one component allowed to change the approach.

WHAT WAS MISSING

`REPLAN` has been in `TRIGGERS` since the architect boundary landed and nothing
ever produced it. A run that failed blocked its item and stopped the loop;
`reattempt` could repair a failing ARTIFACT check, deterministically and for
free, but a failure with no artifact behind it — the tests do not pass, the
worker could not do it — yielded `NO_REPAIR_DERIVED` and that was the end of the
road. Meanwhile the evidence of why it failed was sitting in the run store,
durable and unread.

This reads it and hands it back. Nothing here decides anything: the architect
decides, `evaluate` still refuses a plan that weakens the definition of done, and
the call is bounded by the same per-trigger ceiling as any other.

WHY THE DETERMINISTIC REPAIR GOES FIRST

`reattempt.derive_repair` produces a repair from the item's own failing check —
no model, no cost, and it is right whenever the artifact was the problem. Asking
a model to reconsider the whole approach before trying that would spend a call to
rediscover something a command already knew. So the replan is what happens when
the repair path returns nothing, which is exactly the case it was missing.

WHY A NEW FAILURE IS AUTOMATICALLY A NEW QUESTION

Two mechanisms, and they agree. `close_session` writes `blocked_reason` and bumps
the item's version, so `architect_contract_digest` moves; and the failure context
below is folded into the request digest directly. A second identical failure
therefore dedupes to the first answer — correct, because re-asking about a
failure that has not changed is the thing `decision_for` exists to prevent — while
a DIFFERENT failure is a different question and gets a fresh call.

WHAT IT CANNOT DO

An architect may still only propose acceptance checks the project already
declares. So a replan cannot fix a broken implementation by lowering the bar; it
can add a check, add a dependency, or — with `--compile-plans` — propose
different `execution_steps`, which is the path that actually changes what runs.
Without that flag a replan can only re-shape the contract, and the same generic
graph runs again. That limit is real and is stated rather than worked around.
"""

from __future__ import annotations

from . import architecture as A
from .models import BLOCKED

#: Characters of a node's failure detail carried into the prompt. A verifier
#: that dumps a whole test log costs tokens without adding identification: the
#: node id and the classification are what name the failure, and the tail is
#: where a command's own error usually is.
DETAIL_CAP = 400

#: Failing nodes reported. A graph where everything downstream of one break also
#: "failed" would otherwise fill the prompt with consequences of the first one.
MAX_NODES = 5


def failure_context(data_dir: str, run_id: str, *, detail_cap: int = DETAIL_CAP,
                    max_nodes: int = MAX_NODES) -> tuple[str, ...]:
    """The failing nodes of one run, named, in the order they were to run.

    Returns lines rather than a blob so the caller can fold them into a digest:
    a list whose ORDER is the graph's own order is stable across reads, and a
    stable identity is what makes the dedupe correct rather than accidental.

    A run that cannot be read at all returns empty. That is not silently fine —
    the caller treats "no failure context" as "nothing new to tell the
    architect", which is the safe reading.
    """
    from ..runtime import graph as G
    from ..runtime.store import RunStore

    try:
        run = RunStore(data_dir).load_run(run_id)
    except Exception:
        return ()

    graph = run["graph"]
    try:
        order = graph.topological_order()
    except Exception:                       # pragma: no cover - defensive
        order = list(graph.nodes)

    lines: list[str] = []
    for node_id in order:
        node = graph.nodes.get(node_id)
        if node is None or node.state != G.NODE_FAILED:
            continue
        failure = node.last_failure or {}
        kind = failure.get("kind") or failure.get("classification") or "failed"
        detail = str(failure.get("message") or failure.get("detail") or "")
        detail = detail.strip().replace("\r", "")
        if len(detail) > detail_cap:
            # The tail: a command's own error is at the end of its output, and
            # the head is usually the banner.
            detail = "..." + detail[-detail_cap:]
        lines.append(f"{node_id} ({node.kind}) {kind}: {detail or '(no detail)'}")
        if len(lines) >= max_nodes:
            lines.append(f"(and any nodes after {node_id}, not listed)")
            break
    return tuple(lines)


def request_replan(data_dir: str, item, *, project_id: str, session_id: str = "",
                   provider: str | None = None, propose=None,
                   allow_network: bool = False,
                   ceiling: int = A.ARCHITECT_CALL_CEILING) -> A.PlanDecision:
    """Ask the architect to reconsider, carrying what actually went wrong.

    Raises nothing a caller has to catch: a provider that fails, times out or
    answers in prose comes back as a `REJECTED` decision, which is the same shape
    every other refusal takes here.
    """
    context = failure_context(data_dir, item.latest_run_id or "")
    if not context and item.blocked_reason:
        # The run could not be read, but the kernel's own judgement of it can.
        # Better than an empty request, and honest about being second-hand.
        context = (f"(from the item, not the run) {item.blocked_reason}",)

    try:
        return A.request_architecture(
            data_dir, item.work_item_id, project_id=project_id,
            session_id=session_id, trigger=A.REPLAN, provider=provider,
            propose=propose, allow_network=allow_network, ceiling=ceiling,
            failure_context=context)
    except A.PlanRejected as exc:
        return A.PlanDecision(request_digest="", plan_id=None,
                              outcome=A.REJECTED, reason=str(exc))


def blocked_needs_replan(item) -> bool:
    """Whether this item's state is the one a replan is for.

    Kept as a named predicate rather than an inline `item.state == BLOCKED`
    because BLOCKED alone is not the condition: an item blocked with no recorded
    reason gives the architect nothing to reconsider, and asking anyway is how a
    budget gets spent on an empty question.
    """
    return item.state == BLOCKED and bool(item.blocked_reason)
