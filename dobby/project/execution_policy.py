"""Which SHAPE of execution a work item gets, decided before any provider is picked.

WHAT THE MEASUREMENT SAID

`evals/ab/RESULTS_pilot.md`, nine real runs on 2026-08-22: A_direct, B_gated and
C_dobby all verified 3/3 on the first pass. The only difference was the bill.

    provider calls      A 1.0     B 1.0     C 3.0      (3.00x)
    cost / verified     A $0.6178 B $0.6294 C $1.8150  (2.94x)
    thinking tokens     A 358     B 521     C 5,576    (15.58x)

B costs 1.9% more than A. So the GATE is nearly free — the effect contract, the
acceptance check and artifact promotion together add almost nothing — and the
whole overhead is `default_graph` making `plan` and `report` into provider nodes
on every item, whether or not the work needed planning.

THE POLICY THIS ENCODES

Default to executing and checking; escalate only on evidence. A harness earns its
place by REFUSING calls, not by generating them. Concretely:

    DIRECT_GATED            gradeable, scoped, first attempt      -> 1 call
    COMPILED_SERIAL         an applied plan with real steps       -> 1-3
    CODEX_FOCUSED_IMPLEMENT a scoped patch in the working tree    -> 1
    AGY_ISOLATED_DELEGATE   investigation or bulk work, isolated  -> 1 + gates
    ARCHITECT_REPLAN        a verified failure, or no acceptance  -> 1 + worker
    HUMAN_BOUNDARY          nothing a model may decide            -> 0

WHY THE PROFILE IS DETERMINISTIC

Every signal here is read from the item, the plan or the run store. Asking a
model "is this simple?" would add back the call the fast path exists to remove,
and it would answer differently on Tuesday. A model classifier is admissible
later as an ADVISORY signal, once an offline evaluation shows it beats these
rules — not before.

WHAT THIS IS NOT

It is not provider selection. An execution class says how much structure the work
gets; `ProviderPolicy` says who does it. Keeping them apart matters: a provider
being briefly slow must not turn a one-shot task into a multi-agent one, and a
capability only one provider has must be able to change the shape without
rewriting the routing table.
"""

from __future__ import annotations

import dataclasses
from enum import Enum

from .models import UNCERTAINTY_ESCALATION


class ExecutionClass(str, Enum):
    """How much structure this item gets. A policy decision, not a placement."""

    DIRECT_GATED = "DIRECT_GATED"
    COMPILED_SERIAL = "COMPILED_SERIAL"
    AGY_ISOLATED_DELEGATE = "AGY_ISOLATED_DELEGATE"
    CODEX_FOCUSED_IMPLEMENT = "CODEX_FOCUSED_IMPLEMENT"
    ARCHITECT_REPLAN = "ARCHITECT_REPLAN"
    HUMAN_BOUNDARY = "HUMAN_BOUNDARY"


#: Classes that run exactly one provider node. The measured point of all this.
SINGLE_CALL_CLASSES = frozenset({ExecutionClass.DIRECT_GATED,
                                 ExecutionClass.CODEX_FOCUSED_IMPLEMENT})

#: Above this many declared write paths, "scoped" stops being true and the work
#: gets the compiled path. Three, because a fix that touches four separate files
#: is one whose ordering somebody should have thought about — and because a
#: number with no argument behind it is a knob rather than a policy.
MAX_SCOPED_PATHS = 3

#: Side-effect classes the fast path may carry without a person. Mirrors
#: `architecture.SAFE_SIDE_EFFECTS`; anything above is the runtime's approval
#: path and reaching it from here would route around it.
SAFE_EFFECTS = ("NONE", "LOCAL_WRITE")


