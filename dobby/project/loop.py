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
    NEEDS_ARCHITECT         the next item has no machine-checkable acceptance,
                            or too much uncertainty to hand to an
                            implementation — and no architect was offered
    NEEDS_DISCOVERY         the architect answered, correctly, that the item
                            needs evidence before it needs an implementation
    NEEDS_HUMAN_APPROVAL    the plan wants something outside what the project
                            has declared: a new command, a raised effect class
    PLAN_REJECTED           the plan would have weakened the definition of done
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

from . import architecture as A
from .models import BLOCKED, DONE, ProjectError
from .session import attach_run, close_session, open_session
from .store import ProjectStore

# -- stop reasons ------------------------------------------------------------
PORTFOLIO_COMPLETE = "portfolio_complete"
NOTHING_STARTABLE = "nothing_startable"
NEEDS_ARCHITECT = "needs_architect"
NEEDS_DISCOVERY = "needs_discovery"
NEEDS_HUMAN_APPROVAL = "needs_human_approval"
PLAN_REJECTED = "plan_rejected"
NEEDS_RECONCILIATION = "needs_reconciliation"
BASELINE_FAILED = "baseline_failed"
ITEM_BLOCKED = "item_blocked"
MAX_ITEMS = "max_items"

#: The caller asked for an isolated run and it could not be given: no git, no
#: commits, or no declared write set to gate the result against. Reported rather
#: than quietly downgraded to an unisolated run, because an operator who asked
#: for isolation and silently did not get it is worse off than one who was told.
ISOLATION_UNAVAILABLE = "isolation_unavailable"

STOP_REASONS = (PORTFOLIO_COMPLETE, NOTHING_STARTABLE, NEEDS_ARCHITECT,
                NEEDS_DISCOVERY, NEEDS_HUMAN_APPROVAL, PLAN_REJECTED,
                NEEDS_RECONCILIATION, BASELINE_FAILED, ITEM_BLOCKED,
                ISOLATION_UNAVAILABLE, MAX_ITEMS)

#: A ceiling on an unbounded drain. Not a tuning knob — a runaway guard, so a
#: portfolio that somehow keeps producing startable items cannot spend a machine
#: overnight without anybody having chosen that.
DRAIN_CEILING = 50

#: An architect outcome that is not APPLIED is a halt, and each kind of halt is
#: its own reason. Mapped rather than branched so a new outcome cannot silently
#: fall through to "the loop ended".
_STOP_FOR_OUTCOME = {
    A.NEEDS_DISCOVERY: NEEDS_DISCOVERY,
    A.NEEDS_HUMAN_APPROVAL: NEEDS_HUMAN_APPROVAL,
    A.REJECTED: PLAN_REJECTED,
}


#: A rejected plan gets ONE more attempt, with the rejection handed back.
#:
#: Bounded at one on purpose. Two is a budget decision nobody made, and a plan
#: rejected twice for the same stated reason is a provider that cannot act on
#: that reason rather than one that missed it.
PLAN_REPAIR_ATTEMPTS = 1


