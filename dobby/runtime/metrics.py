"""What the recorded runs actually say — and where they say nothing.

Two consumers, one source. An operator asks "is this getting worse", and the
scheduler asks "which provider should do this node". Both questions are answered
from the same tables the runtime already writes, so neither depends on anybody
remembering to instrument something.

The rule that shapes every function here: **a metric with no data returns
`None` and says why.** Zero is a measurement. `None` is the absence of one, and
collapsing the two is how a dashboard comes to show 0% success for a system
nobody has run. Every summary therefore carries an `n` alongside its value, and
`unmeasured` lists what could not be computed at all.

Cost is the sharpest case, and the claim here has NARROWED. This module used to
say flatly that the kit cannot see money because CLI providers do not report
token usage. Measured 2026-08-22, that is false for one of them:
`claude -p --output-format json` returns `total_cost_usd`, input/output/thinking
tokens and both cache counters — the vendor's own figures, not a price table this
repository maintains. `providers/usage.py` parses them and `run_provider` will
collect them when asked.

What remains true is everything else: no other CLI has been probed, the runs in
this store predate the instrumentation, and a call made without
`collect_usage=True` reports nothing. So `cost_per_verified_task` still returns
`None` here, and now for a reason that can be fixed rather than one that cannot.
The scheduler still ranks by the catalog's declared `cost_tier` — an ordering,
explicitly not a price.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import graph as G
from .contracts import PROMOTED

#: Relative expense, for ordering only. Not money, and never rendered as money.
COST_RANK = {"local": 0, "cheap": 1, "standard": 2, "premium": 3}
DEFAULT_COST_RANK = 2


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile. `None` for an empty sample.

    Nearest-rank rather than interpolated because these samples are small — a
    handful of runs — and an interpolated p95 over four points invents a number
    between two real ones and presents it with the same confidence.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round(q / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[index], 3)


@dataclass
class Measurement:
    """A value, the sample it came from, and the reason when there is none."""

    value: float | None
    n: int = 0
    unit: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"value": self.value, "n": self.n, "unit": self.unit,
                "note": self.note}


def task_success_at_verifier(store, *, limit: int = 500) -> Measurement:
    """Share of finished runs whose every node passed its gate.

    Runs still in flight are excluded rather than counted as failures. A run
    that has not finished has not failed, and counting it as one makes the
    metric drop whenever somebody starts work.
    """
    runs = store.list_runs(limit=limit)
    finished = [r for r in runs if r["state"] in G.RUN_TERMINAL]
    if not finished:
        return Measurement(None, 0, "ratio",
                           "no run has reached a terminal state yet")
    ok = sum(1 for r in finished if r["state"] == G.SUCCEEDED)
    return Measurement(round(ok / len(finished), 4), len(finished), "ratio")


def completion_latency(store, *, limit: int = 500) -> dict:
    """p50/p95 from the run's first span to its last, per finished run."""
    samples: list[float] = []
    for run in store.list_runs(limit=limit):
        if run["state"] not in G.RUN_TERMINAL:
            continue
        spans = store.spans(run["run_id"])
        if not spans:
            continue
        start = min(s["started_ms"] for s in spans)
        end = max((s["ended_ms"] or s["started_ms"]) for s in spans)
        samples.append((end - start) / 1000.0)
    if not samples:
        return {"p50": Measurement(None, 0, "s",
                                   "no finished run has spans recorded"
                                   ).to_dict(),
                "p95": Measurement(None, 0, "s",
                                   "no finished run has spans recorded"
                                   ).to_dict()}
    return {"p50": Measurement(percentile(samples, 50), len(samples), "s"
                               ).to_dict(),
            "p95": Measurement(percentile(samples, 95), len(samples), "s"
                               ).to_dict()}


def cost_per_verified_task(store, *, limit: int = 500) -> Measurement:
    """Unmeasurable here, and it says so.

    Present as a named gap rather than absent, because a metrics table missing
    the cost row reads as "cost is fine". `agent_seconds_per_verified_task` is
    the honest neighbour and is reported beside it.
    """
    return Measurement(
        None, 0, "usd",
        "no run in this store carries provider usage. Token and cost reporting "
        "exists as of 2026-08-22 (providers/usage.py, run_provider("
        "collect_usage=True), verified against claude --output-format json) but "
        "nothing has yet run through it, and these runs predate it. This is a "
        "missing measurement, not an impossible one — which is a change from "
        "what this function used to say. agent_seconds_per_verified_task is the "
        "neighbour that is measurable today")


