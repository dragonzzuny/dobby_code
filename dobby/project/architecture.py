"""The architect boundary: the one place a model may change the plan.

`needs_architect` was already the right signal — an item with no
machine-checkable acceptance, or with too much uncertainty, is one whose result
nobody could grade — and the loop already stopped on it. Stopping is safe and it
is not progress. This turns that stop into a bounded transaction:

    ArchitectureRequest  ->  PlanSpec  ->  PlanDecision  ->  portfolio

WHAT THE ARCHITECT MAY AND MAY NOT DO
-------------------------------------
The temptation is to let the architect write acceptance checks freely. That is
precisely the failure this kernel exists to prevent: an architect that invents
`echo ok` as the definition of done has not planned the work, it has removed the
gate. So v1 is deliberately narrow, and every widening below is a separate,
visible decision rather than a default:

    already in the manifest's smoke checks or the item's own acceptance
                                            -> may be applied automatically
    adds to the item's acceptance           -> may be applied automatically
    removes or weakens any existing check   -> REJECTED, always
    a command the manifest never declared   -> NEEDS_HUMAN_APPROVAL
    a command `guard_command` calls destructive -> NEEDS_HUMAN_APPROVAL, named
    a dependency on an item that does not exist -> REJECTED
    a dependency that would close a cycle    -> REJECTED
    raising the side-effect class            -> NEEDS_HUMAN_APPROVAL
    new top-level work items                 -> REJECTED in v1
    no gradeable acceptance, but read-only discovery proposed
                                            -> NEEDS_DISCOVERY

`NEEDS_DISCOVERY` is a fourth outcome rather than a flavour of "needs a human",
because it is the one case where the architect did its job correctly and the
answer is more evidence rather than a decision. Nothing here executes those
steps yet — the compiler that would is a later change — so the honest result is
a named stop and a recorded plan, not a run.

READ-ONLY, AND WHY THAT IS A REAL CLAIM HERE
--------------------------------------------
The provider is invoked through the catalog's own argv with NO `write_extra`.
That tuple is the opt-in that puts a CLI into a state where it may edit files;
withholding it is how `swebench` distinguishes a read pass from a write one, and
`claude`'s catalog argv ends in `--permission-mode plan` for the same reason.
The architect therefore returns a document, and this module is the only thing
that changes the portfolio.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from ..core.security import guard_command, load_protected
from .models import ProjectError, digest_of

#: The logical role, resolved through the provider catalog like every other one.
ARCHITECT_ROLE = "architect"

#: Distinct architect calls allowed per (work item, trigger). Two, and the number
#: is an argument rather than a preference:
#:
#:   the FIRST call asks the question.
#:   the SECOND asks it again carrying whatever the first said was missing —
#:     which is the only retry the evidence actually licenses, and it is
#:     reachable only because the request digest changed, meaning the item, the
#:     tree or the evidence really did move.
#:   a THIRD is the loop paying a model to rediscover that a person has to
#:     decide this one.
#:
#: A ceiling of 1 would look stricter and would be wrong: it makes the legitimate
#: retry unreachable, so an operator who supplies exactly the evidence the
#: architect asked for gets refused for supplying it. Unbounded is what this
#: replaces — `decision_for` deduped the SAME question and nothing bounded a
#: sequence of slightly different ones.
ARCHITECT_CALL_CEILING = 2

#: Written into the refusal's reason so the budget check can tell its own
#: refusals apart from real decisions and not charge the item for them.
BUDGET_MARKER = "architect budget"

# -- why the architect was called --------------------------------------------
MISSING_ACCEPTANCE = "MISSING_ACCEPTANCE"
HIGH_UNCERTAINTY = "HIGH_UNCERTAINTY"
REPLAN = "REPLAN"

TRIGGERS = (MISSING_ACCEPTANCE, HIGH_UNCERTAINTY, REPLAN)

# -- what came of it ----------------------------------------------------------
APPLIED = "APPLIED"
NEEDS_DISCOVERY = "NEEDS_DISCOVERY"
NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_APPROVAL"
REJECTED = "REJECTED"

OUTCOMES = (APPLIED, NEEDS_DISCOVERY, NEEDS_HUMAN_APPROVAL, REJECTED)

#: Side-effect classes an applied plan may declare without a human. Anything
#: above these is the runtime's approval path, not this one's.
SAFE_SIDE_EFFECTS = ("NONE", "LOCAL_WRITE")

#: The document the architect is asked for. Shown to it verbatim: a model told
#: "return JSON" and a model shown the shape produce different rates of the
#: contract violation this module would otherwise have to reject.
PLAN_SCHEMA = {
    "type": "object",
    "required": ["objective", "proposed_acceptance_checks"],
    "properties": {
        "objective": {"type": "string"},
        "proposed_acceptance_checks": {
            "type": "array", "items": {"type": "string"}},
        "discovery_steps": {"type": "array", "items": {"type": "object"}},
        "execution_steps": {"type": "array", "items": {"type": "object"}},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "side_effect_class": {"type": "string"},
        "new_work_items": {"type": "array", "items": {"type": "object"}},
        "risk_notes": {"type": "array", "items": {"type": "string"}},
    },
}


class PlanRejected(ProjectError):
    """The proposal could not be read as a plan at all."""


@dataclass(frozen=True)
class ArchitectureRequest:
    """Why the architect is being called, and against which world.

    The digest is what makes repeated calls cheap and honest. It covers the
    contract, the tree, and the item's own gradeability — so asking twice about
    the same item in the same state returns the first answer instead of paying
    for a second opinion that has no new evidence behind it.
    """

    project_id: str
    work_item_id: str
    trigger: str
    manifest_digest: str
    baseline_git_sha: str
    session_id: str = ""
    item_uncertainty: int = 0
    acceptance_checks: tuple = ()
    evidence_refs: tuple = ()
    #: `WorkItem.architect_contract_digest` — everything about the item that
    #: reaches the prompt, including the title and outcome the three fields above
    #: do not cover. Kept ALONGSIDE them rather than replacing them: those three
    #: each independently move identity today and a caller may build a request
    #: without an item at all, where this is empty and they are all there is.
    item_contract: str = ""
    created_at: str = ""

    def __post_init__(self):
        if self.trigger not in TRIGGERS:
            raise ProjectError(
                f"unknown architecture trigger {self.trigger!r}; expected one "
                f"of {TRIGGERS}")
        if not self.created_at:
            object.__setattr__(self, "created_at",
                               time.strftime("%Y-%m-%dT%H:%M:%S"))

    @property
    def request_id(self) -> str:
        return self.digest[:16]

    @property
    def digest(self) -> str:
        """Identity, deliberately excluding the clock and the session.

        Two sessions that hit the same wall on the same tree are asking one
        question. Folding `created_at` or `session_id` in here would make every
        call unique and the dedupe below dead code.
        """
        return digest_of({
            "project_id": self.project_id,
            "work_item_id": self.work_item_id,
            "trigger": self.trigger,
            "manifest_digest": self.manifest_digest,
            "baseline_git_sha": self.baseline_git_sha,
            "item_uncertainty": self.item_uncertainty,
            "acceptance_checks": sorted(self.acceptance_checks),
            "evidence_refs": sorted(self.evidence_refs),
            "item_contract": self.item_contract,
        })

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("acceptance_checks", "evidence_refs"):
            out[key] = list(getattr(self, key))
        out["request_id"] = self.request_id
        out["digest"] = self.digest
        return out


@dataclass(frozen=True)
class PlanSpec:
    """What the architect proposes. Data, never an instruction to obey."""

    plan_id: str
    work_item_id: str
    objective: str = ""
    proposed_acceptance_checks: tuple = ()
    discovery_steps: tuple = ()
    execution_steps: tuple = ()
    dependencies: tuple = ()
    side_effect_class: str = "NONE"
    new_work_items: tuple = ()
    risk_notes: tuple = ()
    evidence_refs: tuple = ()

    @classmethod
    def from_payload(cls, payload, *, work_item_id: str) -> "PlanSpec":
        """Read a provider's answer, or refuse it.

        Refusing loudly matters more here than anywhere else in the kernel: a
        half-read plan whose acceptance list came back as a string rather than a
        list would otherwise be applied one character at a time.
        """
        if not isinstance(payload, dict):
            raise PlanRejected(
                f"the architect returned {type(payload).__name__}, not a JSON "
                f"object")
        missing = [k for k in ("objective", "proposed_acceptance_checks")
                   if k not in payload]
        if missing:
            raise PlanRejected(f"the plan omits {missing}")

        def as_tuple(key, of=str):
            value = payload.get(key) or []
            if isinstance(value, (str, bytes)) or not isinstance(value, list):
                raise PlanRejected(
                    f"{key!r} must be a list, got {type(value).__name__}")
            for entry in value:
                if of is str and not isinstance(entry, str):
                    raise PlanRejected(
                        f"{key!r} must hold strings; found "
                        f"{type(entry).__name__}")
                if of is dict and not isinstance(entry, dict):
                    raise PlanRejected(
                        f"{key!r} must hold objects; found "
                        f"{type(entry).__name__}")
            return tuple(value)

        objective = payload.get("objective")
        if not isinstance(objective, str):
            raise PlanRejected("'objective' must be a string")

        checks = as_tuple("proposed_acceptance_checks")
        return cls(
            plan_id=digest_of({"work_item_id": work_item_id,
                               "objective": objective,
                               "checks": list(checks),
                               "steps": list(payload.get("execution_steps")
                                             or [])})[:16],
            work_item_id=work_item_id,
            objective=objective,
            proposed_acceptance_checks=checks,
            discovery_steps=as_tuple("discovery_steps", dict),
            execution_steps=as_tuple("execution_steps", dict),
            dependencies=as_tuple("dependencies"),
            side_effect_class=str(payload.get("side_effect_class") or "NONE"),
            new_work_items=as_tuple("new_work_items", dict),
            risk_notes=as_tuple("risk_notes"))

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("proposed_acceptance_checks", "discovery_steps",
                    "execution_steps", "dependencies", "new_work_items",
                    "risk_notes", "evidence_refs"):
            out[key] = list(getattr(self, key))
        return out


@dataclass(frozen=True)
class PlanDecision:
    """What this module did about the plan, and on what grounds."""

    request_digest: str
    plan_id: str | None
    outcome: str
    reason: str
    portfolio_version: int | None = None
    applied_checks: tuple = ()
    decided_at: str = ""

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ProjectError(
                f"unknown plan outcome {self.outcome!r}; expected one of "
                f"{OUTCOMES}")
        if not self.decided_at:
            object.__setattr__(self, "decided_at",
                               time.strftime("%Y-%m-%dT%H:%M:%S"))

    @property
    def applied(self) -> bool:
        return self.outcome == APPLIED

    def to_dict(self) -> dict:
        out = asdict(self)
        out["applied_checks"] = list(self.applied_checks)
        return out


# -- validation ---------------------------------------------------------------

def allow_list(manifest, item) -> tuple:
    """Commands an applied plan may use, in the order a report should read.

    The manifest's smoke checks are the project's own declaration of how it is
    checked, and the item's existing acceptance was already accepted by whoever
    wrote the portfolio. Nothing else is on this list, which is the whole reason
    an architect cannot quietly redefine done.
    """
    seen, out = set(), []
    for command in tuple(manifest.smoke_checks) + tuple(item.acceptance_checks):
        if command not in seen:
            seen.add(command)
            out.append(command)
    return tuple(out)


def _would_cycle(item_id: str, new_deps, by_id) -> str:
    """The dependency that closes a cycle, or ""."""
    adjacency = {i.work_item_id: list(i.depends_on) for i in by_id.values()}
    adjacency[item_id] = list(new_deps)
    for dep in new_deps:
        stack, seen = [dep], set()
        while stack:
            current = stack.pop()
            if current == item_id:
                return dep
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, ()))
    return ""


def evaluate(plan: PlanSpec, *, item, manifest, portfolio,
             protected=None) -> tuple:
    """`(outcome, reason, checks)` — what may be done with this plan.

    Pure. It reads the plan, the item and the contract, and decides; nothing
    here writes, so the rules can be exercised without a store, a provider or a
    project on disk.
    """
    by_id = portfolio.by_id()
    existing = tuple(item.acceptance_checks)
    proposed = tuple(plan.proposed_acceptance_checks)

    if plan.new_work_items:
        return (REJECTED,
                f"the plan creates {len(plan.new_work_items)} new work "
                f"item(s); decomposing a portfolio is a separate decision and "
                f"this path may only change the item it was called for",
                ())

    dropped = [c for c in existing if c not in proposed]
    if dropped:
        return (REJECTED,
                f"the plan drops acceptance check(s) already on this item: "
                f"{dropped}. An architect may add to the definition of done "
                f"and may never narrow it",
                ())

    if plan.side_effect_class not in SAFE_SIDE_EFFECTS:
        return (NEEDS_HUMAN_APPROVAL,
                f"the plan declares side effect class "
                f"{plan.side_effect_class!r}; anything beyond "
                f"{list(SAFE_SIDE_EFFECTS)} is approved by a person, not here",
                ())

    unknown_deps = [d for d in plan.dependencies if d not in by_id]
    if unknown_deps:
        return (REJECTED,
                f"the plan depends on {unknown_deps}, which are not in this "
                f"portfolio — an unknown dependency is unmet, not absent",
                ())
    cycle = _would_cycle(item.work_item_id, plan.dependencies, by_id)
    if cycle:
        return (REJECTED,
                f"depending on {cycle!r} would close a cycle back to "
                f"{item.work_item_id}", ())

    if not proposed:
        if plan.discovery_steps:
            return (NEEDS_DISCOVERY,
                    f"the architect proposed {len(plan.discovery_steps)} "
                    f"read-only discovery step(s) and no gradeable acceptance, "
                    f"which is the correct answer to an under-evidenced item. "
                    f"Nothing here executes them yet",
                    ())
        return (REJECTED,
                "the plan proposes neither an acceptance check nor a discovery "
                "step, so it leaves the item exactly as ungradeable as it was",
                ())

    allowed = set(allow_list(manifest, item))
    unknown = [c for c in proposed if c not in allowed]
    if unknown:
        destructive = []
        for command in unknown:
            ok, why = guard_command(command, protected or load_protected(None))
            if not ok:
                destructive.append(f"{command!r} ({why})")
        if destructive:
            return (NEEDS_HUMAN_APPROVAL,
                    f"the plan introduces command(s) the manifest never "
                    f"declared, and they are destructive: {destructive}",
                    ())
        return (NEEDS_HUMAN_APPROVAL,
                f"the plan introduces command(s) the manifest never declared: "
                f"{unknown}. Add them to the manifest's smoke checks if they "
                f"are how this project is checked",
                ())

    return (APPLIED,
            f"every proposed check is already declared by this project "
            f"({len(proposed)} check(s)), and none of the item's existing "
            f"acceptance was dropped",
            proposed)


# -- the provider call --------------------------------------------------------

def build_prompt(request: ArchitectureRequest, *, item, manifest) -> str:
    """What the architect is shown. The allow-list is shown too, on purpose.

    An architect that does not know which commands are acceptable spends a call
    proposing ones this module will refuse. Telling it the boundary up front
    turns a rejection into a plan.
    """
    allowed = allow_list(manifest, item)
    return "\n".join([
        "You are the ARCHITECT for one work item in an existing project.",
        "You do not write code and nothing you return edits anything. You",
        "return one JSON document describing how this item becomes gradeable.",
        "",
        f"## The item ({item.work_item_id})",
        f"title: {item.title}",
        f"outcome: {item.outcome or '(none recorded)'}",
        f"uncertainty: {item.uncertainty}",
        f"existing acceptance checks: {list(item.acceptance_checks) or 'NONE'}",
        f"why you were called: {request.trigger}",
        "",
        "## Commands you may propose as acceptance",
        "These, and ONLY these, can be applied without a human. They are the",
        "project's declared way of checking itself:",
        *(f"  {c}" for c in allowed),
        "",
        "If none of them can grade this item, propose read-only",
        "`discovery_steps` instead and leave `proposed_acceptance_checks`",
        "empty. That is a correct answer, not a failure. Do NOT invent a",
        "command to make the item look gradeable.",
        "",
        "You may never remove or weaken an existing acceptance check.",
        "",
        "## Required output",
        "Reply with ONE JSON document and nothing else, satisfying:",
        json.dumps(PLAN_SCHEMA, ensure_ascii=False, indent=1),
    ])


def propose_via_provider(request: ArchitectureRequest, *, item, manifest,
                         provider: str | None = None,
                         allow_network: bool = False,
                         timeout_s: int | None = None) -> dict:
    """Ask a provider for a plan, and refuse one that came with an edit.

    The docstring here used to read "READ-ONLY by construction", on the grounds
    that `write_extra` is never passed. That was wrong, and this repository's own
    catalog was already the counter-evidence: `write_extra=()` says what this
    harness declined to send, and `agy` was measured writing files under exactly
    the argv this function uses. Read-only is now enforced in two places that
    fail differently, because neither is sufficient alone:

      routing   `catalog.READ_ONLY_ROLES` will not resolve this role to a
                provider recorded as RO_DENIED. A rule about a list.
      detection `readonly.run_read_only` fingerprints the tree either side of
                the call and discards the plan if it moved. A fact about the
                tree, and the one that would catch a provider whose CLAIMED
                read-only mode turns out to be the next agy.
    """
    from ..providers.catalog import registry
    from ..providers.detect import resolve_role
    from ..runtime.workers import extract_json
    from .readonly import ReadOnlyViolation, run_read_only

    provider_id = provider or resolve_role(ARCHITECT_ROLE,
                                           allow_network=allow_network)
    if not provider_id:
        raise PlanRejected(
            f"no provider on this machine fills the {ARCHITECT_ROLE!r} role; "
            f"install one or pass --provider")
    spec = registry().get(provider_id)
    try:
        result = run_read_only(
            spec, build_prompt(request, item=item, manifest=manifest),
            root=manifest.root, timeout_s=timeout_s, role=ARCHITECT_ROLE)
    except ReadOnlyViolation as exc:
        # Surfaced as a rejected plan so the loop's existing stop path carries
        # it, rather than as a crash that loses the request already recorded.
        raise PlanRejected(str(exc)) from exc
    if not result.ok:
        raise PlanRejected(
            f"the architect provider {provider_id} failed: "
            f"{result.error or 'no output'}")
    payload = extract_json(result.text)
    if payload is None:
        raise PlanRejected(
            f"the architect ({provider_id}) answered in prose where a JSON "
            f"plan was required")
    return payload


# -- the controller -----------------------------------------------------------

def trigger_for(item) -> str:
    """Why this item needs an architect. Never guessed from the text."""
    if not item.acceptance_checks:
        return MISSING_ACCEPTANCE
    return HIGH_UNCERTAINTY


def request_architecture(data_dir: str, work_item_id: str, *,
                         project_id: str | None = None,
                         session_id: str = "",
                         trigger: str | None = None,
                         propose=None, provider: str | None = None,
                         allow_network: bool = False,
                         ceiling: int = ARCHITECT_CALL_CEILING) -> PlanDecision:
    """Call the architect for one item and settle what it proposed.

    `propose` is the seam. It takes the request and returns the raw plan
    payload; the default asks a provider. Injecting it is how the rules above
    are tested without a model, and it is also how a caller can supply a plan
    from somewhere else entirely — a human, a file, a different harness.

    Ordering is the crash-safety argument. The REQUEST is recorded before the
    provider is called, so dying mid-call leaves a request nobody answered and a
    portfolio nobody touched. The plan, the decision and the portfolio change
    are then one transaction, so there is no state in which a plan was recorded
    as applied and the item did not change.
    """
    from .store import ProjectStore

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    portfolio = project["portfolio"]
    manifest = project["manifest"]
    item = portfolio.get(work_item_id)
    if item is None:
        raise ProjectError(
            f"no work item {work_item_id!r} in {project['project_id']!r}")

    baseline = project["baseline"]
    request = ArchitectureRequest(
        project_id=project["project_id"], work_item_id=work_item_id,
        trigger=trigger or trigger_for(item),
        manifest_digest=project["manifest_digest"],
        baseline_git_sha=(baseline.git_sha if baseline else ""),
        session_id=session_id,
        item_uncertainty=item.uncertainty,
        acceptance_checks=tuple(item.acceptance_checks),
        evidence_refs=tuple(item.evidence_refs),
        item_contract=item.architect_contract_digest)

    settled = store.decision_for(request.project_id, request.digest)
    if settled is not None:
        # Same item, same contract, same tree, same evidence. A second call
        # would buy a second opinion with nothing new behind it.
        return PlanDecision(**settled)

    # Checked AFTER the dedupe, deliberately: a repeat of a question already
    # answered costs nothing and must keep returning its answer even once the
    # budget is spent. What the ceiling bounds is NEW questions.
    spent = [r for r in store.settled_requests(
        request.project_id, work_item_id, trigger=request.trigger)
        if BUDGET_MARKER not in (
            store.decision_for(request.project_id, r.get("digest", "")) or {}
        ).get("reason", "")]
    if len(spent) >= max(1, int(ceiling)):
        decision = PlanDecision(
            request_digest=request.digest, plan_id=None,
            outcome=NEEDS_HUMAN_APPROVAL,
            reason=(f"{BUDGET_MARKER} for {work_item_id} on trigger "
                    f"{request.trigger} is spent: {len(spent)} call(s) already "
                    f"reached a decision and the ceiling is {ceiling}. The "
                    f"evidence has changed enough to make this a new question "
                    f"and not enough to make it a different one — which is the "
                    f"point at which more planning stops being the missing "
                    f"input. A person decides this item, or supplies the "
                    f"discovery the earlier call asked for"))
        store.open_architecture_request(request)
        store.settle_architecture(request, None, decision)
        return decision

    store.open_architecture_request(request)

    proposer = propose or (lambda req: propose_via_provider(
        req, item=item, manifest=manifest, provider=provider,
        allow_network=allow_network))
    try:
        payload = proposer(request)
        plan = PlanSpec.from_payload(payload, work_item_id=work_item_id)
    except PlanRejected as exc:
        decision = PlanDecision(request_digest=request.digest, plan_id=None,
                                outcome=REJECTED, reason=str(exc))
        store.settle_architecture(request, None, decision)
        return decision

    outcome, reason, checks = evaluate(
        plan, item=item, manifest=manifest, portfolio=portfolio,
        protected=load_protected(_config(manifest.root)))

    if outcome != APPLIED:
        decision = PlanDecision(request_digest=request.digest,
                                plan_id=plan.plan_id, outcome=outcome,
                                reason=reason)
        store.settle_architecture(request, plan, decision)
        return decision

    item.acceptance_checks = list(checks)
    item.depends_on = sorted(set(item.depends_on) | set(plan.dependencies))
    # What answered the question, so the next selector does not ask it again.
    item.planned_by = plan.plan_id
    decision = PlanDecision(request_digest=request.digest,
                            plan_id=plan.plan_id, outcome=APPLIED,
                            reason=reason, applied_checks=tuple(checks))
    version = store.settle_architecture(request, plan, decision, item=item,
                                        expected_version=portfolio.version)
    return PlanDecision(request_digest=request.digest, plan_id=plan.plan_id,
                        outcome=APPLIED, reason=reason,
                        portfolio_version=version,
                        applied_checks=tuple(checks),
                        decided_at=decision.decided_at)


def _config(root: str) -> dict:
    """The host project's `.dobby/config.json`, for `protected_paths`."""
    import os
    path = os.path.join(root, ".dobby", "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}
