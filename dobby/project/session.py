"""Opening and closing a shift.

A session is a handover, not a conversation. It opens by checking that the world
still matches the contract the last session left, and closes by writing down
what a stranger would need to continue — which is a short, specific list, and
notably does not include what anybody said.

The two rules that make it work:

    PK-4  a session whose manifest digest or baseline sha no longer matches the
          project does not start work; it asks for a re-baseline.
    PK-2  an item becomes DONE only when a runtime run says SUCCEEDED, that run
          promoted at least one artifact, and it left no unconfirmed external
          effect.

PK-2 is the one that keeps the portfolio honest. Every harness eventually grows
a path where "the agent said it finished" marks something complete; this store
cannot express that, because `close` reads the run rather than the report.
"""

from __future__ import annotations

import time

from .init import git_sha, repo_digest, take_baseline
from .models import (BLOCKED, DONE, IN_PROGRESS, ProjectError, SessionEnvelope,
                     VERIFYING)
from .select import select_next
from .store import ProjectStore, new_session_id


def _unconfirmed_by_run(run_store, run_ids) -> dict:
    """Unconfirmed external effects, keyed by run. Empty entries are dropped."""
    out = {}
    for run_id in {r for r in run_ids if r}:
        pending = run_store.unconfirmed_effects(run_id)
        if pending:
            out[run_id] = pending
    return out


def open_session(data_dir: str, *, project_id: str | None = None,
                 rebaseline: bool = False) -> SessionEnvelope:
    """Start a shift, or refuse to and say what has to happen first.

    The staleness check is PK-4 and it compares two things, because they fail
    differently. A changed **manifest digest** means the contract itself moved —
    a new smoke command, a different stack — so previous baselines describe a
    different project. A changed **git sha** means the tree moved, so the
    baseline describes different code. Either one makes the recorded soundness
    evidence about something else.
    """
    from ..runtime.store import RunStore

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    manifest = project["manifest"]
    portfolio = project["portfolio"]
    baseline = project["baseline"]

    # Recomputed live, not read back. The stored manifest IS the contract, so
    # comparing it to itself can never differ — the first version of this check
    # did exactly that and could not fail. What moves is the TREE, so that is
    # what gets measured here.
    current_sha = git_sha(manifest.root)
    current_repo = repo_digest(manifest.root)
    current_digest = manifest.manifest_digest
    stale = baseline is None or not baseline.matches(
        current_sha, current_digest, current_repo)

    if stale and rebaseline:
        baseline = take_baseline(manifest.root, manifest)
        store.set_baseline(manifest.project_id, baseline)
        stale = not baseline.matches(current_sha, current_digest, current_repo)

    if stale:
        why = ("no baseline has been recorded" if baseline is None else
               baseline.staleness(current_sha, current_digest, current_repo)
               or "the recorded baseline did not pass")
        envelope = SessionEnvelope(
            session_id=new_session_id(), project_id=manifest.project_id,
            portfolio_version=portfolio.version,
            manifest_digest=current_digest, baseline_git_sha=current_sha,
            active_work_item_id=None, needs_rebaseline=True,
            next_action=(f"re-baseline before working: {why}. Run "
                         f"`dobby session open --rebaseline`"))
        store.put_envelope(envelope)
        return envelope

    run_store = RunStore(data_dir)
    unconfirmed = _unconfirmed_by_run(
        run_store, [i.latest_run_id for i in portfolio.items])

    previous = store.latest_envelope(manifest.project_id)
    selection = select_next(
        portfolio, baseline=baseline, unconfirmed_effects=unconfirmed,
        active_work_item_id=(previous.active_work_item_id if previous
                             else None))

    verified: list[str] = []
    for item in portfolio.items:
        if item.state == DONE:
            verified.extend(item.evidence_refs)

    open_failures = [
        {"work_item_id": i.work_item_id, "state": i.state,
         "reason": i.blocked_reason, "latest_run_id": i.latest_run_id}
        for i in portfolio.items if i.state == BLOCKED]

    envelope = SessionEnvelope(
        session_id=new_session_id(), project_id=manifest.project_id,
        portfolio_version=portfolio.version, manifest_digest=current_digest,
        baseline_git_sha=current_sha,
        active_work_item_id=(selection.item.work_item_id if selection.item
                             else None),
        verified_artifact_ids=tuple(verified),
        open_failures=tuple(open_failures),
        unconfirmed_effects=tuple(
            {"run_id": run_id, "count": len(effects)}
            for run_id, effects in sorted(unconfirmed.items())),
        # Carried from the selection, not just from the staleness branch above.
        # A baseline can be current and still FAILING — the tree is the one the
        # checks ran against, and they said no. That reaches here rather than
        # the early return, and without this line the envelope reported
        # `needs_rebaseline=False` beside an empty `active_work_item_id`: a
        # caller keying on the flag saw a healthy shift with nothing to do.
        # `close_session` already set it; the two now agree.
        needs_rebaseline=selection.needs_rebaseline,
        next_action=_next_action(selection))
    store.put_envelope(envelope)
    return envelope


def _next_action(selection) -> str:
    if selection.item is None:
        return selection.reason
    if selection.recovery:
        return f"{selection.reason}"
    if selection.needs_architect:
        return (f"decide {selection.item.work_item_id} before implementing it: "
                f"{selection.reason}. It has uncertainty "
                f"{selection.item.uncertainty}"
                + ("" if selection.item.acceptance_checks else
                   " and no machine-checkable acceptance, so nothing could "
                   "grade the result"))
    return (f"implement {selection.item.work_item_id} — {selection.item.title}. "
            f"Done means: "
            + "; ".join(selection.item.acceptance_checks))


