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

    def to_dict(self) -> dict:
        return {"provider": self.provider, "reason": self.reason,
                "candidates": list(self.candidates), "scores": dict(self.scores),
                "provisional": self.provisional, "hedge_with": self.hedge_with}


class ProviderPlacement:
    """Ranks providers for a node from the recorded scorecard."""

    def __init__(self, store, *, weights: Weights = DEFAULT_WEIGHTS,
                 allow_network: bool = False, scorecard: dict | None = None):
        self.store = store
        self.weights = weights
        self.allow_network = allow_network
        self._breakers: dict[str, Breaker] = {}
        #: Read once per placement pass. Recomputing it per candidate would
        #: re-read every run in the store for each provider.
        self._card = scorecard

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
    def candidates(self, node, *, avoid: set | None = None) -> list[str]:
        """Providers that are usable here and allowed for THIS node.

        Three filters, in the order that they refuse most cheaply: installed and
        working (`detect`), not on the node's avoid list, and permitted by the
        node's side-effect class.
        """
        from ..providers.detect import available_ids
        avoid = set(avoid or ())
        usable = [pid for pid in available_ids(allow_network=self.allow_network)
                  if pid not in avoid]
        preferred = node.config.get("fallback_providers") or []
        # Declared preference first, then the rest in catalog order, so a node
        # that names its preferences gets them and still has the others behind.
        ordered = [p for p in preferred if p in usable]
        ordered += [p for p in usable if p not in ordered]
        return ordered

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
    def choose(self, node, *, avoid: set | None = None) -> Placement:
        """Pick a provider for `node`, with the argument recorded.

        Order of resort, and each step says which one it took:
        1. an explicitly configured provider that is still allowed;
        2. the highest utility among candidates with a record;
        3. an unmeasured candidate — trying it IS the exploration;
        4. the catalog's static role preference, when nothing is measured;
        5. nothing, and the reason.
        """
        node_kind = node.kind
        allowed = [p for p in self.candidates(node, avoid=avoid)
                   if self.breaker(p).allows()]
        tripped = [p for p in self.candidates(node, avoid=avoid)
                   if not self.breaker(p).allows()]

        configured = node.config.get("provider")
        if configured and configured in allowed and not (avoid or set()) & {
                configured}:
            _, terms = self.score(configured, node_kind)
            return Placement(configured,
                             "the node names this provider and nothing "
                             "disqualifies it",
                             candidates=allowed, scores={configured: terms})

        if not allowed:
            return Placement(
                None,
                ("no usable provider for this node"
                 + (f"; {tripped} are tripped by the circuit breaker"
                    if tripped else "")
                 + ("; the avoid list from the last failure removed the rest"
                    if avoid else "")),
                candidates=[])

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
            preferred = self._static_preference(node_kind, allowed)
            return Placement(
                preferred or allowed[0],
                "nothing measured for this node kind yet; using the catalog's "
                "static preference so an unfitted formula does not pretend to "
                "be a measurement",
                candidates=allowed, scores=scored, provisional=True)

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

        return Placement(best, reason, candidates=allowed, scores=scored,
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
