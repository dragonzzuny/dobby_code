"""Availability detection and role resolution.

The harness must never claim a provider works because the catalog lists it.
This module is the single place where "declared" becomes "available", and it
distinguishes three states rather than two, because the fix differs for each:

- `available`   — binary on PATH (or api key present) and network gating passed.
- `absent`      — not installed. Fix: install it. Reported, not an error.
- `blocked`     — installed/declared but deliberately unusable here: an api
                  provider while `providers.allow_network` is false, or a
                  missing required env var. Fix: change config or set the var.

Keeping `blocked` separate from `absent` is what lets `dobby doctor` tell a user
"you have Kimi configured but network providers are off" instead of the useless
"kimi not found".

Detection is PATH-only (`shutil.which`) and env-only. It deliberately does NOT
execute the provider to test it: a real invocation costs money, can take a
minute, and — for tools that fall back to interactive mode on a bad flag — can
hang. A `--version` probe would avoid the cost but still proves nothing about
whether the one-shot flag works or whether auth is valid, so it would buy a
false sense of verification. Actual behaviour is verified by `dobby fleet probe`,
which the user runs knowingly.
"""

from __future__ import annotations

import dataclasses

from .base import ProviderSpec
from .catalog import (LOCAL_ONLY_ROLES, ROLE_ROUTING, registry,
                      role_preference)

AVAILABLE = "available"
ABSENT = "absent"
BLOCKED = "blocked"


@dataclasses.dataclass(frozen=True)
class Availability:
    """Why a provider can or cannot be used on this machine, right now."""

    id: str
    state: str
    detail: str
    path: str | None = None
    cost_tier: str = "standard"
    kind: str = "cli"
    verified_here: bool = False

    @property
    def usable(self) -> bool:
        return self.state == AVAILABLE

    def to_dict(self) -> dict:
        return dataclasses.asdict(self) | {"usable": self.usable}


def _platform_tag() -> str:
    import sys
    return sys.platform


def check(spec: ProviderSpec, allow_network: bool = False) -> Availability:
    """Classify one provider without invoking it."""
    common = {
        "id": spec.id,
        "cost_tier": spec.cost_tier,
        "kind": spec.kind,
        # `verified_here` says whether THIS platform is among the platforms
        # where the invocation was actually observed to work. A provider can be
        # available (binary present) yet unverified on this OS — worth knowing
        # before a fan-out depends on it.
        "verified_here": _platform_tag() in spec.verified_on,
    }
    if spec.kind == "api":
        if not allow_network:
            return Availability(
                state=BLOCKED,
                detail="api provider disabled: providers.allow_network is false "
                       "(enabling it adds a network egress path — see "
                       "docs/THREAT_MODEL.md)",
                **common)
        missing = spec.missing_env()
        if missing:
            return Availability(
                state=BLOCKED,
                detail=f"missing required env: {', '.join(missing)}",
                **common)
        return Availability(state=AVAILABLE, detail="api key present", **common)

    path = spec.which()
    if path is None:
        return Availability(
            state=ABSENT,
            detail=f"binary {spec.binary!r} not on PATH",
            **common)
    return Availability(state=AVAILABLE, detail=f"found at {path}",
                        path=path, **common)


def survey(allow_network: bool = False) -> dict[str, Availability]:
    """Classify the whole catalog. The basis of every routing decision."""
    reg = registry()
    return {s.id: check(s, allow_network=allow_network) for s in reg.all()}


def available_ids(allow_network: bool = False) -> list[str]:
    return [pid for pid, a in survey(allow_network).items() if a.usable]


def resolve_role(role: str, allow_network: bool = False,
                 exclude: set[str] | None = None,
                 availability: dict[str, Availability] | None = None
                 ) -> str | None:
    """Best available provider for `role`, or None if the machine has none.

    `exclude` is how the orchestrator enforces "the critic is not the author":
    it passes the drafting provider's id and gets a genuinely different one.
    Returning None (rather than falling back to an excluded provider) is
    deliberate — a self-review dressed up as an independent one is worse than an
    admitted gap, because it produces a confident PASS with no second opinion
    behind it.
    """
    avail = availability if availability is not None else survey(allow_network)
    banned = set(exclude or ())
    reg = registry()
    for pid in role_preference(role):
        if pid in banned:
            continue
        if role in LOCAL_ONLY_ROLES and reg.get(pid).kind == "api":
            # The aggregated context is the crown jewel; never ship it out.
            continue
        entry = avail.get(pid)
        if entry is not None and entry.usable:
            return pid
    return None


def resolve_panel(role: str, size: int, allow_network: bool = False,
                  availability: dict[str, Availability] | None = None
                  ) -> list[str]:
    """Up to `size` DISTINCT providers for a fan-out of `role`.

    Returns fewer than `size` when the machine has fewer usable providers, and
    never repeats one. Repeating a provider to hit a requested panel size would
    manufacture the appearance of independent opinions from one model — exactly
    the correlated-sampling failure that decorrelated fan-out exists to avoid
    (see dobby/swarm/diversity.py). The caller is told the real size and can
    decide whether that is enough.
    """
    if size <= 0:
        return []
    avail = availability if availability is not None else survey(allow_network)
    reg = registry()
    picked: list[str] = []
    for pid in role_preference(role):
        if len(picked) >= size:
            break
        if role in LOCAL_ONLY_ROLES and reg.get(pid).kind == "api":
            continue
        entry = avail.get(pid)
        if entry is not None and entry.usable and pid not in picked:
            picked.append(pid)
    return picked


def report(allow_network: bool = False) -> dict:
    """Doctor-style summary: what is usable, what is missing, what to do."""
    avail = survey(allow_network)
    usable = [a for a in avail.values() if a.usable]
    roles = {r: resolve_role(r, allow_network, availability=avail)
             for r in sorted(ROLE_ROUTING)}
    unfilled = sorted(r for r, p in roles.items() if p is None)
    return {
        "platform": _platform_tag(),
        "allow_network": allow_network,
        "providers": {pid: a.to_dict() for pid, a in sorted(avail.items())},
        "usable_count": len(usable),
        "usable_ids": sorted(a.id for a in usable),
        "roles": roles,
        "unfilled_roles": unfilled,
        # A single usable provider is not a fan-out. Say so plainly rather than
        # letting a caller believe a "panel" of one gives independent views.
        "multi_agent_ready": len(usable) >= 2,
        "max_panel_size": len(usable),
    }
