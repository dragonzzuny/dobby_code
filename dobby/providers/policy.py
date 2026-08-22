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
        candidates=("codex", "claude"),
        original_root_ok=True, writes=True, max_effort="high",
        rationale=("the default focused implementer. agy is absent here on "
                   "purpose: it is RO_DENIED and belongs in isolation")),
    ISOLATED_DELEGATE: ProviderPolicy(
        role=ISOLATED_DELEGATE,
        candidates=("agy", "codex"),
        requires_isolation=True, original_root_ok=False, writes=True,
        max_effort="high",
        rationale=("broad investigation, bulk generation and wide refactors, "
                   "run somewhere the project is not")),
    CRITIC: ProviderPolicy(
        role=CRITIC,
        candidates=("codex", "claude", "gemini"),
        original_root_ok=True, writes=False, max_effort="medium",
        rationale=("advisory only; the orchestrator additionally excludes the "
                   "provider that authored the thing under review")),
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