def agent_seconds_per_verified_task(store, *, limit: int = 500) -> Measurement:
    """Wall clock actually bought, per run that passed every gate."""
    seconds, verified = 0.0, 0
    for run in store.list_runs(limit=limit):
        spans = store.spans(run["run_id"], kind="agent.generation")
        seconds += sum((s["duration_ms"] or 0) / 1000.0 for s in spans)
        if run["state"] == G.SUCCEEDED:
            verified += 1
    if not verified:
        return Measurement(None, 0, "s",
                           "no run has passed every gate yet")
    return Measurement(round(seconds / verified, 2), verified, "s")


def retry_amplification(store, *, limit: int = 500) -> Measurement:
    """Total attempts divided by nodes that succeeded.

    Above ~1.3 something is being retried that should have been repaired or
    routed elsewhere. Below 1.0 is impossible and would mean the store lost
    attempts.
    """
    attempts, succeeded = 0, 0
    for run in store.list_runs(limit=limit):
        rows = store.attempts(run["run_id"])
        attempts += len(rows)
        succeeded += sum(1 for r in rows if r["outcome"] == G.FINISHED)
    if not succeeded:
        return Measurement(None, 0, "ratio", "no node has succeeded yet")
    return Measurement(round(attempts / succeeded, 3), attempts, "ratio")


def recovery_success_rate(store, *, limit: int = 500) -> Measurement:
    """Of runs that were interrupted, how many later reached a terminal state.

    An interrupted run is one with a recovered attempt — the signature
    `Runner._reconcile` leaves. Anything below 1.0 here is a P0 defect: the
    whole point of the store is that an interrupted run can be finished.
    """
    interrupted, recovered = 0, 0
    for run in store.list_runs(limit=limit):
        rows = store.attempts(run["run_id"])
        was_interrupted = any(
            r["outcome"] == G.RETRYABLE_FAILURE
            and G.INTERRUPTED_DETAIL in (r["detail"] or "") for r in rows)
        if not was_interrupted:
            continue
        interrupted += 1
        if run["state"] in G.RUN_TERMINAL:
            recovered += 1
    if not interrupted:
        return Measurement(None, 0, "ratio",
                           "no run has been interrupted yet — nothing to "
                           "recover, which is not the same as recovering")
    return Measurement(round(recovered / interrupted, 4), interrupted, "ratio")


def side_effect_duplicate_rate(store, *, limit: int = 500) -> Measurement:
    """Duplicate external effects per key. Anything but 0 is a stop-everything.

    The store's primary key makes a duplicate row impossible, so this measures
    the thing that CAN still happen: one key confirmed more than once, which
    would mean an effect was performed twice under one claim.
    """
    keys, duplicates = 0, 0
    for run in store.list_runs(limit=limit):
        confirmations: dict[str, int] = {}
        for span in store.spans(run["run_id"]):
            if span["name"] == "effect.confirmed":
                key = span["attributes"].get("key", "")
                confirmations[key] = confirmations.get(key, 0) + 1
        keys += len(confirmations)
        duplicates += sum(1 for c in confirmations.values() if c > 1)
    if not keys:
        return Measurement(None, 0, "ratio",
                           "no external effect has been performed yet")
    return Measurement(round(duplicates / keys, 4), keys, "ratio")


def report(store, *, limit: int = 500) -> dict:
    """Every operational metric, with its sample size and its gaps named."""
    measured = {
        "task_success_at_verifier": task_success_at_verifier(store, limit=limit),
        "cost_per_verified_task": cost_per_verified_task(store, limit=limit),
        "agent_seconds_per_verified_task":
            agent_seconds_per_verified_task(store, limit=limit),
        "retry_amplification": retry_amplification(store, limit=limit),
        "recovery_success_rate": recovery_success_rate(store, limit=limit),
        "side_effect_duplicate_rate":
            side_effect_duplicate_rate(store, limit=limit),
    }
    out = {name: m.to_dict() for name, m in measured.items()}
    out["completion_latency"] = completion_latency(store, limit=limit)
    out["unmeasured"] = sorted(name for name, m in measured.items()
                               if m.value is None)
    out["span_write_failures"] = list(store.span_write_failures)
    return out