@dataclasses.dataclass(frozen=True)
class TaskProfile:
    """Deterministic signals about one item. Nothing here costs a provider call."""

    one_shot_plausible: bool = True
    acceptance_declared: bool = False
    expected_paths: tuple = ()
    uncertainty: int = 0
    #: Exclusive capabilities, which can change the SHAPE and not only the
    #: placement: a task needing live web cannot be done by a provider that has
    #: none, however well it would otherwise fit.
    requires_live_web: bool = False
    requires_codebase_investigation: bool = False
    expected_tool_calls: int | None = None
    #: Verified failures already recorded against this item. The trigger for
    #: spending an architect: a plan is worth paying for when there is evidence
    #: that the straightforward attempt did not work.
    prior_failures: int = 0
    side_effect_class: str = "NONE"
    worktree_available: bool = False
    has_compiled_plan: bool = False

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["expected_paths"] = list(self.expected_paths)
        return d


def profile_item(item, *, store=None, project_id: str | None = None,
                 worktree_available: bool = False,
                 one_shot_plausible: bool | None = None) -> TaskProfile:
    """Read an item's shape from what is already recorded about it.

    `one_shot_plausible` is taken from the caller when supplied — a corpus or an
    operator may know — and otherwise DERIVED from scope and uncertainty. It is
    never asked of a model.
    """
    expected: tuple = ()
    has_plan = False
    if store is not None and project_id and getattr(item, "planned_by", None):
        from .workspace import declared_write_set

        expected = tuple(declared_write_set(store, project_id, item))
        has_plan = bool(expected)
    if not expected:
        # An item may name its own scope without an architect ever running.
        expected = tuple(getattr(item, "expected_paths", ()) or ())

    uncertainty = int(getattr(item, "uncertainty", 0) or 0)
    scoped = len(expected) <= MAX_SCOPED_PATHS
    derived = scoped and uncertainty < UNCERTAINTY_ESCALATION

    # Caller first, then the ITEM's own declaration, then derivation. A path
    # count is a poor proxy: an S2 fixture touching three files scored as
    # "scoped" and went down the fast path with no plan, which is the measured
    # reason this field exists.
    declared = getattr(item, "one_shot_plausible", None)
    if one_shot_plausible is None:
        one_shot_plausible = declared

    return TaskProfile(
        one_shot_plausible=(derived if one_shot_plausible is None
                            else bool(one_shot_plausible)),
        acceptance_declared=bool(getattr(item, "acceptance_checks", None)),
        expected_paths=expected,
        uncertainty=uncertainty,
        # A recorded failure, not a guess: `blocked_reason` is written by
        # `close_session` when a run did not satisfy the item.
        prior_failures=(1 if getattr(item, "blocked_reason", "") else 0),
        # LOCAL_WRITE unless the item says otherwise, matching what
        # `runner.default_graph` has always assumed for its execute node.
        #
        # This defaulted to NONE-when-no-paths and the 4-arm pilot caught it
        # within twelve runs: items with no compiled plan profiled as read-only,
        # the fast path therefore granted no write, and all three tasks failed
        # 0/3 having been asked to edit a file they were not permitted to touch.
        # That is the same defect the write grant was built to fix, arriving
        # through a new door. An empty `expected_paths` means "check the tree",
        # not "this node writes nothing".
        side_effect_class=str(getattr(item, "side_effect_class", "")
                              or "LOCAL_WRITE"),
        worktree_available=worktree_available,
        has_compiled_plan=has_plan)


def choose_execution(profile: TaskProfile) -> ExecutionClass:
    """The execution class, as a total function of the profile.

    Ordered by what OUTRANKS what, and the order is the argument:

    1. no acceptance — nothing downstream can grade the result, so no amount of
       execution helps and the architect is the only move.
    2. an effect above LOCAL_WRITE — the runtime's approval path exists and this
       must not route around it.
    3. a prior verified failure — the straightforward attempt has been tried and
       did not work, which is exactly when a plan is worth paying for.
    4. an exclusive capability — changes the shape, not just the placement.
    5. scoped, low-uncertainty, one-shot — the fast path, and the default.
    6. everything else — the compiled path.
    """
    if not profile.acceptance_declared:
        return ExecutionClass.ARCHITECT_REPLAN
    if profile.side_effect_class not in SAFE_EFFECTS:
        return ExecutionClass.HUMAN_BOUNDARY
    if profile.prior_failures > 0:
        return ExecutionClass.ARCHITECT_REPLAN
    if profile.requires_live_web or profile.requires_codebase_investigation:
        # Isolation is a PRECONDITION, not a preference: the one provider with
        # these capabilities is the one measured writing files under a mode
        # documented as read-only, so without a worktree there is nowhere safe
        # to put it and a person decides.
        return (ExecutionClass.AGY_ISOLATED_DELEGATE
                if profile.worktree_available
                else ExecutionClass.HUMAN_BOUNDARY)
    if (profile.one_shot_plausible
            and len(profile.expected_paths) <= MAX_SCOPED_PATHS
            and profile.uncertainty < UNCERTAINTY_ESCALATION):
        return ExecutionClass.DIRECT_GATED
    return ExecutionClass.COMPILED_SERIAL


