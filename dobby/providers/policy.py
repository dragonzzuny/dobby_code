"""Who may do which role, and what has to be true before they are allowed to.

WHY THIS IS SEPARATE FROM THE EXECUTION CLASS

`project/execution_policy.py` decides how much STRUCTURE work gets. This decides
WHO does it. Keeping them apart is the point: a provider being briefly slow must
not turn a one-shot task into a multi-agent one, and a capability only one
provider has must be able to change the shape without anyone editing a routing
table.

WHY A GLOBAL `--provider` IS THE WRONG SHAPE

`project run --provider X` pins planner, implementer, critic and report to one
CLI. That makes the role table decorative and it makes the critic the author,
which the orchestrator elsewhere goes to some trouble to prevent. A policy names
CANDIDATES per role and the placement layer picks among them on measured
evidence.

THE HARD CONSTRAINTS COME FIRST, AND THEY ARE NOT SCORES

A scorecard ranks providers that are ALLOWED. Whether a provider is allowed is
not a matter of degree:

- `agy` may fill a role only inside an isolated workspace, and never with the
  original root as its cwd. This is not a preference. `providers/catalog.py`
  records the probe: `--mode plan` created a file in all four mode/permission
  combinations, so its `read_only_default` is RO_DENIED and the only thing that
  has ever contained it here is the directory it was launched in.
- a writing role requires a verified `write_extra`. A provider whose write flag
  nobody has probed is refused rather than run read-only into a call that
  succeeds and changes nothing — the exact defect measured on 2026-08-22.

UNMEASURED ECONOMICS ARE NOT CHEAP ECONOMICS

`providers/usage.py` parses claude's usage envelope and nothing else's. So the
harness currently knows what claude costs and does not know what codex or agy
cost. Treating "no cost data" as "low cost" would route work to whichever
provider is least instrumented, which is an accident dressed as an optimisation.
`economics_status` carries `unmeasured` explicitly and the cost term is neutral
until a role has real samples.
"""

from __future__ import annotations

import dataclasses

#: The default focused implementer. `codex exec -s workspace-write` is recorded
#: in the catalog as editing inside the working directory and refusing outside
#: it, which is the narrowest write grant any provider here offers.
IMPLEMENT = "implement"
#: Work that must not see the original tree. Agy's home.
ISOLATED_DELEGATE = "isolated_delegate"
CRITIC = "critic"
ARCHITECT = "architect"
#: Reading the tree to find out what is true, in place and without writing.
#: NOT the isolated delegate: mapping scouting onto that made every compiled
#: plan's first step unreachable without a worktree, and a read-only look at the
#: project one is an ordinary thing to want.
SCOUT = "scout"

#: Samples of a (provider, role) pair below which its economics are not a number
#: anybody should route on. Eight, matching `runtime/bench.MIN_TASKS`, and for
#: the same reason: below it a single unlucky run moves the ranking.
MIN_ECONOMIC_SAMPLES = 8


@dataclasses.dataclass(frozen=True)
class ProviderPolicy:
    """What a role allows, before anything is scored."""

    role: str
    candidates: tuple = ()
    #: The provider must run in a workspace that is not the project.
    requires_isolation: bool = False
    #: The provider may be launched with the project root as its cwd.
    original_root_ok: bool = True
    #: The role writes, so a verified write grant is mandatory.
    writes: bool = False
    max_effort: str = "medium"
    allow_network: bool = False
    rationale: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["candidates"] = list(self.candidates)
        return d


ROLE_POLICY: dict[str, ProviderPolicy] = {
    IMPLEMENT: ProviderPolicy(
        role=IMPLEMENT,
        # Codex first: its write grant is the narrowest on offer — inside the
        # working directory, refusing outside it — and the catalog records that
        # as measured rather than documented.
        # agy is LISTED and gated rather than excluded. `admissible()` refuses
        # it whenever `isolated` is false, because its `read_only_default` is
        # RO_DENIED — so it can only ever be reached here as a fallback once a
        # fresh worktree exists, which is exactly the operator's intent:
        # use the subscription often, never on the original tree.
        candidates=("codex", "agy", "claude"),
        original_root_ok=True, writes=True, max_effort="high",
        rationale=("the default focused implementer is codex; agy is reachable "
                   "only with isolation and claude only within its cap")),
    ISOLATED_DELEGATE: ProviderPolicy(
        role=ISOLATED_DELEGATE,
        candidates=("agy", "codex"),
        requires_isolation=True, original_root_ok=False, writes=True,
        max_effort="high",
        rationale=("broad investigation, bulk generation and wide refactors, "
                   "run somewhere the project is not")),
    CRITIC: ProviderPolicy(
        role=CRITIC,
        candidates=("codex", "agy", "claude", "gemini"),
        original_root_ok=True, writes=False, max_effort="medium",
        rationale=("advisory only; the orchestrator additionally excludes the "
                   "provider that authored the thing under review")),
    SCOUT: ProviderPolicy(
        role=SCOUT,
        # agy is listed and gated: read-only in the original tree is exactly
        # what it was measured not to honour, so `admissible` refuses it here
        # unless a worktree exists.
        candidates=("codex", "agy", "claude", "gemini"),
        original_root_ok=True, writes=False, max_effort="medium",
        rationale=("read the tree and report; the cheapest useful role and the "
                   "one a compiled plan starts with")),
    ARCHITECT: ProviderPolicy(
        role=ARCHITECT,
        candidates=("claude", "codex", "gemini"),
        original_root_ok=True, writes=False, max_effort="high",
        rationale=("agy is excluded by READ_ONLY_ROLES, having been measured "
                   "writing under the argv this role uses")),
}