def attach_run(data_dir: str, work_item_id: str, run_id: str, *,
               project_id: str | None = None) -> dict:
    """Point a work item at the runtime run that is trying to satisfy it.

    Recorded before the run finishes, on purpose. A crash between starting a run
    and recording which item it belonged to leaves an orphan run and an item
    that looks untouched — and the next session then starts a second run for the
    same work.
    """
    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    portfolio = project["portfolio"]
    item = portfolio.get(work_item_id)
    if item is None:
        raise ProjectError(
            f"no work item {work_item_id!r} in {project['project_id']!r}")
    item.latest_run_id = run_id
    if item.state not in (IN_PROGRESS, VERIFYING, DONE):
        item.state = IN_PROGRESS
    version = store.update_item(item, expected_version=portfolio.version,
                                reason=f"attached run {run_id}")
    return {"work_item_id": work_item_id, "run_id": run_id,
            "state": item.state, "portfolio_version": version}


def promote_from_run(data_dir: str, work_item_id: str, *,
                     project_id: str | None = None,
                     run_id: str | None = None) -> dict:
    """Apply PK-2: the RUN decides whether the item is done, not the worker.

    Three conditions, and each one has failed on its own in this kit's history:
    a run that ended `WAITING` on a budget looks finished from the outside; a
    run that succeeded with every artifact REJECTED promoted nothing; and a run
    that claimed an external effect and died before confirming it has changed
    the world in a way nobody has checked.
    """
    from ..runtime import graph as G
    from ..runtime.store import RunStore

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    portfolio = project["portfolio"]
    item = portfolio.get(work_item_id)
    if item is None:
        raise ProjectError(f"no work item {work_item_id!r}")

    target = run_id or item.latest_run_id
    if not target:
        raise ProjectError(
            f"{work_item_id} has no run to be judged by; attach one first")

    run_store = RunStore(data_dir)
    run = run_store.load_run(target)
    promoted = run_store.artifacts(target, state="PROMOTED")
    pending = run_store.unconfirmed_effects(target)

    reasons = []
    if run["state"] != G.SUCCEEDED:
        reasons.append(f"the run ended {run['state']}, not SUCCEEDED")
    if not promoted:
        reasons.append("the run promoted no artifact, so it produced nothing "
                       "that passed a gate")
    if pending:
        reasons.append(
            f"{len(pending)} external effect(s) were claimed and never "
            f"confirmed; the outside world must be reconciled before this "
            f"counts as done")

    item.latest_run_id = target
    if reasons:
        item.state = BLOCKED
        item.blocked_reason = "; ".join(reasons)
    else:
        item.state = DONE
        item.blocked_reason = ""
        item.evidence_refs = sorted(
            set(item.evidence_refs) | {a["artifact_id"] for a in promoted})

    version = store.update_item(item, expected_version=portfolio.version,
                                reason=f"judged by run {target}")
    return {"work_item_id": work_item_id, "run_id": target,
            "state": item.state, "reasons": reasons,
            "evidence_refs": list(item.evidence_refs),
            "portfolio_version": version}


def close_session(data_dir: str, session_id: str, *,
                  promote: bool = True) -> SessionEnvelope:
    """End the shift: judge the active item by its run, then write the handover."""
    store = ProjectStore(data_dir)
    envelope = store.get_envelope(session_id)
    project = store.load_project(envelope.project_id)

    if promote and envelope.active_work_item_id:
        item = project["portfolio"].get(envelope.active_work_item_id)
        if item is not None and item.latest_run_id:
            # Judged here rather than reported here. The outcome lands in the
            # portfolio and in a `project_event`; the envelope below is written
            # from the refreshed portfolio, so it cannot disagree with it.
            promote_from_run(data_dir, item.work_item_id,
                             project_id=envelope.project_id)

    refreshed = store.load_project(envelope.project_id)
    portfolio = refreshed["portfolio"]
    baseline = refreshed["baseline"]

    from ..runtime.store import RunStore
    unconfirmed = _unconfirmed_by_run(
        RunStore(data_dir), [i.latest_run_id for i in portfolio.items])

    selection = select_next(portfolio, baseline=baseline,
                            unconfirmed_effects=unconfirmed)
    verified: list[str] = []
    for item in portfolio.items:
        if item.state == DONE:
            verified.extend(item.evidence_refs)

    closed = SessionEnvelope(
        session_id=session_id, project_id=envelope.project_id,
        portfolio_version=portfolio.version,
        manifest_digest=refreshed["manifest_digest"],
        baseline_git_sha=(baseline.git_sha if baseline else
                          envelope.baseline_git_sha),
        active_work_item_id=(selection.item.work_item_id if selection.item
                             else None),
        verified_artifact_ids=tuple(verified),
        open_failures=tuple(
            {"work_item_id": i.work_item_id, "state": i.state,
             "reason": i.blocked_reason, "latest_run_id": i.latest_run_id}
            for i in portfolio.items if i.state == BLOCKED),
        unconfirmed_effects=tuple(
            {"run_id": run_id, "count": len(effects)}
            for run_id, effects in sorted(unconfirmed.items())),
        next_action=_next_action(selection),
        needs_rebaseline=selection.needs_rebaseline,
        created_at=envelope.created_at,
        closed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.put_envelope(closed)
    return closed
