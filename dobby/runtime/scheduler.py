"""Budgets and node selection.

Two jobs, kept apart because they answer different questions:

    RunBudget   may the run start ANOTHER node at all?
    Scheduler   given that it may, which node, and with what worker?

The budget is expressed per RUN rather than per call. "60 tool calls remaining"
is not a fact anyone can act on; "this must finish inside ten minutes and three
dollars, and nothing ships below the quality floor" is. The router's per-level
budgets remain the starting values — they are a good default and they are
already tested — and this converts them into ceilings the loop enforces.

Enforcement is at node ADMISSION, never mid-node. Killing a node halfway leaves
exactly the state this runtime exists to avoid: an effect that happened and was
never recorded. A run over budget stops admitting work and reports what it did
not start, which is a legible outcome; a run that kills its own worker is a
corrupted one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import graph as G
from .contracts import EXTERNAL_IRREVERSIBLE, NEEDS_APPROVAL


class BudgetExceeded(Exception):
    """Admission refused. Carries which ceiling, so the report can say."""

    def __init__(self, which: str, detail: str):
        super().__init__(f"{which}: {detail}")
        self.which = which
        self.detail = detail


@dataclass
class RunBudget:
    """Ceilings for one run. `None` means no ceiling of that kind.

    `quality_floor` is not enforced here — it is enforced by the verifier gate —
    but it is carried with the other ceilings because it is a budget in the only
    sense that matters: a run that cannot reach it must stop rather than ship.
    """

    max_attempts: int | None = 40
    deadline_s: float | None = 3600.0
    max_cost_usd: float | None = None
    #: How many EXTERNAL_IRREVERSIBLE nodes this run may perform. Defaults to
    #: zero: a run acquires the right to do something irreversible explicitly,
    #: it does not inherit it from having been started.
    max_irreversible: int = 0
    quality_floor: float = 1.0

    started_at: float = field(default_factory=time.monotonic)
    attempts_spent: int = 0
    cost_spent: float = 0.0
    irreversible_spent: int = 0

    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.started_at, 2)

    def remaining_s(self) -> float | None:
        if self.deadline_s is None:
            return None
        return round(max(0.0, self.deadline_s - self.elapsed_s()), 2)

    def admit(self, node) -> None:
        """Raise `BudgetExceeded` if this node must not start."""
        if self.max_attempts is not None and self.attempts_spent >= self.max_attempts:
            raise BudgetExceeded(
                "attempts",
                f"{self.attempts_spent} attempts spent of {self.max_attempts}")
        if self.deadline_s is not None and self.elapsed_s() >= self.deadline_s:
            raise BudgetExceeded(
                "deadline",
                f"{self.elapsed_s()}s elapsed of {self.deadline_s}s")
        if (self.max_cost_usd is not None
                and self.cost_spent >= self.max_cost_usd):
            raise BudgetExceeded(
                "cost", f"${self.cost_spent:.2f} spent of ${self.max_cost_usd:.2f}")
        if node.contract.side_effect_class == EXTERNAL_IRREVERSIBLE:
            if self.irreversible_spent >= self.max_irreversible:
                raise BudgetExceeded(
                    "irreversible",
                    f"node {node.node_id!r} is EXTERNAL_IRREVERSIBLE and this "
                    f"run is allowed {self.max_irreversible}")

    def charge(self, node, *, cost_usd: float = 0.0) -> None:
        self.attempts_spent += 1
        self.cost_spent += cost_usd
        if node.contract.side_effect_class == EXTERNAL_IRREVERSIBLE:
            self.irreversible_spent += 1

    def to_dict(self) -> dict:
        return {"max_attempts": self.max_attempts,
                "deadline_s": self.deadline_s,
                "max_cost_usd": self.max_cost_usd,
                "max_irreversible": self.max_irreversible,
                "quality_floor": self.quality_floor,
                "attempts_spent": self.attempts_spent,
                "cost_spent": round(self.cost_spent, 4),
                "irreversible_spent": self.irreversible_spent,
                "elapsed_s": self.elapsed_s(),
                "remaining_s": self.remaining_s()}

    @classmethod
    def from_dict(cls, raw: dict) -> "RunBudget":
        fields = {k: v for k, v in (raw or {}).items()
                  if k in ("max_attempts", "deadline_s", "max_cost_usd",
                           "max_irreversible", "quality_floor")}
        budget = cls(**fields)
        # Spend does NOT carry across a resume. The wall clock restarts because
        # a run that was paused overnight has not been running overnight, and
        # attempts already recorded are counted from the store instead — the
        # log is the truth, so a resumed budget reads it rather than trusting a
        # number that was serialized before the crash.
        return budget

    @classmethod
    def from_router_budgets(cls, budgets: dict, **overrides) -> "RunBudget":
        """Convert the router's per-level budget into run ceilings."""
        minutes = budgets.get("minutes")
        tool_calls = budgets.get("tool_calls")
        base = cls(
            max_attempts=int(tool_calls) if tool_calls else None,
            deadline_s=float(minutes) * 60 if minutes else None)
        for key, value in overrides.items():
            if value is not None and hasattr(base, key):
                setattr(base, key, value)
        return base


@dataclass
class Decision:
    """Why this node, this worker. Recorded so a run can be argued with."""

    node_id: str
    worker: str
    reason: str
    blocked: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "worker": self.worker,
                "reason": self.reason, "blocked": list(self.blocked)}


class Scheduler:
    """Chooses what runs next.

    Deliberately simple in this version: dependency order, then declaration
    order. No provider scoring, no hedging, no bandit. Those need per-node
    outcome data that does not exist until runs have been recorded, and a
    selection policy fitted to no data is a random policy with a formula in
    front of it. The store now records exactly the data they will need
    (attempts, failure classes, durations per worker), which is the point of
    doing this part first.
    """

    def __init__(self, budget: RunBudget, *, approvals: set | None = None):
        self.budget = budget
        #: Node ids a human has approved. Nothing else may run a node whose
        #: contract needs approval.
        self.approvals = set(approvals or ())

    def next_nodes(self, task_graph: "G.TaskGraph", *,
                   limit: int = 1) -> tuple[list[Decision], list[dict]]:
        """(runnable decisions, deferrals). Never raises for a full budget.

        Deferrals are returned rather than logged because "nothing ran and here
        is the list of reasons" is the answer to the only question an operator
        has about a stalled run.
        """
        decisions: list[Decision] = []
        deferred: list[dict] = []
        for node in task_graph.ready_nodes():
            if len(decisions) >= limit:
                deferred.append({"node_id": node.node_id,
                                 "reason": "concurrency limit"})
                continue
            if (node.contract.side_effect_class in NEEDS_APPROVAL
                    and node.node_id not in self.approvals):
                deferred.append({
                    "node_id": node.node_id,
                    "reason": f"{node.contract.side_effect_class} and not "
                              f"approved; approve it explicitly to proceed"})
                continue
            try:
                self.budget.admit(node)
            except BudgetExceeded as exc:
                deferred.append({"node_id": node.node_id,
                                 "reason": f"budget {exc.which}: {exc.detail}"})
                continue
            decisions.append(Decision(
                node.node_id, node.worker,
                reason=f"dependencies satisfied ({', '.join(node.depends_on) or 'none'})"))
        return decisions, deferred
