"""Which work item is next — decided without calling a model.

A selector that asks an LLM "what should I do next" produces a different answer
each session, and the differences are not insight: they are the ordering noise
of a model reading a slightly different context. Long-horizon work needs the
opposite property. The same portfolio in the same state must yield the same next
item, so that a session which is interrupted and resumed continues rather than
reconsiders.

So the ranking is arithmetic and total: priority, then impact, then the least
uncertain, then the id. The id is the tie-break of last resort and it exists so
the function is a function — two candidates that are equal in every recorded
respect still have a deterministic order.

The one judgement left to a model is not *which* item but *whether this item is
ready to be implemented at all*. An item with high uncertainty or with no
machine-checkable acceptance gets `needs_architect`, because sending it to an
implementation worker now produces something nobody can grade.

Three invariants live here rather than in a worker, because a worker that
enforces its own preconditions is a worker that can decide not to:

    PK-1  a failing baseline yields no item at all
    PK-3  DONE is not selectable without an explicit reopen
    PK-5  recovery outranks new work
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (BLOCKED, CLOSED_STATES, DONE, IN_PROGRESS, SELECTABLE,
                     Portfolio, WorkItem)


@dataclass
class Selection:
    """The chosen item, or nothing and the reason — never a bare `None`.

    A selector that returns `None` makes the caller guess between "the project
    is finished", "the tree is broken", and "everything is blocked on a human".
    Those need three different responses.
    """

    item: WorkItem | None = None
    reason: str = ""
    needs_architect: bool = False
    needs_rebaseline: bool = False
    recovery: bool = False
    blocked: list = field(default_factory=list)
    considered: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"work_item_id": self.item.work_item_id if self.item else None,
                "title": self.item.title if self.item else None,
                "state": self.item.state if self.item else None,
                "reason": self.reason,
                "needs_architect": self.needs_architect,
                "needs_rebaseline": self.needs_rebaseline,
                "recovery": self.recovery,
                "blocked": list(self.blocked),
                "considered": list(self.considered)}


def dependencies_met(item: WorkItem, by_id: dict) -> bool:
    """Every dependency exists and is DONE.

    A dependency naming an item that is not in the portfolio counts as UNMET
    rather than as absent. The alternative — treating an unknown id as
    satisfied — turns a typo into a silently skipped prerequisite.
    """
    for dep_id in item.depends_on:
        dep = by_id.get(dep_id)
        if dep is None or dep.state != DONE:
            return False
    return True


def rank_key(item: WorkItem):
    """Total order. Highest priority, then highest impact, then least unknown."""
    return (-item.priority, -item.impact, item.uncertainty, item.work_item_id)


def select_next(portfolio: Portfolio, *, baseline=None,
                unconfirmed_effects: dict | None = None,
                active_work_item_id: str | None = None) -> Selection:
    """The next item to work on, or a refusal that says which kind it is."""
    by_id = portfolio.by_id()
    unconfirmed = unconfirmed_effects or {}

    # PK-1. A failing baseline stops everything. Implementing a feature on a
    # tree that does not build produces a diff whose test result means nothing,
    # and the failure surfaces later attributed to the wrong change.
    if baseline is None:
        return Selection(
            reason=("no baseline has been taken for this project, so nothing "
                    "is known about whether the tree is sound"),
            needs_rebaseline=True)
    if not baseline.passed:
        failing = [r.get("check") for r in baseline.smoke_results
                   if not r.get("passed")]
        return Selection(
            reason=("the baseline is failing, so no item may start: "
                    + (", ".join(str(c) for c in failing) if failing
                       else baseline.note or "no smoke check is defined")),
            needs_rebaseline=True)

    # PK-5. Recovery before new work, in two forms. An external effect that was
    # claimed and never confirmed is the most urgent thing in the system: the
    # outside world may have changed and nothing in here knows how.
    for item in sorted(portfolio.items, key=rank_key):
        if item.state in CLOSED_STATES:
            continue
        pending = unconfirmed.get(item.latest_run_id or "", [])
        if pending:
            return Selection(
                item=item, recovery=True,
                reason=(f"{item.work_item_id} has {len(pending)} external "
                        f"effect(s) claimed and never confirmed by run "
                        f"{item.latest_run_id}. Reconcile with the outside "
                        f"world before any new work"),
                considered=[i.work_item_id for i in portfolio.items])

    # An item left IN_PROGRESS by an interrupted session is resumed before
    # anything new is started, whatever the ranking says. Starting a second item
    # while the first is half-done is how a portfolio accumulates work in
    # flight that nobody finishes.
    in_flight = [i for i in portfolio.items if i.state == IN_PROGRESS]
    # Resume means IN_PROGRESS, and only that. Among items genuinely in flight
    # the previous session's own item WINS rather than merely joining the set:
    # ranking is the right tie-break between things not yet started, but between
    # two half-done items it is the wrong question, because the one this project
    # was last actually working on is the one whose branch and partial artifacts
    # exist. An earlier version prepended that candidate and then re-sorted the
    # list, which sorted the preference straight back out.
    #
    # An item the last envelope merely NAMED as next is not in flight. Treating
    # it as a resume would label ordinary selection as recovery, and — the part
    # that matters — would hand it out without passing the candidate filter
    # below, so a dependency that is not DONE would no longer stop it.
    resumed = None
    if active_work_item_id and active_work_item_id in by_id:
        candidate = by_id[active_work_item_id]
        if candidate.state == IN_PROGRESS:
            resumed = candidate
    if resumed is None and in_flight:
        resumed = sorted(in_flight, key=rank_key)[0]
    if resumed is not None:
        return Selection(
            item=resumed, recovery=True,
            reason=(f"{resumed.work_item_id} was left IN_PROGRESS by an earlier "
                    f"session; resuming it before starting anything new"),
            needs_architect=resumed.needs_architect,
            considered=[i.work_item_id for i in portfolio.items])

    # PK-3 is structural: DONE is not in SELECTABLE, so a finished item cannot
    # be chosen without `reopen` putting it back into OPEN.
    candidates = [i for i in portfolio.items
                  if i.state in SELECTABLE and dependencies_met(i, by_id)]
    startable = {i.work_item_id for i in candidates}
    # Compared by id, not by value: `WorkItem` is a plain dataclass, so `in`
    # would compare every field of every item against every candidate.
    blocked = [{"work_item_id": i.work_item_id,
                "state": i.state,
                "reason": i.blocked_reason or _unmet(i, by_id)}
               for i in portfolio.items
               if i.state not in CLOSED_STATES
               and i.work_item_id not in startable]

    if not candidates:
        remaining = portfolio.remaining()
        if not remaining:
            return Selection(reason="every work item is DONE or CANCELLED",
                             blocked=blocked)
        return Selection(
            reason=("nothing is startable: every remaining item is blocked, "
                    "waiting on a dependency, or needs a human"),
            blocked=blocked)

    item = min(candidates, key=rank_key)
    return Selection(
        item=item,
        reason=(f"highest ranked of {len(candidates)} startable item(s) "
                f"(priority {item.priority}, impact {item.impact}, "
                f"uncertainty {item.uncertainty})"),
        needs_architect=item.needs_architect,
        blocked=blocked,
        considered=[i.work_item_id for i in candidates])


def _unmet(item: WorkItem, by_id: dict) -> str:
    if item.state == BLOCKED:
        return "blocked"
    missing = [d for d in item.depends_on
               if by_id.get(d) is None or by_id[d].state != DONE]
    if missing:
        unknown = [d for d in missing if d not in by_id]
        if unknown:
            return (f"depends on {unknown}, which is not in this portfolio — "
                    f"an unknown dependency counts as unmet, not as absent")
        return f"waiting on {missing}"
    if item.state not in SELECTABLE:
        return f"state {item.state} is not selectable"
    return ""
