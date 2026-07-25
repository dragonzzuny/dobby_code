"""Where the session's time actually went, and a one-line status to show it.

The gap this fills
------------------
A harness that dispatches work to several agents hides its own cost. The user
sees a long pause and has no way to tell whether one provider is slow, three ran
in parallel, or something has been retrying for four minutes. Existing status
displays report the *session* — model, context percentage, quota — which is the
environment, not the work.

This records the work: per provider, how many calls, how much wall clock, how
many failed, and what is running right now.

Wall clock, not tokens
----------------------
Time is recorded because time is what this harness can actually measure. Token
counts are only available from providers that return usage, and CLI providers
mostly do not — so a token total would be a partial sum presented as a whole,
which is worse than an honest duration. `tokens.estimate_savings` handles the
token side separately and labels its numbers as estimates.

Parallel time is reported twice, on purpose
-------------------------------------------
`wall_s` is what the user waited. `agent_s` is the summed time across agents,
which is what was *bought*. On a fan-out of six these differ by up to six times,
and reporting only one of them misleads in opposite directions: wall clock alone
hides the spend, and summed agent time alone implies the user waited for all of
it. The ratio is the parallelism actually achieved.

Append-only JSONL, swept on read
--------------------------------
Same storage discipline as the rest of the kit: diffable, crash-safe, no
infrastructure. A statusline is invoked on a keystroke cadence, so reads are
bounded by a tail window rather than by file size — a session that ran a thousand
agents must not make the prompt slow.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Sequence

#: Ledger location under the data dir.
SPEND_FILE = os.path.join("state", "spend.jsonl")

#: Lines read from the tail of the ledger for a status render. The statusline is
#: re-invoked constantly, so this is a latency budget, not a data limit.
TAIL_LINES = 400

#: Bar width for the compact render. Short enough to sit in a status line
#: alongside the other segments a host already shows.
BAR_WIDTH = 8


@dataclasses.dataclass
class Entry:
    """One recorded agent call."""

    t: float
    provider: str
    role: str
    duration_s: float
    ok: bool
    label: str = ""
    round_id: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _path(data_dir: str) -> str:
    p = os.path.join(data_dir, SPEND_FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def record(data_dir: str, *, provider: str, duration_s: float, ok: bool,
           role: str = "", label: str = "", round_id: str = "") -> Entry:
    """Append one call to the ledger."""
    entry = Entry(t=time.time(), provider=provider, role=role,
                  duration_s=round(float(duration_s), 3), ok=bool(ok),
                  label=label, round_id=round_id)
    with open(_path(data_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return entry


def record_round(data_dir: str, round_, *, role: str = "",
                 round_id: str = "") -> int:
    """Record every result of a `FanoutRound`. Returns the count written."""
    written = 0
    for result in round_.results:
        if result is None:
            continue
        record(data_dir, provider=result.provider,
               duration_s=result.duration_s, ok=result.ok, role=role,
               label=str(result.meta.get("label") or ""), round_id=round_id)
        written += 1
    return written


def _read_tail(data_dir: str, limit: int = TAIL_LINES) -> list[Entry]:
    path = os.path.join(data_dir, SPEND_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()[-limit:]
    out: list[Entry] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Entry(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            # A corrupt line must not break the status prompt.
            continue
    return out


def summarize(data_dir: str, *, window_s: float | None = None,
              limit: int = TAIL_LINES) -> dict:
    """Aggregate the ledger. `window_s` restricts to the recent past."""
    entries = _read_tail(data_dir, limit)
    if window_s is not None:
        cutoff = time.time() - window_s
        entries = [e for e in entries if e.t >= cutoff]
    if not entries:
        return {"calls": 0, "providers": {},
                "note": "no agent calls recorded"}

    per: dict[str, dict] = {}
    for e in entries:
        row = per.setdefault(e.provider, {"calls": 0, "agent_s": 0.0,
                                          "failed": 0, "slowest_s": 0.0})
        row["calls"] += 1
        row["agent_s"] += e.duration_s
        row["failed"] += 0 if e.ok else 1
        row["slowest_s"] = max(row["slowest_s"], e.duration_s)

    for row in per.values():
        row["agent_s"] = round(row["agent_s"], 1)
        row["slowest_s"] = round(row["slowest_s"], 1)
        row["mean_s"] = round(row["agent_s"] / row["calls"], 2)

    agent_s = sum(r["agent_s"] for r in per.values())

    # Wall time is the union of the calls' [start, end) intervals, NOT the span
    # of their timestamps.
    #
    # `t` is when the entry was WRITTEN, and `record_round` writes a whole round
    # at once after it finishes. Using the timestamp span therefore reports a
    # span of milliseconds for a round that took minutes, and the parallelism
    # ratio explodes — observed at 11050x on an 8-call fixture before this was
    # fixed. Reconstructing each call's start from `t - duration` and merging
    # overlapping intervals gives the time actually spent waiting, and correctly
    # counts a gap between rounds as not-waiting.
    intervals = sorted((e.t - e.duration_s, e.t) for e in entries)
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    wall_s = sum(end - start for start, end in merged)

    # Physical floor: the wall clock can never be shorter than the longest
    # single call, whatever the bookkeeping says.
    slowest_call = max(e.duration_s for e in entries)
    wall_s = max(wall_s, slowest_call)

    # Physical ceiling: parallelism cannot exceed the number of calls, and
    # cannot exceed the concurrency the machine actually ran.
    parallelism = (agent_s / wall_s) if wall_s > 0 else 1.0
    parallelism = min(parallelism, float(len(entries)))

    return {
        "calls": len(entries),
        "failed": sum(1 for e in entries if not e.ok),
        "agent_s": round(agent_s, 1),
        "wall_s": round(wall_s, 1),
        # Both numbers are reported because each misleads alone: wall clock hides
        # the spend, summed agent time implies the user waited for all of it.
        "parallelism": round(parallelism, 2),
        "providers": dict(sorted(per.items(),
                                 key=lambda kv: -kv[1]["agent_s"])),
        "busiest": max(per, key=lambda k: per[k]["agent_s"]),
        "window_s": window_s,
    }


def _short(seconds: float) -> str:
    if seconds < 60:
        return f"{int(round(seconds))}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds / 3600:.1f}h"


def statusline(data_dir: str, *, active: Sequence[dict] = (),
               window_s: float | None = 3600.0) -> str:
    """One line describing what the agents are doing and what it has cost.

    Designed for a host status bar, so it is plain text with no escape codes and
    no trailing newline. Segments are dropped rather than shown empty — a status
    line reading `agents:0 spend:0s eta:-` is noise occupying space that the
    host's other segments could use.

    `active` entries are `{"label": str, "started": float, "eta_s": float|None}`.
    """
    parts: list[str] = []

    if active:
        now = time.monotonic()
        running = []
        for a in active:
            elapsed = now - float(a.get("started", now))
            running.append(f"{a.get('label', '?')} {_short(elapsed)}")
        parts.append(f"running {len(active)}: " + ", ".join(running[:3])
                     + ("…" if len(running) > 3 else ""))
        etas = [a["eta_s"] for a in active
                if a.get("eta_s") is not None]
        if etas:
            # The round finishes when its SLOWEST member does, so the round ETA
            # is the maximum, not the sum and not the mean.
            parts.append(f"eta ~{_short(max(etas))}")

    summary = summarize(data_dir, window_s=window_s)
    if summary["calls"]:
        seg = (f"agents {summary['calls']} · {_short(summary['agent_s'])} spent "
               f"· {summary['parallelism']}x parallel")
        if summary["failed"]:
            seg += f" · {summary['failed']} failed"
        parts.append(seg)
        top = summary["busiest"]
        row = summary["providers"][top]
        parts.append(f"top {top} {_short(row['agent_s'])}")

    return " | ".join(parts) if parts else "dobby: idle"


def render_detail(data_dir: str, *, window_s: float | None = None) -> str:
    """Multi-line breakdown for a report or a `dobby spend` invocation."""
    summary = summarize(data_dir, window_s=window_s)
    if not summary["calls"]:
        return "no agent calls recorded"

    lines = [
        f"calls {summary['calls']}  failed {summary['failed']}",
        f"agent time {_short(summary['agent_s'])}  "
        f"wall time {_short(summary['wall_s'])}  "
        f"parallelism {summary['parallelism']}x",
        "",
        f"{'provider':<12}{'calls':>6}{'time':>9}{'mean':>8}{'slowest':>9}"
        f"{'failed':>8}  share",
    ]
    total = summary["agent_s"] or 1.0
    for name, row in summary["providers"].items():
        share = row["agent_s"] / total
        filled = int(round(BAR_WIDTH * share))
        bar = "#" * filled + "-" * (BAR_WIDTH - filled)
        lines.append(
            f"{name:<12}{row['calls']:>6}{_short(row['agent_s']):>9}"
            f"{row['mean_s']:>7.1f}s{_short(row['slowest_s']):>9}"
            f"{row['failed']:>8}  [{bar}] {share:.0%}")
    lines.append("")
    lines.append("agent time is what was BOUGHT; wall time is what was WAITED. "
                 "Their ratio is the parallelism actually achieved.")
    return "\n".join(lines)
