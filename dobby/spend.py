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

from .core.jsonl import append_jsonl

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
    #: Everything below is optional and defaulted, because `_read_tail` builds
    #: an Entry with `Entry(**json.loads(line))` and a ledger written before
    #: these existed must keep parsing. A missing token count is 0 here and
    #: `measured` is what says whether that 0 is a measurement or an absence.
    #: WHICH MODEL. A token count with no model attached cannot be read: codex
    #: and agy do not name theirs in their JSON, so it is recorded from what the
    #: caller pinned. Discovered 2026-08-24 the hard way — an agy row of
    #: 1,601,155 tokens that could have been Gemini Flash or Claude Opus.
    model: str = ""
    #: The skill or command that caused this call, so a status bar can say what
    #: the spend was FOR rather than only how much it was.
    skill: str = ""
    measured: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: None, never 0, when the provider bills by subscription and reports no
    #: figure. Zero would sum into a total that reads as "this was free".
    cost_usd: float | None = None

    @property
    def tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.thinking_tokens
                + self.cache_read_tokens + self.cache_creation_tokens)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _path(data_dir: str) -> str:
    p = os.path.join(data_dir, SPEND_FILE)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def record(data_dir: str, *, provider: str, duration_s: float, ok: bool,
           role: str = "", label: str = "", round_id: str = "",
           model: str = "", skill: str = "", usage: dict | None = None) -> Entry:
    """Append one call to the ledger.

    `usage` is the provider's own envelope as `providers/usage.py` normalises
    it. Passing None records the call WITHOUT inventing zeros for it: a call
    whose provider reported nothing and a call that consumed nothing are
    different facts, and `measured` is the field that keeps them apart.
    """
    usage = usage or {}
    entry = Entry(
        t=time.time(), provider=provider, role=role,
        duration_s=round(float(duration_s), 3), ok=bool(ok),
        label=label, round_id=round_id,
        model=model or str(usage.get("model") or ""),
        skill=skill,
        measured=bool(usage),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        thinking_tokens=int(usage.get("thinking_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
        cost_usd=(None if usage.get("cost_usd") is None
                  else float(usage["cost_usd"])))
    append_jsonl(_path(data_dir), entry.to_dict())
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
        row = per.setdefault(e.provider, {
            "calls": 0, "agent_s": 0.0, "failed": 0, "slowest_s": 0.0,
            "calls_measured": 0, "tokens": 0, "input_tokens": 0,
            "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cost_usd": None, "models": []})
        row["calls"] += 1
        row["agent_s"] += e.duration_s
        row["failed"] += 0 if e.ok else 1
        row["slowest_s"] = max(row["slowest_s"], e.duration_s)
        row["calls_measured"] += 1 if e.measured else 0
        for field in ("input_tokens", "output_tokens", "thinking_tokens",
                      "cache_read_tokens", "cache_creation_tokens"):
            row[field] += getattr(e, field)
        row["tokens"] += e.tokens
        if e.cost_usd is not None:
            row["cost_usd"] = round((row["cost_usd"] or 0.0) + e.cost_usd, 4)
        if e.model and e.model not in row["models"]:
            row["models"].append(e.model)

    for row in per.values():
        row["agent_s"] = round(row["agent_s"], 1)
        row["slowest_s"] = round(row["slowest_s"], 1)
        row["mean_s"] = round(row["agent_s"] / row["calls"], 2)
        # A total over calls that did not all report is a FLOOR. Saying so here
        # means a status bar cannot accidentally present it as complete.
        row["complete"] = row["calls_measured"] == row["calls"]

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

    measured = sum(1 for e in entries if e.measured)
    costed = [e.cost_usd for e in entries if e.cost_usd is not None]
    return {
        "calls": len(entries),
        "failed": sum(1 for e in entries if not e.ok),
        "agent_s": round(agent_s, 1),
        "wall_s": round(wall_s, 1),
        "tokens": sum(e.tokens for e in entries),
        "calls_measured": measured,
        "tokens_complete": measured == len(entries),
        # Summed only over the providers that reported one. A subscription CLI
        # contributes tokens and no dollars, so this total is what the metered
        # providers cost and never what the run cost.
        "cost_usd_reported": round(sum(costed), 4) if costed else None,
        # False as soon as one call reported no dollars: the total is then the
        # metered providers' spend and not the run's.
        "dollars_complete": len(costed) == len(entries),
        # Oldest call to now, so a status bar can say how long this has been
        # going. `t - duration` is when the first call STARTED; the timestamp
        # alone is when it finished.
        "session_s": round(time.time() - min(e.t - e.duration_s
                                             for e in entries), 1),
        "skills": sorted({e.skill for e in entries if e.skill}),
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
        if summary.get("skills"):
            shown = summary["skills"][:2]
            parts.append("skill " + ",".join(shown)
                         + ("+" if len(summary["skills"]) > len(shown) else ""))

        seg = (f"agents {summary['calls']} · {_short(summary['agent_s'])} spent "
               f"· {summary['parallelism']}x parallel")
        if summary["failed"]:
            seg += f" · {summary['failed']} failed"
        parts.append(seg)

        # KEPT: the busiest provider by TIME. The per-provider cells below are
        # ordered by tokens, and the two orderings disagree exactly when it
        # matters — a slow cheap model against a fast expensive one — so
        # dropping this in favour of those would lose the answer to "what was
        # everyone waiting on".
        top = summary["busiest"]
        parts.append(f"top {top} {_short(summary['providers'][top]['agent_s'])}")

        # Per provider, because "which of the three did the work" is the whole
        # question a decomposing harness is asked. Ordered by tokens rather than
        # by seconds, for the reason just given.
        rows = sorted(summary["providers"].items(),
                      key=lambda kv: -kv[1]["tokens"])
        for provider, row in rows[:3]:
            if not row["tokens"]:
                parts.append(f"{provider} {row['calls']}c")
                continue
            cell = f"{provider} {row['calls']}c {_tokens(row['tokens'])}"
            if not row["complete"]:
                cell += "+"          # a floor: some calls reported nothing
            if row["cost_usd"] is not None:
                cell += f" ${row['cost_usd']:.2f}"
            parts.append(cell)

        if summary["tokens"]:
            total = f"total {_tokens(summary['tokens'])}"
            if not summary["tokens_complete"]:
                total += "+"
            if summary["cost_usd_reported"] is not None:
                total += f" ${summary['cost_usd_reported']:.2f}"
            parts.append(total)

    return " | ".join(parts) if parts else "dobby: idle"


def _bar(share: float, width: int = BAR_WIDTH) -> str:
    """`[####----]` for a 0..1 share, clamped. Never wider than `width`."""
    share = 0.0 if share != share else max(0.0, min(1.0, share))   # NaN -> 0
    filled = int(round(width * share))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _ratio(used, ceiling) -> str:
    """`[###-----]27k/80k`, or the raw count when there is no ceiling.

    A dimension with no ceiling gets no bar rather than a full one: an unbounded
    budget drawn as 100% used reads as exhausted, which is the opposite of true.
    """
    if not ceiling:
        return _tokens(int(used or 0))
    return f"{_bar((used or 0) / ceiling)}{_tokens(int(used or 0))}/{_tokens(int(ceiling))}"


def dashboard(data_dir: str, *, skill: str = "", detail: str = "",
              window_s: float | None = None, quota=None,
              context_used: float | None = None) -> str:
    """A multi-line status block: what is running, what it spent, what is left.

    `statusline` stays one line because a host may only have one. This is for a
    host that has three, and it exists because a single line cannot hold a
    per-provider breakdown AND a quota AND still be read at a glance.

    Only what dobby actually knows is drawn. There is deliberately no 5-hour or
    weekly subscription window here: those belong to the host's account, dobby
    has no way to observe them, and a bar drawn from a number nobody measured is
    worse than no bar.
    """
    summary = summarize(data_dir, window_s=window_s)
    lines = []

    head = []
    if skill:
        head.append(f"skill:{skill}" + (f"({_clip(detail, 24)})" if detail else ""))
    if summary["calls"]:
        head.append(f"session:{_short(summary['session_s'])}")
        head.append(f"agents:{summary['calls']} par:{summary['parallelism']}x")
        if summary["failed"]:
            head.append(f"failed:{summary['failed']}")
    if context_used is not None:
        head.append(f"ctx:{_bar(context_used)}{context_used:.0%}")
    if head:
        lines.append(" | ".join(head))

    if summary["calls"]:
        total = summary["tokens"] or 1
        cells = []
        for provider, row in sorted(summary["providers"].items(),
                                    key=lambda kv: -kv[1]["tokens"]):
            share = row["tokens"] / total
            cell = (f"{provider}:{_bar(share)}{share:.0%} "
                    f"{_tokens(row['tokens'])}" + ("+" if not row["complete"] else ""))
            if row["cost_usd"] is not None:
                cell += f" ${row['cost_usd']:.2f}"
            cells.append(cell)
        if cells:
            lines.append(" · ".join(cells))

    if quota is not None:
        remaining = quota.remaining() if hasattr(quota, "remaining") else quota
        config = getattr(quota, "config", None)
        row = ["quota claude"]
        for key, ceiling_attr, label in (
                ("calls", "max_calls", "calls"),
                ("thinking_tokens", "max_thinking_tokens", "think"),
                ("billable_tokens", "max_billable_tokens", "bill")):
            ceiling = getattr(config, ceiling_attr, None) if config else None
            left = remaining.get(key) if isinstance(remaining, dict) else None
            used = None if (left is None or ceiling is None) else ceiling - left
            row.append(f"{label}:{_ratio(used, ceiling)}")
        lines.append(" ".join(row))

    if summary["calls"] and summary["tokens"]:
        tail = f"total:{_tokens(summary['tokens'])}"
        if not summary["tokens_complete"]:
            tail += "+"
        if summary["cost_usd_reported"] is not None:
            tail += f" ${summary['cost_usd_reported']:.2f}"
            if not summary["dollars_complete"]:
                tail += " (metered only)"
        lines.append(tail)

    return "\n".join(lines) if lines else "dobby: idle"


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _tokens(count: int) -> str:
    """Compact token counts. A status bar has one line and no room for commas."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.2f}M".replace(".00M", "M")


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
