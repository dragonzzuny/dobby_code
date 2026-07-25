"""Progress and completion estimates that refuse to guess.

Why most progress bars lie
--------------------------
The usual implementation divides elapsed time by fraction complete and
extrapolates. That is correct only when every remaining unit costs what the
average finished unit cost — which is false for essentially all agent work.
Provider calls vary by an order of magnitude between a cached short answer and a
long reasoning turn. A tree search's later nodes are slower than its drafts. A
fan-out finishes when its *slowest* member does, not when the average does.

So the bar reads 90% for a long time and then jumps, and the user learns to
ignore it. A number that is ignored is worse than no number, because producing it
cost something.

What this module does instead
-----------------------------
- **Refuses to estimate below `MIN_SAMPLES` completions.** One sample is a
  measurement of one thing, not a rate. `eta()` returns `None` with a reason
  rather than a confident wrong number.
- **Reports a RANGE, from observed spread.** The interval comes from the actual
  per-unit durations seen so far, so it widens when the work is erratic and
  narrows when it is uniform. A point estimate implies a precision that
  per-unit variance does not support.
- **Handles unknown totals honestly.** When `total` is None the answer is a rate
  and an elapsed time, never a completion time.
- **Models waves, not items, for parallel work.** A fan-out of six across two
  waves finishes in two round-trips regardless of the six durations, so
  `eta_waves` extrapolates on the critical path rather than on item count.

Rendering
---------
`bar()` writes a plain-text bar for a terminal. It is deliberately not animated
and carries no escape codes: this output lands in logs, ledgers, and CI
transcripts as often as in a TTY, and a carriage-return-based animation turns
those into unreadable single lines.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Sequence

#: Completions required before any estimate is offered. Two gives a rate; three
#: gives the first usable idea of spread. Set at 3 because the interval is the
#: point, and an interval from two samples is a straight line through two dots.
MIN_SAMPLES = 3

#: Bar width in characters. Fits an 80-column terminal alongside the counts.
BAR_WIDTH = 24


@dataclasses.dataclass
class Tracker:
    """Records completions and answers questions about them.

    Durations are kept individually rather than as a running mean, because the
    spread is what makes an estimate honest and a mean discards it.
    """

    label: str = "work"
    total: int | None = None
    started: float = dataclasses.field(default_factory=time.monotonic)
    durations: list[float] = dataclasses.field(default_factory=list)
    failures: int = 0
    _last_mark: float | None = None

    def start_unit(self) -> None:
        self._last_mark = time.monotonic()

    def complete_unit(self, duration_s: float | None = None, *,
                      failed: bool = False) -> None:
        """Record one finished unit.

        A failed unit still records its DURATION, because it consumed wall clock
        and the remaining work must be estimated against real elapsed time. It is
        counted separately so the caller can see that progress and success are
        not the same number.
        """
        if duration_s is None:
            mark = self._last_mark
            duration_s = (time.monotonic() - mark) if mark else 0.0
        self.durations.append(max(0.0, float(duration_s)))
        if failed:
            self.failures += 1
        self._last_mark = None

    # -- state ----------------------------------------------------------
    @property
    def done(self) -> int:
        return len(self.durations)

    @property
    def succeeded(self) -> int:
        return self.done - self.failures

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> int | None:
        if self.total is None:
            return None
        return max(0, self.total - self.done)

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.done / self.total)

    # -- statistics -----------------------------------------------------
    def rate_per_s(self) -> float | None:
        """Completions per second, from elapsed wall clock.

        Uses total elapsed rather than the sum of durations, so it accounts for
        gaps between units — queueing, rate-limit backoff, the caller thinking.
        Those gaps are real time the user waits through.

        When elapsed falls below the clock's resolution — durations reported by a
        caller that measured them elsewhere, or units that completed inside one
        tick — wall clock cannot produce a rate, and returning `None` would
        withhold a number that IS available. The sum of unit durations is used
        instead; `rate_basis()` reports which of the two was used, because a rate
        from summed durations assumes the units ran serially and will overstate
        throughput for parallel work.
        """
        if self.done == 0:
            return None
        basis = self.elapsed_s
        if basis <= 1e-6:
            basis = sum(self.durations)
        if basis <= 0:
            return None
        return self.done / basis

    def rate_basis(self) -> str:
        """Which denominator `rate_per_s` used, so the caveat can be stated."""
        if self.elapsed_s > 1e-6:
            return "wall_clock"
        return "summed_durations (assumes serial execution)"

    def per_unit_stats(self) -> dict:
        n = len(self.durations)
        if n == 0:
            return {"n": 0}
        mean = sum(self.durations) / n
        if n == 1:
            return {"n": 1, "mean": round(mean, 2), "stdev": None,
                    "min": round(self.durations[0], 2),
                    "max": round(self.durations[0], 2)}
        var = sum((d - mean) ** 2 for d in self.durations) / (n - 1)
        return {"n": n, "mean": round(mean, 2),
                "stdev": round(math.sqrt(var), 2),
                "min": round(min(self.durations), 2),
                "max": round(max(self.durations), 2)}

    def eta(self) -> dict:
        """Completion estimate, or an explicit refusal with the reason.

        The returned range is `remaining × (mean ∓ stdev)`, floored at the
        fastest observed unit and ceilinged at the slowest. Clamping to observed
        extremes keeps the interval inside what has actually been seen — a lower
        bound faster than anything yet measured is not a bound, it is a wish.
        """
        now_iso = time.strftime("%H:%M:%S")
        if self.total is None:
            rate = self.rate_per_s()
            return {
                "estimable": False,
                "reason": ("total work is unknown, so there is no completion "
                           "time to estimate — only a rate"),
                "done": self.done,
                "elapsed_s": round(self.elapsed_s, 1),
                "rate_per_min": round(rate * 60, 2) if rate else None,
                "rate_basis": self.rate_basis(),
                "as_of": now_iso,
            }
        if self.done >= self.total:
            return {"estimable": True, "remaining_s": 0.0, "done": self.done,
                    "total": self.total, "as_of": now_iso,
                    "note": "complete"}
        if self.done < MIN_SAMPLES:
            return {
                "estimable": False,
                "reason": (f"{self.done} of {MIN_SAMPLES} samples needed. One or "
                           "two completions measure those units, not a rate — "
                           "extrapolating from them is the guess that makes "
                           "progress bars untrustworthy"),
                "done": self.done, "total": self.total,
                "elapsed_s": round(self.elapsed_s, 1),
                "as_of": now_iso,
            }

        stats = self.per_unit_stats()
        remaining = self.remaining or 0
        mean, stdev = stats["mean"], stats["stdev"] or 0.0
        low_unit = max(stats["min"], mean - stdev)
        high_unit = min(stats["max"], mean + stdev) if stats["max"] > mean else mean
        # `high_unit` must never fall below the mean: an upper bound tighter than
        # the central estimate would make the interval nonsensical.
        high_unit = max(high_unit, mean)

        low_s = remaining * low_unit
        mid_s = remaining * mean
        high_s = remaining * high_unit

        return {
            "estimable": True,
            "done": self.done, "total": self.total, "remaining": remaining,
            "fraction": round(self.fraction or 0.0, 4),
            "elapsed_s": round(self.elapsed_s, 1),
            "remaining_s": round(mid_s, 1),
            "remaining_range_s": [round(low_s, 1), round(high_s, 1)],
            "eta_human": _human(mid_s),
            "eta_range_human": [_human(low_s), _human(high_s)],
            "per_unit": stats,
            "as_of": now_iso,
            "caveat": ("extrapolated from "
                       f"{stats['n']} completed unit(s); the range reflects "
                       "observed spread, not a confidence interval. Remaining "
                       "work that differs in kind from what finished will "
                       "invalidate it"),
        }

    def snapshot(self) -> dict:
        return {
            "label": self.label,
            "done": self.done, "succeeded": self.succeeded,
            "failed": self.failures, "total": self.total,
            "elapsed_s": round(self.elapsed_s, 1),
            "eta": self.eta(),
            "bar": self.bar(),
        }

    def bar(self, width: int = BAR_WIDTH) -> str:
        """Plain-text progress line. No escape codes, no animation.

        Deliberately static: this output lands in logs, ledgers, and CI
        transcripts as often as in a terminal, and a carriage-return animation
        turns those into one unreadable line.
        """
        if self.total:
            filled = int(round(width * (self.fraction or 0.0)))
            track = "#" * filled + "-" * (width - filled)
            head = f"[{track}] {self.done}/{self.total}"
        else:
            head = f"[{'?' * width}] {self.done}/?"
        eta = self.eta()
        if eta.get("estimable") and eta.get("remaining_s"):
            head += f" ~{eta['eta_human']} left"
        elif not eta.get("estimable"):
            head += "  ETA: not yet estimable"
        if self.failures:
            head += f"  ({self.failures} failed)"
        return f"{self.label}: {head}"


def _human(seconds: float) -> str:
    """Duration at a precision the estimate actually supports.

    Rounded coarsely on purpose: reporting "4m 37s" from a three-sample
    extrapolation implies a precision the samples do not carry, and a user who
    sees that precision will plan against it.
    """
    if seconds < 10:
        return "<10s"
    if seconds < 90:
        return f"~{int(round(seconds / 10) * 10)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{int(round(minutes))}m"
    hours = minutes / 60
    return f"~{hours:.1f}h"


def eta_waves(*, waves_total: int, waves_done: int,
              wave_durations: Sequence[float]) -> dict:
    """Estimate for parallel work, extrapolated on WAVES not items.

    A fan-out finishes when its slowest member does, so item count is the wrong
    unit: six agents across two waves cost two round-trips whether the sixth
    agent is fast or slow. Estimating on items would predict six units of work
    and be wrong by a factor of three.
    """
    if waves_total <= 0:
        return {"estimable": False, "reason": "no waves to run"}
    if waves_done >= waves_total:
        return {"estimable": True, "remaining_s": 0.0, "note": "complete"}
    if len(wave_durations) < 1:
        return {"estimable": False,
                "reason": "no completed wave yet; a wave's cost is its slowest "
                          "member and cannot be inferred before one finishes"}

    mean = sum(wave_durations) / len(wave_durations)
    remaining = waves_total - waves_done
    slowest = max(wave_durations)
    return {
        "estimable": True,
        "waves_done": waves_done, "waves_total": waves_total,
        "remaining_waves": remaining,
        "remaining_s": round(remaining * mean, 1),
        "remaining_range_s": [round(remaining * min(wave_durations), 1),
                              round(remaining * slowest, 1)],
        "eta_human": _human(remaining * mean),
        "mean_wave_s": round(mean, 2),
        "caveat": (f"extrapolated from {len(wave_durations)} wave(s); a wave "
                   "costs what its SLOWEST member costs, so one slow provider "
                   "sets the pace regardless of how many agents run"),
    }


def render_report(trackers: Sequence[Tracker]) -> str:
    """Multi-line status for several concurrent trackers."""
    if not trackers:
        return "(nothing in progress)"
    return "\n".join(t.bar() for t in trackers)