class PolicyRefused(ValueError):
    """A provider may not fill this role here, and this says which rule stopped it."""


def admissible(provider_id: str, policy: ProviderPolicy, *,
               isolated: bool = False, spec=None) -> tuple[bool, str]:
    """`(allowed, why_not)` — the hard constraints, before any score.

    Returns the reason even when allowed, so a run record can say what was
    checked rather than only what was chosen.
    """
    from .catalog import registry
    from .base import RO_DENIED

    spec = spec or registry().get(provider_id)

    if provider_id not in policy.candidates:
        return False, (f"{provider_id} is not a candidate for {policy.role!r} "
                       f"{list(policy.candidates)}")
    if policy.requires_isolation and not isolated:
        return False, (f"{policy.role!r} runs only in an isolated workspace and "
                       f"none was provided")
    if not policy.original_root_ok and not isolated:
        return False, (f"{provider_id} may not be launched against the project "
                       f"root for {policy.role!r}")
    if spec.read_only_default == RO_DENIED and not isolated:
        return False, (f"{provider_id} was measured writing files under its "
                       f"default argv, so it runs only where a write is "
                       f"contained; see providers/catalog.py")
    if policy.writes and not spec.write_extra:
        return False, (f"nobody has verified how to grant {provider_id} edit "
                       f"rights, and running it read-only produces a call that "
                       f"succeeds and changes nothing")
    return True, "hard constraints satisfied"


def candidates_for(role: str, *, isolated: bool = False,
                   availability=None) -> list[str]:
    """Providers that MAY fill `role` here, in preference order, before scoring."""
    from .detect import survey

    policy = ROLE_POLICY.get(role)
    if policy is None:
        return []
    avail = availability if availability is not None else survey(False)
    out = []
    for pid in policy.candidates:
        entry = avail.get(pid)
        if entry is None or not entry.usable:
            continue
        ok, _ = admissible(pid, policy, isolated=isolated)
        if ok:
            out.append(pid)
    return out


def economics(scorecard: dict, provider_id: str, role: str) -> dict:
    """What is actually known about this pair's cost, including that nothing is.

    `usd_per_verified` stays None until there are real samples, and
    `economics_status` says which of the two situations produced it. A router
    that read a missing cost as a low one would send work to whichever provider
    is least instrumented.
    """
    key = f"{provider_id}/{role}"
    row = (scorecard or {}).get(key) or {}
    samples = int(row.get("cost_samples") or 0)
    measured = samples >= MIN_ECONOMIC_SAMPLES and row.get("usd_per_verified")
    return {
        "provider": provider_id,
        "role": role,
        "usd_per_verified": row.get("usd_per_verified") if measured else None,
        "cost_samples": samples,
        "economics_status": "measured" if measured else "unmeasured",
        "note": ("" if measured else
                 f"{samples} cost sample(s), below the {MIN_ECONOMIC_SAMPLES} "
                 f"this router requires. Unmeasured is not cheap: this pair is "
                 f"ranked on verified success and latency only"),
    }


# --------------------------------------------------------------------------
# Quota: the operator's constraint, which is not the same as cost
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ProviderCap:
    """A ceiling on one provider, expressed in what the operator actually owns.

    `cost_tier` cannot say this. An Agy subscription already paid for and a
    Codex allowance are not USD, and an operator who wants Claude used sparingly
    is stating a constraint about their own budget rather than a belief about
    which model is better. So the cap is a COUNT, enforced before any score.

    `fallback_allowed=False` is the important default: a capped provider that is
    still reachable by falling back is not capped, it is discouraged, and the
    difference shows up as a bill.
    """

    max_calls: int | None = None
    #: Enforced only where the provider reports it. `providers/usage.py` parses
    #: claude's envelope and nothing else's, so a cap here on an unmeasured
    #: provider would be a limit nobody could apply.
    max_thinking_tokens: int | None = None
    fallback_allowed: bool = False


