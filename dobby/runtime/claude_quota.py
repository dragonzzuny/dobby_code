"""Claude admission control: reserve before the call, settle from the envelope.

WHY THIS IS NOT `RunBudget`

`RunBudget` bounds a run: attempts, deadline, USD, irreversible effects. This
bounds a PROVIDER across runs, in the units the operator actually feels — an Agy
subscription already paid for and a Codex allowance are not USD, and "use Claude
sparingly" is a statement about somebody's budget rather than a belief about
which model is better. Mixing the two would make one of them lie.

It is enforceable at all only because `claude --output-format json` reports
`usage.output_tokens_details.thinking_tokens` and both cache counters, probed
2026-08-22. No other provider here reports anything, which is why this module is
Claude-specific and says so in its name rather than pretending to be general.

WHAT IT GUARANTEES AND WHAT IT DELIBERATELY DOES NOT

It controls ADMISSION. A node that cannot reserve is not started, and that costs
nothing. It does NOT kill a Claude process that is running over its estimate:
killing mid-call is how an effect happens and goes unrecorded, which is a worse
failure than an overspend. So a node may exceed its reservation, the excess is
recorded as an `overrun`, and the lane closes AFTERWARDS.

REFUSED AND OVERRUN ARE DIFFERENT AND ARE NEVER MERGED

`refused` means nothing ran and nothing was spent. `overrun` means it ran and
cost more than estimated. One is a policy working; the other is a policy learning
its estimate was wrong. A single "quota problem" counter would hide which.

RESERVED EXISTS BECAUSE TWO SCHEDULERS CAN READ THE SAME REMAINING

Without a reservation, two nodes admitted concurrently both see the whole
remaining allowance and both proceed. The reservation is taken under a lock
immediately before the process starts, and released on cancel or settle.

`Usage is None` IS NOT ZERO USAGE

A call whose envelope did not parse spent real tokens that nobody counted.
Recording it as zero would make the cap decorative exactly when it stopped
working. It is recorded as `unmeasured`, and `fail_closed_on_unmeasured_usage`
decides whether the lane closes — which defaults to True, because an operator
whose stated goal is spending less on Claude is not helped by a counter that
silently stops counting.
"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field

#: Ledger states, reported rather than inferred from counters.
OPEN = "OPEN"
EXHAUSTED = "EXHAUSTED"

#: Why a reservation was refused. Each is a different operator action.
CALLS = "calls"
THINKING = "thinking_tokens"
BILLABLE = "billable_tokens"
OVERRUN = "overrun"
UNMEASURED = "unmeasured_claude_usage"

#: Samples of a (role, class) pair below which p95 is not a number to reserve
#: against. Eight, matching `bench.MIN_TASKS` and `policy.MIN_ECONOMIC_SAMPLES`.
MIN_P95_SAMPLES = 8


class ClaudeQuotaExceeded(RuntimeError):
    """A reservation was refused. Nothing ran, so nothing was spent."""

    def __init__(self, dimension: str, detail: str):
        super().__init__(f"claude quota {dimension}: {detail}")
        self.dimension = dimension
        self.detail = detail


@dataclass(frozen=True)
class ClaudeQuotaConfig:
    """Ceilings and how to estimate against them.

    The defaults are a first guess and are labelled as one. They are not derived
    from anything: two calls and 12k thinking tokens is a blast radius somebody
    chose, and the p95 path replaces the static minimums once a role has real
    samples.
    """

    enabled: bool = True
    max_calls: int | None = 2
    max_thinking_tokens: int | None = 12_000
    #: input + cache CREATION + output. Cache READS are excluded deliberately:
    #: they are the cheap path, and counting them like raw input would penalise
    #: caching for working. A measured "reply OK" call showed 2 input tokens
    #: against 29,613 of cache creation, which is why creation is in.
    max_billable_tokens: int | None = 80_000
    min_thinking_tokens: int = 800
    min_billable_tokens: int = 4_000
    p95_multiplier: float = 1.25
    fallback_multiplier: float = 1.50
    #: A call whose usage did not parse closes the lane. Default True: a cap
    #: that keeps admitting calls it cannot count is decoration.
    fail_closed_on_unmeasured_usage: bool = True

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "max_calls": self.max_calls,
                "max_thinking_tokens": self.max_thinking_tokens,
                "max_billable_tokens": self.max_billable_tokens,
                "fail_closed_on_unmeasured_usage":
                    self.fail_closed_on_unmeasured_usage}


@dataclass(frozen=True)
class TokenEstimate:
    """What one node is expected to spend, and where the number came from."""

    calls: int = 1
    thinking_tokens: int = 0
    billable_tokens: int = 0
    #: "role_p95" | "static_default" | "fallback_static". Carried so a refusal
    #: can say whether it was measured against evidence or against a guess.
    basis: str = "static_default"

    def to_dict(self) -> dict:
        return {"calls": self.calls, "thinking_tokens": self.thinking_tokens,
                "billable_tokens": self.billable_tokens, "basis": self.basis}


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    node_id: str
    role: str
    estimate: TokenEstimate

    def to_dict(self) -> dict:
        return {"reservation_id": self.reservation_id, "node_id": self.node_id,
                "role": self.role, "estimate": self.estimate.to_dict()}


@dataclass(frozen=True)
class Actual:
    """What a settled call really cost, with `measured` kept separate."""

    calls: int = 1
    thinking_tokens: int = 0
    billable_tokens: int = 0
    measured: bool = False

    def to_dict(self) -> dict:
        return {"calls": self.calls, "thinking_tokens": self.thinking_tokens,
                "billable_tokens": self.billable_tokens,
                "measured": self.measured}


def estimate_for(role: str, *, config: ClaudeQuotaConfig,
                 p95_thinking: float | None = None,
                 p95_billable: float | None = None,
                 samples: int = 0) -> TokenEstimate:
    """A reservation size. Never a model call.

    Asking a model how much a model will spend is a call to find out about a
    call, and it would be the first thing the cap should refuse.
    """
    enough = samples >= MIN_P95_SAMPLES and p95_thinking and p95_billable
    multiplier = (config.p95_multiplier if enough
                  else config.fallback_multiplier)

    if enough:
        thinking = max(config.min_thinking_tokens,
                       math.ceil(multiplier * float(p95_thinking)))
        billable = max(config.min_billable_tokens,
                       math.ceil(multiplier * float(p95_billable)))
        basis = "role_p95"
    else:
        thinking = math.ceil(multiplier * config.min_thinking_tokens)
        billable = math.ceil(multiplier * config.min_billable_tokens)
        basis = "fallback_static" if samples else "static_default"

    return TokenEstimate(calls=1, thinking_tokens=thinking,
                         billable_tokens=billable, basis=basis)


def actual_from_usage(usage) -> Actual:
    """A settled figure from the provider's envelope, or an honest unknown.

    `usage is None` is NOT zero. A call whose envelope did not parse spent real
    tokens nobody counted, and writing zero would make the ledger most wrong at
    exactly the moment it stopped working. The call itself is still counted —
    that much is observable without any envelope.
    """
    if usage is None:
        return Actual(calls=1, thinking_tokens=0, billable_tokens=0,
                      measured=False)

    def get(name):
        value = (usage.get(name) if isinstance(usage, dict)
                 else getattr(usage, name, None))
        return int(value) if isinstance(value, (int, float)) else 0

    billable = get("input_tokens") + get("cache_creation_tokens") + get(
        "output_tokens")
    return Actual(calls=1, thinking_tokens=get("thinking_tokens"),
                  billable_tokens=billable, measured=True)


@dataclass
class ClaudeQuotaLedger:
    """Reserve, settle, and refuse. Thread-safe, because two schedulers race."""

    config: ClaudeQuotaConfig = field(default_factory=ClaudeQuotaConfig)
    calls_settled: int = 0
    thinking_settled: int = 0
    billable_settled: int = 0
    calls_reserved: int = 0
    thinking_reserved: int = 0
    billable_reserved: int = 0
    reservations: dict = field(default_factory=dict)
    overrun: bool = False
    unmeasured_calls: int = 0
    lane_closed_reason: str = ""
    events: list = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        return EXHAUSTED if self.lane_closed_reason else OPEN

    def remaining(self) -> dict:
        """What is left, counting reservations as spent until they settle."""
        with self._lock:
            def left(cap, settled, reserved):
                return None if cap is None else max(0, cap - settled - reserved)
            return {
                "calls": left(self.config.max_calls, self.calls_settled,
                              self.calls_reserved),
                "thinking_tokens": left(self.config.max_thinking_tokens,
                                        self.thinking_settled,
                                        self.thinking_reserved),
                "billable_tokens": left(self.config.max_billable_tokens,
                                        self.billable_settled,
                                        self.billable_reserved),
                "state": self.state,
                "closed_because": self.lane_closed_reason,
            }

    def admits(self, estimate: TokenEstimate | None = None) -> tuple[bool, str]:
        """Whether a call could be reserved right now. Used by placement.

        A refusal here is NOT a provider failure: it consumes no retry, trips no
        breaker, and the right response is a different provider rather than the
        same one again.
        """
        if not self.config.enabled:
            return True, ""
        with self._lock:
            if self.lane_closed_reason:
                return False, self.lane_closed_reason
            estimate = estimate or TokenEstimate(calls=1)
            for dimension, cap, settled, reserved, want in (
                (CALLS, self.config.max_calls, self.calls_settled,
                 self.calls_reserved, estimate.calls),
                (THINKING, self.config.max_thinking_tokens,
                 self.thinking_settled, self.thinking_reserved,
                 estimate.thinking_tokens),
                (BILLABLE, self.config.max_billable_tokens,
                 self.billable_settled, self.billable_reserved,
                 estimate.billable_tokens),
            ):
                if cap is not None and settled + reserved + want > cap:
                    return False, (
                        f"claude {dimension}: {settled} settled + {reserved} "
                        f"reserved + {want} requested exceeds the cap of {cap}")
            return True, ""

    # -- admission ---------------------------------------------------------
    def reserve(self, *, node_id: str, role: str,
                estimate: TokenEstimate) -> Reservation:
        """Take the allowance BEFORE the process starts, or refuse.

        Called immediately before launching, and the caller must `cancel` if the
        launch does not happen. Reserving earlier would hold an allowance across
        a lease wait; reserving later would let two nodes both see it free.
        """
        with self._lock:
            ok, why = self.admits(estimate)
            if not ok:
                dimension = (OVERRUN if self.lane_closed_reason == OVERRUN
                             else (UNMEASURED
                                   if self.lane_closed_reason == UNMEASURED
                                   else why.split(":")[0].replace("claude ", "")
                                   ))
                self._record("CLAUDE_QUOTA_REFUSED",
                             {"node_id": node_id, "role": role,
                              "dimension": dimension, "detail": why,
                              "estimate": estimate.to_dict()})
                raise ClaudeQuotaExceeded(dimension, why)

            reservation = Reservation(uuid.uuid4().hex[:16], node_id, role,
                                      estimate)
            self.reservations[reservation.reservation_id] = reservation
            self.calls_reserved += estimate.calls
            self.thinking_reserved += estimate.thinking_tokens
            self.billable_reserved += estimate.billable_tokens
            self._record("CLAUDE_QUOTA_RESERVED", reservation.to_dict())
            return reservation

    def cancel(self, reservation: Reservation) -> None:
        """Release an allowance whose process never started.

        Only correct when the caller KNOWS no process ran. After a crash between
        reserving and launching, that is not known, and `runtime/store.py`'s
        unconfirmed-effect reconciliation is the model to follow: leave it held
        and make a person look.
        """
        with self._lock:
            if self._release(reservation):
                self._record("CLAUDE_QUOTA_CANCELLED",
                             {"reservation_id": reservation.reservation_id,
                              "node_id": reservation.node_id})

    def settle(self, reservation: Reservation, usage=None) -> dict:
        """Convert a reservation into what was actually spent."""
        with self._lock:
            self._release(reservation)
            actual = actual_from_usage(usage)

            self.calls_settled += actual.calls
            self.thinking_settled += actual.thinking_tokens
            self.billable_settled += actual.billable_tokens
            if not actual.measured:
                self.unmeasured_calls += 1

            over = self._exceeds_any_limit()
            if over and not self.lane_closed_reason:
                self.lane_closed_reason = OVERRUN
                self.overrun = True
            if (not actual.measured
                    and self.config.fail_closed_on_unmeasured_usage
                    and not self.lane_closed_reason):
                self.lane_closed_reason = UNMEASURED

            self._record("CLAUDE_QUOTA_SETTLED",
                         {"reservation_id": reservation.reservation_id,
                          "node_id": reservation.node_id,
                          "actual": actual.to_dict(),
                          "overrun": self.overrun,
                          "closed_because": self.lane_closed_reason})
            return {"actual": actual.to_dict(), "overrun": self.overrun,
                    "state": self.state,
                    "closed_because": self.lane_closed_reason,
                    "remaining": self.remaining()}

    # -- internals ---------------------------------------------------------
    def _release(self, reservation: Reservation) -> bool:
        held = self.reservations.pop(reservation.reservation_id, None)
        if held is None:
            return False
        self.calls_reserved = max(0, self.calls_reserved
                                  - held.estimate.calls)
        self.thinking_reserved = max(0, self.thinking_reserved
                                     - held.estimate.thinking_tokens)
        self.billable_reserved = max(0, self.billable_reserved
                                     - held.estimate.billable_tokens)
        return True

    def _exceeds_any_limit(self) -> bool:
        config = self.config
        return any((
            config.max_calls is not None
            and self.calls_settled > config.max_calls,
            config.max_thinking_tokens is not None
            and self.thinking_settled > config.max_thinking_tokens,
            config.max_billable_tokens is not None
            and self.billable_settled > config.max_billable_tokens))

    def _record(self, kind: str, payload: dict) -> None:
        self.events.append({"type": kind, **payload})

    def to_dict(self) -> dict:
        return {"config": self.config.to_dict(), "state": self.state,
                "settled": {"calls": self.calls_settled,
                            "thinking_tokens": self.thinking_settled,
                            "billable_tokens": self.billable_settled},
                "reserved": {"calls": self.calls_reserved,
                             "thinking_tokens": self.thinking_reserved,
                             "billable_tokens": self.billable_reserved},
                "open_reservations": len(self.reservations),
                "unmeasured_calls": self.unmeasured_calls,
                "overrun": self.overrun,
                "closed_because": self.lane_closed_reason,
                "remaining": self.remaining()}