def explain(profile: TaskProfile, chosen: ExecutionClass) -> str:
    """Why this class, in one line, for the run record.

    A class with no stated reason is a routing decision nobody can argue with,
    and the whole point of making this deterministic was that it could be.
    """
    if chosen is ExecutionClass.ARCHITECT_REPLAN:
        return ("no acceptance check to grade the result"
                if not profile.acceptance_declared
                else f"{profile.prior_failures} prior verified failure(s)")
    if chosen is ExecutionClass.HUMAN_BOUNDARY:
        if profile.side_effect_class not in SAFE_EFFECTS:
            return (f"side effect {profile.side_effect_class} is above "
                    f"LOCAL_WRITE and needs the runtime's approval path")
        return ("an exclusive capability is required and no isolated workspace "
                "is available to run it in")
    if chosen is ExecutionClass.AGY_ISOLATED_DELEGATE:
        return "an exclusive capability is required, and a worktree exists"
    if chosen is ExecutionClass.DIRECT_GATED:
        return (f"gradeable, {len(profile.expected_paths)} declared path(s), "
                f"uncertainty {profile.uncertainty}, no prior failure")
    return (f"{len(profile.expected_paths)} declared path(s) or uncertainty "
            f"{profile.uncertainty} puts this past the fast path")


#: Which provider role each execution class asks for. Separate from the class
#: itself because the class says how much structure and the role says who — and
#: a capability changing the shape must not silently change the safety rules.
#:
#: NOT AN ENFORCEMENT POINT, and this is worth saying because it reads like one.
#: Nothing calls `provider_role_for`. Every rule it would carry is already
#: enforced closer to the thing it protects:
#:
#:     isolation for AGY_ISOLATED_DELEGATE   `choose_execution` refuses the
#:                                           class outright without a worktree
#:                                           and hands the item to a person, so
#:                                           the requirement is a PRECONDITION
#:                                           of the class rather than a property
#:                                           of the role it names
#:     the three fast-path classes           `placement` falls back to
#:                                           `node_role_for(node.kind)`, which
#:                                           answers "implement" — the same
#:                                           value this table gives them
#:     ARCHITECT_REPLAN, HUMAN_BOUNDARY      neither reaches a graph builder;
#:                                           `choose_graph` says explicitly that
#:                                           it is not that function's decision
#:
#: Kept rather than deleted because the mapping states an intent the code makes
#: true by other means, and a reader comparing the two should find the claim
#: written down. Audited 2026-08-25: wiring it changed no behaviour anywhere,
#: which is why it is documented as redundant instead of connected.
CLASS_ROLE = {
    ExecutionClass.DIRECT_GATED: "implement",
    ExecutionClass.CODEX_FOCUSED_IMPLEMENT: "implement",
    ExecutionClass.COMPILED_SERIAL: "implement",
    ExecutionClass.AGY_ISOLATED_DELEGATE: "isolated_delegate",
    ExecutionClass.ARCHITECT_REPLAN: "architect",
    ExecutionClass.HUMAN_BOUNDARY: "",
}


def provider_role_for(chosen: ExecutionClass) -> str:
    return CLASS_ROLE.get(chosen, "implement")
