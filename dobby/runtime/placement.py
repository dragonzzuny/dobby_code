"""Which provider runs this node — decided from what was measured.

Separate from the router on purpose. The router answers a POLICY question, once,
before anything runs: how much agency does this task need, which tier of model,
which skills, what budget. That answer is deterministic and explainable and
should not change because a provider is rate-limited this afternoon.

This answers a PLACEMENT question, every time a node starts: given that the task
needs a large model, which of the ones installed here should do THIS node right
now. That answer must change with the afternoon.

The utility
-----------
    U(p|n) = wq·q̂(p,n) − wc·ĉ(p) − wl·l̂(p,n) − wr·r̂(p,n)

`q̂` is the share of that provider's attempts on that kind of node that survived
the VERIFIER — not that exited zero. Optimising for exit codes selects for
providers that answer fast and wrongly.

`ĉ` is the catalog's declared cost tier, normalised to 0..1. It is an ORDERING
and is never presented as money; this engine cannot see money, and
`metrics.cost_per_verified_task` says so rather than inventing a figure.

`l̂` is p95 duration normalised against the slowest candidate. p95 and not the
mean, because the complaint is always about the slow calls.

`r̂` is the recent error signal that the circuit breaker also reads.

Only measurable values are used. Where a provider has no history the utility is
not computed at all — see `UNKNOWN_PRIOR`.

What this deliberately is not
-----------------------------
Not a bandit. A contextual bandit needs a reward signal with enough samples per
arm to beat its own exploration cost, and this starts with zero samples. So the
policy is: try the unmeasured ones (that IS the exploration), rank the measured
ones by utility, and fall back to the catalog's static role preference when
nothing is measured at all. When the store has enough rows to fit something
better, the data will already be in the shape a bandit needs — which is the
reason the observation layer was built first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .contracts import HEDGEABLE
from .metrics import COST_RANK, DEFAULT_COST_RANK, ProviderStats

#: A provider with no record on this node kind scores as this, before weights.
#: Above the midpoint on purpose: an unmeasured provider is *worth trying*, and
#: an optimistic prior is the cheapest exploration policy there is. Set it to
#: 0.5 and a provider that fails once is never tried again on a thin record.
UNKNOWN_PRIOR = 0.75

#: Attempts below which a record is treated as thin — used, but reported as
#: provisional so a decision log does not read as if three samples were a
#: measurement.
THIN_RECORD = 5


@dataclass(frozen=True)
class Weights:
    """What the placement cares about, in one editable place."""

    quality: float = 1.0
    cost: float = 0.3
    latency: float = 0.2
    reliability: float = 0.5

    def to_dict(self) -> dict:
        return {"quality": self.quality, "cost": self.cost,
                "latency": self.latency, "reliability": self.reliability}


DEFAULT_WEIGHTS = Weights()

# -- the circuit breaker -----------------------------------------------------

CLOSED = "CLOSED"        # normal
OPEN = "OPEN"            # refusing, after consecutive failures
HALF_OPEN = "HALF_OPEN"  # one probe allowed

#: Consecutive verifier-failing attempts before a provider is taken out.
FAILURE_THRESHOLD = 3

#: How long it stays out before one probe is allowed through.
COOLDOWN_S = 120.0


@dataclass
class Breaker:
    """Per-provider trip state.

    Held in memory rather than in the store, and that limit is stated rather
    than hidden: two `dobby` processes each keep their own view, so a provider
    broken for one is not automatically avoided by the other. Persisting it
    would make one process's bad afternoon into a project-wide ban, which is
    worse — the failures that trip this are usually local (auth, a proxy, a
    rate-limit window tied to one machine).
    """

    provider: str
    state: str = CLOSED
    opened_at: float = 0.0
    consecutive_failures: int = 0

    def record(self, ok: bool, *, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        if ok:
            self.state = CLOSED
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= FAILURE_THRESHOLD:
            self.state = OPEN
            self.opened_at = now

    def allows(self, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        if self.state == CLOSED:
            return True
        if self.state == OPEN and now - self.opened_at >= COOLDOWN_S:
            self.state = HALF_OPEN
            return True
        return self.state == HALF_OPEN

    def to_dict(self) -> dict:
        return {"provider": self.provider, "state": self.state,
                "consecutive_failures": self.consecutive_failures}


@dataclass
class Placement:
    """The chosen provider and the whole argument for choosing it."""

    provider: str | None
    reason: str
    candidates: list = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    provisional: bool = False
    hedge_with: str | None = None
    #: The policy trace. Present on every placement, because "why this provider"
    #: has to be answerable from the record rather than re-derived — and because
    #: a REJECTION is the interesting half: an operator asking why agy never runs
    #: needs the rule that stopped it, not the absence of its name.
    provider_role: str = ""
    isolated: bool = False
    eligible: list = field(default_factory=list)
    rejected: dict = field(default_factory=dict)
    selection_basis: str = ""
    claude_cap_remaining: int | None = None

    def to_dict(self) -> dict:
        return {"provider": self.provider, "reason": self.reason,
                "candidates": list(self.candidates), "scores": dict(self.scores),
                "provisional": self.provisional, "hedge_with": self.hedge_with,
                "provider_role": self.provider_role, "isolated": self.isolated,
                "eligible": list(self.eligible), "rejected": dict(self.rejected),
                "selection_basis": self.selection_basis,
                "claude_cap_remaining": self.claude_cap_remaining}


class ProviderPlacement:
    """Ranks providers for a node from the recorded scorecard."""

    def __init__(self, store, *, weights: Weights = DEFAULT_WEIGHTS,
                 allow_network: bool = False, scorecard: dict | None = None,
                 available: "set | None" = None):
        self.store = store
        self.weights = weights
        self.allow_network = allow_network
        self._breakers: dict[str, Breaker] = {}
        #: Read once per placement pass. Recomputing it per candidate would
        #: re-read every run in the store for each provider.
        self._card = scorecard
        #: The fleet to place against, or None to ask this machine.
        #:
        #: The same kind of seam as `scorecard`: a fact placement reads from the
        #: environment, injectable by a caller that already knows it. A caller
        #: that supplies its own WORKERS -- a test driving the runtime with a
        #: fake adapter -- is such a caller, and had no way to say so.
        #:
        #: The cost of not having it: CI has been red for thirty consecutive
        #: runs, back to 2026-08-21, with 42 tests failing on
        #:
        #:     --override-provider claude is not selectable here: it is not a
        #:     candidate for this role.  candidates: []
        #:
        #: The runners have no agent CLI installed, which is correct and is
        #: exactly what an empty fleet means. The tests were exercising the
        #: RUNTIME with an injected worker and still consulting the real PATH
        #: for a binary they were never going to launch.
        #:
        #: `None` keeps the probe, so nothing in production changes; a set says
        #: "assume this fleet", which is a sentence a test can be held to.
        self._available = None if available is None else set(available)

    # -- state -------------------------------------------------------------
    def breaker(self, provider: str) -> Breaker:
        if provider not in self._breakers:
            self._breakers[provider] = Breaker(provider)
        return self._breakers[provider]

    def record_outcome(self, provider: str, ok: bool) -> None:
        self.breaker(provider).record(ok)

    def card(self) -> dict:
        if self._card is None:
            from .metrics import scorecard
            self._card = scorecard(self.store)
        return self._card

    def stats(self, provider: str, node_kind: str) -> ProviderStats:
        raw = self.card().get(f"{provider}/{node_kind}")
        entry = ProviderStats(provider, node_kind)
        if not raw:
            return entry
        entry.attempts = raw["attempts"]
        entry.successes = int(round((raw["success_rate"] or 0.0)
                                    * raw["attempts"]))
        entry.failure_classes = raw["failure_classes"]
        if raw["p95_s"] is not None:
            entry.durations_s = [raw["p95_s"]]
        entry.recent = ([True] * max(0, entry.attempts
                                     - raw["consecutive_failures"])
                        + [False] * raw["consecutive_failures"])
        return entry

    # -- candidates --------------------------------------------------------
    def eligible(self, node, *, context=None, avoid: set | None = None
                 ) -> tuple[list, dict]:
        """`(eligible, rejected)` for THIS node's role. Policy first, scores never.

        This used to return every installed provider and let the scorecard sort
        them out. That made `providers/policy.py` decorative: the role tables
        existed and no traffic saw them, so "codex is the default implementer"
        was a sentence in a file rather than a thing that happened.

        Four refusals, cheapest first, and each one is RECORDED rather than
        silently dropping a name:

            not installed        `detect` says it is not usable here
            on the avoid list    it just failed and the policy says move
            policy               `admissible()` — isolation, write grant, role
            quota                the operator's cap, which is not a tie-break
        """
        from ..providers.detect import available_ids
        from ..providers.policy import (ROLE_POLICY, admissible, governed,
                                        node_role_for, PlacementContext)

        context = context or PlacementContext()
        avoid = set(avoid or ())
        role = node.config.get("provider_role") or node_role_for(node.kind)
        policy = ROLE_POLICY.get(role)

        usable = (set(self._available) if self._available is not None
                  else set(available_ids(allow_network=self.allow_network)))
        rejected: dict = {}
        eligible: list = []

        if policy is None:                       # pragma: no cover - guard
            return sorted(usable - avoid), {"*": f"no policy for role {role!r}"}

        if not (usable & governed()):
            # The policy governs the catalog's providers and the operator's
            # constraint is written about those. A fleet it has never heard of
            # is outside its scope, and refusing every node on such a machine
            # would be the table asserting authority it does not have. Recorded,
            # never silent — and unreachable on any machine with claude, codex
            # or agy installed.
            rejected["*"] = ("no installed provider is governed by the role "
                             "policy; selecting from the fleet as configured")
            free = sorted(usable - avoid)
            # A node that named its preferences still gets them first. Dropping
            # that ordering here would make the scope statement quietly change
            # behaviour it has no opinion about.
            preferred = [p for p in (node.config.get("fallback_providers") or [])
                         if p in free]
            return preferred + [p for p in free if p not in preferred], rejected

        for pid in policy.candidates:
            if pid not in usable:
                rejected[pid] = "not installed or not usable on this machine"
                continue
            if pid in avoid:
                rejected[pid] = ("on the avoid list from the last failure; "
                                 "retrying the same provider is what the "
                                 "classification said not to do")
                continue
            ok, why = admissible(pid, policy, isolated=context.isolated)
            if not ok:
                rejected[pid] = why
                continue
            allowed, why_not = context.quota_allows(pid)
            if not allowed:
                rejected[pid] = why_not
                continue
            eligible.append(pid)
        return eligible, rejected

    def candidates(self, node, *, avoid: set | None = None) -> list[str]:
        """Kept for callers that only want the list. See `eligible`."""
        return self.eligible(node, avoid=avoid)[0]

    # -- scoring -----------------------------------------------------------
    def score(self, provider: str, node_kind: str, *,
              worst_p95: float | None = None,
              typical_latency: float | None = None) -> tuple[float, dict]:
        """Utility for one provider, and the terms that produced it."""
        from ..providers.catalog import registry
        stats = self.stats(provider, node_kind)

        quality = stats.success_rate
        measured = quality is not None
        if not measured:
            quality = UNKNOWN_PRIOR

        try:
            tier = registry().get(provider).cost_tier
        except Exception:                      # noqa: BLE001 - unknown provider
            tier = "standard"
        cost = COST_RANK.get(tier, DEFAULT_COST_RANK) / 3.0

        p95 = stats.p95_s
        if p95 is not None and worst_p95:
            latency = min(1.0, p95 / worst_p95)
        else:
            # An unmeasured provider is assumed TYPICAL, not fast. Scoring it 0
            # here made "never tried" the best possible latency, which stacks a
            # second advantage on top of the optimistic quality prior — measured
            # on the placement tests, a provider with no record beat one with a
            # 0.9 success rate and a p95 five times better than the field.
            # Exploration is supposed to come from the prior alone.
            latency = (typical_latency if typical_latency is not None
                       else 0.0)

        reliability_penalty = min(
            1.0, stats.consecutive_failures / float(FAILURE_THRESHOLD))

        utility = (self.weights.quality * quality
                   - self.weights.cost * cost
                   - self.weights.latency * latency
                   - self.weights.reliability * reliability_penalty)
        terms = {"quality": round(quality, 4), "quality_measured": measured,
                 "attempts": stats.attempts, "cost_tier": tier,
                 "cost": round(cost, 4), "latency": round(latency, 4),
                 "p95_s": p95, "reliability_penalty": round(
                     reliability_penalty, 4),
                 "utility": round(utility, 4)}
        return utility, terms

    # -- the decision ------------------------------------------------------
    def choose(self, node, *, avoid: set | None = None, context=None,
               override: str | None = None) -> Placement:
        """Pick a provider for `node`, with the argument recorded.

        Order of resort, and each step says which one it took:
        1. an explicitly configured provider that is still allowed;
        2. the highest utility among candidates with a record;
        3. an unmeasured candidate — trying it IS the exploration;
        4. the catalog's static role preference, when nothing is measured;
        5. nothing, and the reason.
        """
        from ..providers.policy import (PlacementContext, first_preferred,
                                        node_role_for)

        node_kind = node.kind
        context = context or PlacementContext()
        role = node.config.get("provider_role") or node_role_for(node_kind)
        eligible, rejected = self.eligible(node, context=context, avoid=avoid)

        tripped = [p for p in eligible if not self.breaker(p).allows()]
        for pid in tripped:
            rejected[pid] = "tripped by the circuit breaker"
        allowed = [p for p in eligible if self.breaker(p).allows()]

        cap = context.preferences.cap_for("claude")
        remaining = (None if cap is None or cap.max_calls is None
                     else max(0, cap.max_calls - context.spent("claude")))

        def placed(provider, reason, basis, **kw):
            return Placement(provider, reason, candidates=allowed,
                             provider_role=role, isolated=context.isolated,
                             eligible=list(eligible), rejected=dict(rejected),
                             selection_basis=basis,
                             claude_cap_remaining=remaining, **kw)

        # B. An override is for reproducing a run, not for leaving the policy.
        # A pin that could bypass isolation or a quota would make both of those
        # advisory, and the whole point of them is that they are not.
        if override:
            if override in allowed:
                return placed(override,
                              f"explicitly overridden to {override}",
                              "explicit_override",
                              scores={override: self.score(override,
                                                           node_kind)[1]})
            return placed(
                None,
                (f"--override-provider {override} is not selectable here: "
                 f"{rejected.get(override, 'it is not a candidate for this role')}"
                 f". An override may reproduce a run; it may not leave the "
                 f"policy"),
                "override_refused")

        # A. Nothing eligible is a stop with the reasons attached, not a silent
        # fallback to whoever is installed.
        if not allowed:
            return placed(
                None,
                ("no provider may fill this role here"
                 + (f"; {tripped} tripped the circuit breaker" if tripped
                    else "")
                 + ("; the avoid list from the last failure removed the rest"
                    if avoid else "")),
                "no_eligible_provider")

        p95s = [self.stats(p, node_kind).p95_s for p in allowed]
        observed = [v for v in p95s if v is not None]
        worst = max(observed, default=None)
        typical = (sum(min(1.0, v / worst) for v in observed) / len(observed)
                   if observed and worst else None)

        scored = {}
        for provider in allowed:
            _, terms = self.score(provider, node_kind, worst_p95=worst,
                                  typical_latency=typical)
            scored[provider] = terms

        measured = [p for p in allowed if scored[p]["quality_measured"]]

        if not measured:
            # C. No calibrated model yet, so the operator's constraint decides.
            # Ranking three providers on one measurement would be a formula
            # pretending to be evidence, and it would quietly prefer whichever
            # provider is least instrumented — `providers/usage.py` can price
            # exactly one of them.
            chosen = first_preferred(allowed, role, context.preferences)
            return placed(
                chosen or allowed[0],
                (f"nothing measured for {role!r} yet; using the "
                 f"subscription-first order so an unfitted formula does not "
                 f"pretend to be a measurement"),
                "subscription_first_static_preference",
                scores=scored, provisional=True)

        # One comparison, over every candidate. An untried provider competes
        # here on the optimistic prior — that IS the exploration, and it is why
        # there is no separate "should I explore" branch. The branch that used
        # to be here compared a fully penalised utility against a bare quality
        # term, so a candidate could lose the ranking and win the tie-break.
        best = max(allowed, key=lambda p: scored[p]["utility"])
        thin = scored[best]["attempts"] < THIN_RECORD
        if scored[best]["quality_measured"]:
            reason = (f"highest utility {scored[best]['utility']} from "
                      f"{scored[best]['attempts']} recorded attempt(s)")
        else:
            reason = (f"highest utility {scored[best]['utility']}, and it has "
                      f"no record here — an untried provider competes on the "
                      f"optimistic prior, which is the exploration")

        return placed(best, reason, "measured_utility", scores=scored,
                      provisional=thin,
                      hedge_with=self._hedge_partner(node, best, allowed,
                                                     scored))

    def _static_preference(self, node_kind: str,
                           allowed: list[str]) -> str | None:
        """The catalog's role ordering, when there is nothing measured."""
        from ..providers.detect import resolve_role
        role = NODE_KIND_ROLE.get(node_kind, "implement")
        chosen = resolve_role(role, allow_network=self.allow_network)
        return chosen if chosen in allowed else None

    def _hedge_partner(self, node, chosen: str, allowed: list[str],
                       scored: dict) -> str | None:
        """A second provider to race, or None — and it is usually None.

        Hedging is allowed ONLY for a node whose contract says it touches
        nothing outside the run. Racing a node with side effects sends the email
        twice, and no latency win is worth that. It is also pointless without a
        measured p95 to be slow against, so an unmeasured node never hedges.
        """
        if node.contract.side_effect_class not in HEDGEABLE:
            return None
        if not node.config.get("hedge"):
            return None
        others = [p for p in allowed if p != chosen]
        if not others:
            return None
        if scored[chosen]["p95_s"] is None:
            return None
        return max(others, key=lambda p: scored[p]["utility"])