# --------------------------------------------------------------------------
# The provider scorecard — the scheduler's only input
# --------------------------------------------------------------------------

@dataclass
class ProviderStats:
    """What has been observed about one provider on one kind of node."""

    provider: str
    node_kind: str
    attempts: int = 0
    successes: int = 0
    durations_s: list[float] = field(default_factory=list)
    failure_classes: dict = field(default_factory=dict)
    #: Most recent outcomes, newest last. Used for the circuit breaker, which
    #: cares about the run of failures at the END, not the average.
    recent: list[bool] = field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        if not self.attempts:
            return None
        return round(self.successes / self.attempts, 4)

    @property
    def p95_s(self) -> float | None:
        return percentile(self.durations_s, 95)

    @property
    def consecutive_failures(self) -> int:
        count = 0
        for ok in reversed(self.recent):
            if ok:
                break
            count += 1
        return count

    def to_dict(self) -> dict:
        return {"provider": self.provider, "node_kind": self.node_kind,
                "attempts": self.attempts, "success_rate": self.success_rate,
                "p95_s": self.p95_s,
                "consecutive_failures": self.consecutive_failures,
                "failure_classes": dict(self.failure_classes)}


def scorecard(store, *, limit: int = 500, window: int = 50) -> dict:
    """Per (provider, node_kind) outcomes, newest `window` attempts each.

    Read from `agent.generation` spans, which carry the provider, and joined to
    the attempt outcome the runtime recorded. A rolling window rather than all
    history: a provider that was broken last month and is fine now should be
    usable, and one that was fine last month and is broken now must not be
    protected by its record.
    """
    stats: dict[tuple, ProviderStats] = {}
    for run in store.list_runs(limit=limit):
        outcomes = {(r["node_id"], r["attempt"]): r
                    for r in store.attempts(run["run_id"])}
        for span in store.spans(run["run_id"], kind="agent.generation"):
            provider = span["attributes"].get("provider")
            if not provider:
                continue
            node_kind = span["attributes"].get("node_kind", "unknown")
            key = (provider, node_kind)
            entry = stats.setdefault(key, ProviderStats(provider, node_kind))
            attempt_row = outcomes.get((span["node_id"], span["attempt"]))
            # The SPAN says the call happened; the ATTEMPT says whether what it
            # produced survived the gate. Success here means verified, not
            # "the process exited zero" — otherwise the scheduler optimises for
            # providers that answer fast and wrongly.
            ok = bool(attempt_row and attempt_row["outcome"] == G.FINISHED)
            entry.attempts += 1
            entry.successes += int(ok)
            if span["duration_ms"]:
                entry.durations_s.append(span["duration_ms"] / 1000.0)
            entry.recent.append(ok)
            if attempt_row and attempt_row["failure_class"]:
                cls = attempt_row["failure_class"]
                entry.failure_classes[cls] = entry.failure_classes.get(cls, 0) + 1

    for entry in stats.values():
        entry.recent = entry.recent[-window:]
        entry.durations_s = entry.durations_s[-window:]
    return {f"{p}/{k}": s.to_dict() for (p, k), s in sorted(stats.items())}


def stats_for(store, provider: str, node_kind: str, *,
              limit: int = 500) -> ProviderStats:
    """One cell of the scorecard, or an empty one. Never `None`.

    An empty `ProviderStats` is the honest representation of "never tried here":
    `success_rate` is `None`, which the utility function treats as unknown
    rather than as zero.
    """
    card = scorecard(store, limit=limit)
    raw = card.get(f"{provider}/{node_kind}")
    if raw is None:
        return ProviderStats(provider, node_kind)
    entry = ProviderStats(provider, node_kind, attempts=raw["attempts"],
                          successes=int(round((raw["success_rate"] or 0)
                                              * raw["attempts"])),
                          failure_classes=raw["failure_classes"])
    entry.recent = [True] * (entry.attempts - raw["consecutive_failures"]) + \
                   [False] * raw["consecutive_failures"]
    if raw["p95_s"] is not None:
        entry.durations_s = [raw["p95_s"]]
    return entry