def _ask_the_architect(data_dir, project_id, item, envelope, *, provider,
                       propose):
    """One bounded call, then one repair, and failures are decisions.

    A provider that is absent, times out or answers in prose is not a crash of
    the loop: it is a plan that could not be obtained, which is exactly the
    `REJECTED` outcome the store records with the reason attached.

    THE REPAIR, and why it is not a guess. `architecture.evaluate` rejects a
    plan for reasons it states as sentences, deterministically -- "the plan
    drops acceptance check(s) already on this item: [...]. An architect may add
    to the definition of done and may never narrow it". That is a machine's
    finding about a specific plan, not a model's theory about a failure, and
    `ArchitectureRequest.failure_context` already carries such text into the
    architect's prompt. Everything needed to ask again was present and nothing
    asked.

    Measured, and this is why it is here. On `django__django-13121` the gate
    refused the plan for exactly that reason and the loop stopped with ONE
    provider call against a budget of five, scoring 0/1 on an instance a single
    claude call resolved. The refusal was correct. Ending there was the waste.
    """
    context: tuple = ()
    for attempt in range(PLAN_REPAIR_ATTEMPTS + 1):
        try:
            decision = A.request_architecture(
                data_dir, item.work_item_id, project_id=project_id,
                session_id=envelope.session_id, provider=provider,
                propose=propose, failure_context=context)
        except A.PlanRejected as exc:
            decision = A.PlanDecision(request_digest="", plan_id=None,
                                      outcome=A.REJECTED, reason=str(exc))
        if decision.outcome != A.REJECTED or attempt >= PLAN_REPAIR_ATTEMPTS:
            return decision
        # The reason travels verbatim. Paraphrasing it here would put this
        # module's reading of the gate in front of what the gate said.
        context = (
            "A previous plan for this item was REJECTED. Fix exactly this and "
            "propose again:",
            decision.reason or "(the rejection carried no reason)",
        )
    return decision