@dataclasses.dataclass(frozen=True)
class ProviderPreferences:
    """Who to prefer once eligibility is settled. Never a way around eligibility.

    `subscription_first` is the honest name for the default: it ranks by what the
    operator has already paid for, NOT by measured economics, because
    `providers/usage.py` can price exactly one provider and ranking three on one
    measurement would be a formula pretending to be evidence.
    """

    mode: str = "subscription_first"
    preferred: tuple = ("agy", "codex")
    caps: dict = dataclasses.field(default_factory=lambda: {
        "claude": ProviderCap(max_calls=2, fallback_allowed=False)})

    def cap_for(self, provider_id: str) -> ProviderCap | None:
        return self.caps.get(provider_id)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "preferred": list(self.preferred),
                "caps": {k: dataclasses.asdict(v)
                         for k, v in self.caps.items()}}


#: Order used while a role has too few measurements to rank on. Encodes the
#: operator's constraint, and every entry is still filtered by `admissible()`
#: first — Agy appears in `implement` only so it can be a FALLBACK once an
#: isolated workspace exists, never as the first pick on the original tree.
STATIC_SUBSCRIPTION_FIRST = {
    IMPLEMENT: ("codex", "agy", "claude"),
    ISOLATED_DELEGATE: ("agy", "codex"),
    SCOUT: ("codex", "agy", "claude"),
    CRITIC: ("codex", "agy", "claude"),
    # claude LEADS here and nowhere else. This is the one role where model
    # depth is the product: a plan is read once and then executed N times, so a
    # bad plan is paid for N times over while a good one is paid for once.
    #
    # It is also the whole tension in "spend less on claude", made explicit:
    # claude for judgement, never for typing. Bounded by
    # `architecture.ARCHITECT_CALL_CEILING` and by the quota ledger, which
    # refuses a third call outright.
    ARCHITECT: ("claude", "codex"),
}


@dataclasses.dataclass(frozen=True)
class PlacementContext:
    """What the scheduler knows that the node does not.

    `isolated` is the load-bearing field. It is not a hint: it decides whether a
    provider measured writing under a read-only flag may run at all.
    """

    isolated: bool = False
    original_root: str = ""
    preferences: ProviderPreferences = dataclasses.field(
        default_factory=ProviderPreferences)
    #: Calls already spent per provider this session, counted at the adapter.
    provider_calls: dict = dataclasses.field(default_factory=dict)

    def spent(self, provider_id: str) -> int:
        return int(self.provider_calls.get(provider_id, 0))

    def quota_allows(self, provider_id: str) -> tuple[bool, str]:
        """`(allowed, why_not)` — the cap, checked before anything is scored."""
        cap = self.preferences.cap_for(provider_id)
        if cap is None or cap.max_calls is None:
            return True, ""
        spent = self.spent(provider_id)
        if spent < cap.max_calls:
            return True, ""
        return False, (f"{provider_id} has spent {spent}/{cap.max_calls} calls "
                       f"this session; the cap is the operator's budget and is "
                       f"not a tie-break")

    def to_dict(self) -> dict:
        return {"isolated": self.isolated,
                "original_root": self.original_root,
                "preferences": self.preferences.to_dict(),
                "provider_calls": dict(self.provider_calls)}


def first_preferred(eligible, role: str, preferences: ProviderPreferences
                    ) -> str | None:
    """The first eligible provider in the role's subscription-first order.

    The role order comes first and `preferences.preferred` breaks its ties, so an
    operator saying "prefer agy" cannot make agy the local implementer — that is
    a safety property and this is a preference.
    """
    order = STATIC_SUBSCRIPTION_FIRST.get(role, ())
    for pid in order:
        if pid in eligible:
            return pid
    for pid in preferences.preferred:
        if pid in eligible:
            return pid
    return eligible[0] if eligible else None


#: Node kind -> role. The scheduler asks the NODE first (`provider_role` in its
#: config) and falls back to this, so a graph builder that forgot to declare a
#: role still lands somewhere defensible rather than on whatever the catalog
#: happened to list first.
NODE_ROLE = {
    "implement": IMPLEMENT,
    "execute": IMPLEMENT,
    "patch": IMPLEMENT,
    "scout": SCOUT,
    "investigate": SCOUT,
    "critic": CRITIC,
    "judge": CRITIC,
    "review": CRITIC,
    "plan": ARCHITECT,
    "architect": ARCHITECT,
    "report": CRITIC,
}


def node_role_for(node_kind: str) -> str:
    return NODE_ROLE.get(node_kind, IMPLEMENT)


def governed() -> frozenset:
    """Every provider any role table mentions.

    The policy's SCOPE, stated rather than assumed. It governs the providers the
    catalog ships and the operator's constraint is written about; a caller who
    registers a fleet of their own is not covered by a table that has never
    heard of it, and pretending otherwise would make the policy refuse every
    node on such a machine.

    This is a scope boundary and not an escape hatch: it applies only when NONE
    of the installed providers is governed, and the placement records
    `ungoverned_fleet` when it fires. On a machine with claude, codex or agy
    installed it never does.
    """
    out: set = set()
    for policy in ROLE_POLICY.values():
        out.update(policy.candidates)
    return frozenset(out)