#: Which catalog role a node kind resembles, for the static fallback only.
NODE_KIND_ROLE = {
    "plan": "draft",
    "execute": "implement",
    "verify": "critic",
    "report": "synthesize",
    "review": "critic",
    "research": "scout",
}


class ConcurrencyLimiter:
    """Global and per-provider caps on simultaneous calls.

    Two ceilings because they protect different things. The global one protects
    the machine — every concurrent agent is a real model session with its own
    memory footprint. The per-provider one protects the run from the provider's
    rate limiter, which turns excess parallelism into serialized retries: slower
    than a queue and more expensive, because the retries are billed.
    """

    def __init__(self, *, total: int = 4, per_provider: int = 2):
        import threading
        self.total = threading.Semaphore(total)
        self.per_provider_limit = per_provider
        self._per_provider: dict = {}
        self._guard = threading.Lock()
        self._threading = threading

    def _for(self, provider: str):
        with self._guard:
            if provider not in self._per_provider:
                self._per_provider[provider] = self._threading.Semaphore(
                    self.per_provider_limit)
            return self._per_provider[provider]

    def acquire(self, provider: str, *, timeout: float | None = None) -> bool:
        """Take both slots, or neither. Returns False on timeout.

        Both-or-neither matters: taking the global slot and then failing to take
        the provider slot would hold a machine-wide resource while waiting for a
        provider-specific one, which is how a fan-out deadlocks itself.
        """
        if not self.total.acquire(timeout=timeout):
            return False
        if not self._for(provider).acquire(timeout=timeout):
            self.total.release()
            return False
        return True

    def release(self, provider: str) -> None:
        self._for(provider).release()
        self.total.release()
