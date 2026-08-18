"""The loop: one verified work item at a time, until something must stop it.

`dobby project next` names the item and `dobby runtime run` executes a graph.
Between them sat the part a person had to do by hand every time — open a shift,
build the graph from the item's own acceptance checks, point the item at the run
before starting it, judge the item by the run rather than by the report, and
decide whether it is safe to carry on. That sequence is not a convenience; it is
where the invariants are actually applied, and a loop that skips one of its steps
is a loop that makes progress it cannot defend.

WHY IT RE-BASELINES BETWEEN ITEMS
---------------------------------
A work item that succeeds has changed the tree. That is the point of it, and it
also means the baseline recorded before it describes code that no longer exists.
`open_session` would refuse the next shift on exactly those grounds (PK-4), so
this loop re-takes the baseline instead — which is not a way around the refusal
but the thing the refusal asks for: run the project's own smoke checks against
the new tree and find out whether the last item broke it. If they fail, PK-1
stops everything, and the failure is attributed to the item that just ran rather
than surfacing three items later against the wrong change.

WHY IT STOPS SO OFTEN
---------------------
The measure of a harness like this is not how long it can keep acting. It is
whether it stops at the boundaries only a person can cross, and says which one it
hit. Every stop reason here is one of those boundaries, and each is a separate
constant because "the loop ended" is not an answer a caller can act on:

    PORTFOLIO_COMPLETE      nothing remains
    NOTHING_STARTABLE       everything left is blocked or waiting on a dependency
    NEEDS_ARCHITECT         the next item has no machine-checkable acceptance, or
                            too much uncertainty to hand to an implementation
    NEEDS_RECONCILIATION    an external effect was claimed and never confirmed
    BASELINE_FAILED         the tree does not pass its own checks
    ITEM_BLOCKED            the run did not satisfy the item, and repeating it
                            unchanged is the one action guaranteed not to help
    MAX_ITEMS               the caller's ceiling

A blocked item is a stop and not a skip. The loop could step over it to the next
one — the selector would, since BLOCKED is not selectable — and that is exactly
how a portfolio fills with quiet failures while the summary line keeps reporting
progress. Running the command again steps over it deliberately, which is a
decision somebody made.
"""

from __future__ import annotations

from .models import BLOCKED, DONE, ProjectError
from .session import attach_run, close_session, open_session
from .store import ProjectStore

# -- stop reasons ------------------------------------------------------------
PORTFOLIO_COMPLETE = "portfolio_complete"
NOTHING_STARTABLE = "nothing_startable"
NEEDS_ARCHITECT = "needs_architect"
NEEDS_RECONCILIATION = "needs_reconciliation"
BASELINE_FAILED = "baseline_failed"
ITEM_BLOCKED = "item_blocked"
MAX_ITEMS = "max_items"

STOP_REASONS = (PORTFOLIO_COMPLETE, NOTHING_STARTABLE, NEEDS_ARCHITECT,
                NEEDS_RECONCILIATION, BASELINE_FAILED, ITEM_BLOCKED, MAX_ITEMS)

#: A ceiling on an unbounded drain. Not a tuning knob — a runaway guard, so a
#: portfolio that somehow keeps producing startable items cannot spend a machine
#: overnight without anybody having chosen that.
DRAIN_CEILING = 50