def advance(data_dir: str, *, project_id: str | None = None,
            provider: str | None = None, execute_command: str | None = None,
            max_items: int = 1, max_steps: int = 100, budget=None,
            architect: bool = False, architect_provider: str | None = None,
            propose=None, compile_plans: bool = False,
            isolate: bool = False, policy: str = "") -> dict:
    """Carry the portfolio forward, and say why it stopped.

    `max_items=0` drains until a stop reason, bounded by `DRAIN_CEILING`.

    `architect=True` turns the `needs_architect` halt into one bounded call:
    the item goes to `project/architecture.py`, which may widen its acceptance
    from the project's own declared checks and nothing else. Off by default,
    because letting a model change the definition of done is a decision somebody
    makes rather than one a loop assumes. `propose` is passed straight through
    as the plan source, so a caller may supply one without a provider.

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
            make_runner=Runner, make_budget=RunBudget,
            architect=architect, architect_provider=architect_provider,
            propose=propose, compile_plans=compile_plans,
            isolate=isolate, policy=policy)
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
              static, max_steps, budget, make_graph, make_runner, make_budget,
              architect=False, architect_provider=None, propose=None,
              compile_plans=False, isolate=False, policy=""):
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
    # AN ITEM PAST THE FAST PATH NEEDS A PLAN, AND THAT IS WHAT A PLAN IS FOR.
    #
    # `needs_architect` fires on a missing acceptance check or high uncertainty.
    # Neither covers the ordinary case the adaptive policy routes to
    # COMPILED_SERIAL: an item that is perfectly gradeable and simply too wide
    # for one call. Without this, such an item reached the compiler with no plan
    # to compile and fell back to the generic graph — which is the shape the
    # pilot priced at 3x.
    #
    # Asking here is the whole Fable-shaped claim: ONE deep call decides how the
    # work splits, and cheap calls carry it out. It is bounded by
    # `ARCHITECT_CALL_CEILING` and by the Claude quota, and it happens only when
    # the class says the fast path was not enough.
    wants_plan = False
    if policy == "adaptive" and (architect or propose):
        from .execution_policy import (ExecutionClass, choose_execution,
                                       profile_item)
        shape = choose_execution(profile_item(item, store=store,
                                              project_id=project_id,
                                              worktree_available=isolate))
        wants_plan = (shape is ExecutionClass.COMPILED_SERIAL
                      and not item.planned_by)

    if item.needs_architect or wants_plan:
        if not (architect or propose):
            return None, (NEEDS_ARCHITECT, envelope.next_action)
        decision = _ask_the_architect(
            data_dir, project_id, item, envelope,
            provider=architect_provider, propose=propose)
        if decision.outcome != A.APPLIED:
            return None, (_STOP_FOR_OUTCOME[decision.outcome], decision.reason)
        # Re-read: the plan changed the item and bumped the portfolio, and the
        # copy in hand is the one from before that write.
        item = store.load_project(project_id)["portfolio"].get(
            item.work_item_id)
        if item.needs_architect:            # pragma: no cover - belt and brace
            return None, (NEEDS_ARCHITECT,
                          f"{item.work_item_id} is still ungradeable after an "
                          f"applied plan")

    # An applied plan describes HOW the work should be shaped, and until
    # `project/workorder.py` existed that description was recorded and then
    # discarded here — every item ran the same generic graph. Compiling it is
    # opt-in for the same reason `architect` is: letting a model shape what
    # executes is a decision somebody makes. `shape` is carried into the step
    # record because a compiled run and a generic one otherwise look identical,
    # and "was the plan actually executed" would be unanswerable again.
    from .workorder import choose_graph
    graph, shape = choose_graph(
        store, project_id, item, manifest=project["manifest"],
        make_graph=make_graph, provider=provider,
        execute_command=execute_command, static=static,
        compile_plans=compile_plans, policy=policy,
        worktree_available=isolate)
    # Isolation runs the graph somewhere the project is NOT, and the changes
    # come back only through `workspace.merge`'s gate. It makes PK-2 stricter
    # rather than looser: promotion still needs a SUCCEEDED run with a promoted
    # artifact, and now additionally that the change was allowed in.
    workspace_report = None
    if isolate:
        from .workspace import (MergeRefused, changed_paths, declared_write_set,
                                isolated, merge)
        allowed = declared_write_set(store, project_id, item)
        if not allowed:
            return None, (ISOLATION_UNAVAILABLE,
                          f"{item.work_item_id} has no declared write set: "
                          f"isolation was requested and its result could not be "
                          f"gated against anything. Apply a plan whose "
                          f"implementing step names a write_set, or run without "
                          f"--isolate")
        with isolated(root, label=item.work_item_id) as (tree, why):
            if tree is None:
                return None, (ISOLATION_UNAVAILABLE, why)
            runner = make_runner(tree, data_dir=data_dir)
            run_id = runner.start(item.outcome or item.title, graph,
                                  budget=budget or make_budget())
            attach_run(data_dir, item.work_item_id, run_id,
                       project_id=project_id)
            result = runner.run(run_id, budget=budget or make_budget(),
                                max_steps=max_steps)
            try:
                workspace_report = merge(
                    changed_paths(tree), worktree=tree, root=root,
                    allowed=allowed,
                    smoke=tuple(project["manifest"].smoke_checks))
            except MergeRefused as exc:
                workspace_report = {"merged": False, "refused": str(exc)}
    else:
        runner = make_runner(root, data_dir=data_dir)
        run_id = runner.start(item.outcome or item.title, graph,
                              budget=budget or make_budget())

        # Attached BEFORE the run, so a crash leaves an item that points at the
        # run rather than an orphan run and an item that looks untouched.
        attach_run(data_dir, item.work_item_id, run_id, project_id=project_id)
        result = runner.run(run_id, budget=budget or make_budget(),
                            max_steps=max_steps)

    # PK-2, applied by `close_session`: the RUN decides, not this function and
    # not whatever the report said. A refused merge withholds the promotion
    # instead of overriding it — an item whose changes never entered the project
    # has not done the work, however cleanly it did it elsewhere.
    merged_ok = workspace_report is None or workspace_report.get("merged")
    closed = close_session(data_dir, envelope.session_id, promote=merged_ok)
    if not merged_ok:
        blocked = store.load_project(project_id)["portfolio"].get(
            item.work_item_id)
        blocked.state = BLOCKED
        blocked.blocked_reason = (
            workspace_report.get("refused")
            or workspace_report.get("note")
            or "the isolated changes were not merged")
        store.update_item(
            blocked,
            expected_version=store.load_project(project_id)["portfolio"].version,
            reason="isolated run was not merged")
    judged = store.load_project(project_id)["portfolio"].get(item.work_item_id)

    step = {"session_id": envelope.session_id,
            "work_item_id": item.work_item_id,
            "title": item.title,
            "graph": shape,
            "workspace": workspace_report,
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