def advance(data_dir: str, *, project_id: str | None = None,
            provider: str | None = None, execute_command: str | None = None,
            max_items: int = 1, max_steps: int = 100, budget=None) -> dict:
    """Carry the portfolio forward, and say why it stopped.

    `max_items=0` drains until a stop reason, bounded by `DRAIN_CEILING`.

    Somebody has to do the work: pass `provider` for an agent CLI or
    `execute_command` for a deterministic step. With neither, the graph is
    static — which exercises this loop and the kernel's invariants and produces
    nothing. That is reported as `static` in the result rather than left for a
    reader to infer, because a drained portfolio full of static runs looks
    exactly like a finished project.
    """
    from ..runtime import RunBudget, Runner, default_graph

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    pid = project["project_id"]
    root = project["manifest"].root
    static = not (provider or execute_command)

    ceiling = DRAIN_CEILING if max_items <= 0 else max_items
    iterations: list[dict] = []
    completed: list[str] = []
    stopped, detail = MAX_ITEMS, f"reached the ceiling of {ceiling} item(s)"

    for _ in range(ceiling):
        step, stop = _one_item(
            data_dir, store, pid, root, provider=provider,
            execute_command=execute_command, static=static,
            max_steps=max_steps, budget=budget, make_graph=default_graph,
            make_runner=Runner, make_budget=RunBudget)
        if step is not None:
            iterations.append(step)
            if step["item_state"] == DONE:
                completed.append(step["work_item_id"])
        if stop is not None:
            stopped, detail = stop
            break

    refreshed = store.load_project(pid)
    return {
        "project_id": pid,
        "static": static,
        "items_completed": completed,
        "iterations": iterations,
        "stopped": stopped,
        "detail": detail,
        "coverage": refreshed["portfolio"].coverage(),
        "baseline_passed": bool(refreshed["baseline"]
                                and refreshed["baseline"].passed),
    }


def _one_item(data_dir, store, project_id, root, *, provider, execute_command,
              static, max_steps, budget, make_graph, make_runner, make_budget):
    """One shift. Returns `(step_record | None, stop | None)`.

    A record with no stop means carry on; a stop with no record means the loop
    never got as far as running anything.
    """
    envelope = open_session(data_dir, project_id=project_id, rebaseline=True)

    if envelope.needs_rebaseline:
        # It was asked to re-take the baseline and still says no, which means the
        # checks ran and failed rather than that they were skipped.
        return None, (BASELINE_FAILED, envelope.next_action)
    if envelope.unconfirmed_effects:
        return None, (NEEDS_RECONCILIATION, envelope.next_action)
    if not envelope.active_work_item_id:
        remaining = store.load_project(project_id)["portfolio"].remaining()
        return None, ((NOTHING_STARTABLE, envelope.next_action) if remaining
                      else (PORTFOLIO_COMPLETE, envelope.next_action))

    project = store.load_project(project_id)
    item = project["portfolio"].get(envelope.active_work_item_id)
    if item is None:                                    # pragma: no cover
        raise ProjectError(
            f"session {envelope.session_id} names work item "
            f"{envelope.active_work_item_id!r}, which is not in the portfolio")
    if item.needs_architect:
        return None, (NEEDS_ARCHITECT, envelope.next_action)

    graph = make_graph(item.outcome or item.title, provider=provider,
                       execute_command=execute_command,
                       acceptance_checks=list(item.acceptance_checks),
                       static=static)
    runner = make_runner(root, data_dir=data_dir)
    run_id = runner.start(item.outcome or item.title, graph,
                          budget=budget or make_budget())

    # Attached BEFORE the run, so a crash leaves an item that points at the run
    # rather than an orphan run and an item that looks untouched.
    attach_run(data_dir, item.work_item_id, run_id, project_id=project_id)
    result = runner.run(run_id, budget=budget or make_budget(),
                        max_steps=max_steps)

    # PK-2, applied by `close_session`: the RUN decides, not this function and
    # not whatever the report said.
    closed = close_session(data_dir, envelope.session_id)
    judged = store.load_project(project_id)["portfolio"].get(item.work_item_id)

    step = {"session_id": envelope.session_id,
            "work_item_id": item.work_item_id,
            "title": item.title,
            "run_id": run_id,
            "run_state": result.state,
            "item_state": judged.state,
            "blocked_reason": judged.blocked_reason,
            "evidence_refs": list(judged.evidence_refs),
            "unconfirmed_effects": len(result.unconfirmed_effects),
            "next_action": closed.next_action}

    if judged.state == BLOCKED:
        return step, (ITEM_BLOCKED,
                      f"{item.work_item_id}: {judged.blocked_reason}")
    return step, None
