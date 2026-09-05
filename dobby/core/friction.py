"""Friction report (adopted from oh-my-claudecode's session friction mining):
scan this instance's recorded history for operator-friction / context-bloat
signals and emit a structured report. Pure stdlib, read-only.

Signals:
  - repeated_commands: the same execute command run 3+ times in one task
    (retry loop or lost state);
  - consecutive_repeats: 3+ identical consecutive event types (thrashing);
  - oversized_events: events whose serialized size exceeds the cap (context
    bloat entering the transcript);
  - failure_hotspots: failure counts by level across tasks (where the harness
    is actually leaking);
  - handoff_gaps: tasks with 5+ events but no handoff written (Handoff
    Amnesia risk);
  - capability_failures: capability calls through the MCP gateway that came
    back with an error, grouped by capability and normalised message.

Output feeds the improvement loop: each signal names the file it came from so
a human/agent can turn it into a policy/skill/criteria candidate.

TWO THINGS THIS REPORT USED TO GET WRONG, both measured on this repository:

`verdict` said "clean" and said nothing about WHEN. On 2026-09-04 the four
trajectory files here ended on 2026-08-18 -- a verdict about mid-August,
printed as a verdict about today. An empty finding whose window the reader has
to go and fetch reads as a measurement of the present. The window is now part
of the report and part of the verdict, and a corpus that stopped growing is
called stale rather than clean.

`.dobby/state/audit.jsonl` was read by nothing. It was the only evidence store
here still growing -- 366 entries reaching 2026-09-04, against trajectories
that stopped seventeen days earlier -- and the whole self-improvement chain
walked past it. It is read now. What it can contribute only became worth
reading once the gateway started recording OUTCOMES: before that every line
said what was attempted and none said whether it worked.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections import Counter

from .textsig import signature

#: A trajectory corpus older than this is reported as stale rather than clean.
#: Two weeks is not a law -- it is long enough that a quiet fortnight is a
#: deliberate quiet fortnight, and short enough that a corpus abandoned mid
#: project is caught. It is a parameter so a caller who disagrees can say so.
STALE_AFTER_DAYS = 14

#: The two gateway capabilities that write the improvement loop's OWN input.
#: Everything else a capability does is work; these two are the work being
#: recorded. If neither has ever been called, the loop is not quiet -- it is
#: unfed, and every downstream report is a report about nothing.
EVIDENCE_CAPABILITIES = ("record_evidence", "handoff")


def _events(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def _stamp(event: dict) -> str | None:
    """The event's timestamp, or None. Compared as ISO text, which sorts."""
    value = event.get("t")
    return value if isinstance(value, str) and value else None


def _days_between(older: str, newer: str) -> int | None:
    try:
        a = _dt.datetime.fromisoformat(older)
        b = _dt.datetime.fromisoformat(newer)
    except ValueError:
        return None
    return (b - a).days


def _registered_capabilities(data_dir: str) -> list[str]:
    """Every capability the gateway offers, or none if there is no registry.

    Read so the report can say which ones have never been called. A registry
    that cannot be read yields an empty list and therefore no claim -- an
    unreadable registry is not evidence that nothing is registered.
    """
    path = os.path.join(data_dir, "registry", "capabilities.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        return []
    items = data.get("capabilities", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list):
        return []
    return sorted({c["id"] for c in items
                   if isinstance(c, dict) and c.get("id")})


def _audit_signals(data_dir: str) -> dict:
    """What the gateway's own log says about capability calls.

    Only `result` lines carry an outcome. Older logs have none at all, and the
    report says so rather than reporting zero failures out of nothing -- the
    same distinction the rest of this module is about.
    """
    path = os.path.join(data_dir, "state", "audit.jsonl")
    out = {"audit_entries": 0, "audit_window": {}, "audited_outcomes": 0,
           "capability_failures": [], "capabilities_used": {},
           "capabilities_never_used": [], "unfed": False}
    registered = _registered_capabilities(data_dir)
    if not os.path.exists(path):
        out["capabilities_never_used"] = registered
        out["unfed"] = any(c in registered for c in EVIDENCE_CAPABILITIES)
        return out
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue           # a torn last line is not a finding
    if not rows:
        out["capabilities_never_used"] = registered
        out["unfed"] = any(c in registered for c in EVIDENCE_CAPABILITIES)
        return out
    stamps = sorted(s for s in (_stamp(r) for r in rows) if s)
    out["audit_entries"] = len(rows)
    if stamps:
        out["audit_window"] = {"oldest": stamps[0], "newest": stamps[-1]}
    out["capabilities_used"] = dict(
        Counter(r.get("id") for r in rows
                if r.get("kind") == "invoke" and r.get("id")))
    out["capabilities_never_used"] = [c for c in registered
                                      if c not in out["capabilities_used"]]
    out["unfed"] = all(c not in out["capabilities_used"]
                       for c in EVIDENCE_CAPABILITIES
                       if c in registered) and bool(
        [c for c in EVIDENCE_CAPABILITIES if c in registered])

    results = [r for r in rows if r.get("kind") == "result"]
    out["audited_outcomes"] = len(results)
    grouped: dict = {}
    for row in results:
        if row.get("ok"):
            continue
        key = (row.get("id") or "unknown", signature(str(row.get("error") or "")))
        entry = grouped.setdefault(
            key, {"capability": key[0], "signature": key[1], "times": 0,
                  "first_seen": None, "last_seen": None})
        entry["times"] += 1
        stamp = _stamp(row)
        if stamp:
            if entry["first_seen"] is None or stamp < entry["first_seen"]:
                entry["first_seen"] = stamp
            if entry["last_seen"] is None or stamp > entry["last_seen"]:
                entry["last_seen"] = stamp
    out["capability_failures"] = sorted(
        grouped.values(), key=lambda e: (-e["times"], e["capability"]))
    return out


def friction_report(data_dir: str, oversize_chars: int = 8000, *,
                    now: str | None = None,
                    stale_after_days: int = STALE_AFTER_DAYS) -> dict:
    """Every signal, the window it was measured over, and an honest verdict.

    `now` is injectable so a test can assert the staleness boundary without
    depending on the day it runs. It defaults to the clock.
    """
    traj_dir = os.path.join(data_dir, "state", "trajectories")
    report = {"tasks_scanned": 0, "repeated_commands": [],
              "consecutive_repeats": [], "oversized_events": [],
              "failure_hotspots": {}, "handoff_gaps": [],
              "window": {}, "stale": False}
    report.update(_audit_signals(data_dir))
    if not os.path.isdir(traj_dir):
        report["verdict"] = _verdict(report)
        return report
    fail_counter: Counter = Counter()
    stamps: list[str] = []
    for fn in sorted(os.listdir(traj_dir)):
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(traj_dir, fn)
        events = _events(path)
        report["tasks_scanned"] += 1
        stamps.extend(s for s in (_stamp(e) for e in events) if s)

        cmds = Counter(e.get("command") for e in events
                       if e.get("event") == "execute" and e.get("command"))
        for cmd, n in cmds.items():
            if n >= 3:
                report["repeated_commands"].append(
                    {"file": fn, "command": cmd[:200], "times": n})

        streak, prev = 1, None
        for e in events:
            kind = e.get("event")
            streak = streak + 1 if kind == prev else 1
            if streak == 3:
                report["consecutive_repeats"].append(
                    {"file": fn, "event": kind})
            prev = kind

        for i, e in enumerate(events):
            size = len(json.dumps(e, ensure_ascii=False))
            if size > oversize_chars:
                report["oversized_events"].append(
                    {"file": fn, "index": i, "event": e.get("event"),
                     "chars": size})

        for e in events:
            if e.get("event") == "failure":
                fail_counter[e.get("level", "unknown")] += 1

        if len(events) >= 5 and not any(e.get("event") == "handoff_written"
                                        for e in events):
            report["handoff_gaps"].append(fn)

    report["failure_hotspots"] = dict(fail_counter)
    if stamps:
        stamps.sort()
        today = now or _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        age = _days_between(stamps[-1], today)
        report["window"] = {"oldest": stamps[0], "newest": stamps[-1],
                            "days_since_newest": age}
        report["stale"] = age is not None and age > stale_after_days
    report["verdict"] = _verdict(report)
    return report


def _verdict(report: dict) -> str:
    """What the signals add up to, and never without the window.

    Ordered worst-first: a stale corpus is reported as stale even when it is
    also clean, because "clean" about a corpus that stopped growing is a claim
    about the past wearing the tense of the present.
    """
    if report.get("unfed"):
        never = ", ".join(c for c in EVIDENCE_CAPABILITIES
                          if c in report.get("capabilities_never_used", []))
        return (f"UNFED: {never} has never been called, so this report is "
                f"about whatever was recorded before that stopped "
                f"({report['tasks_scanned']} task(s)) and not about the work "
                f"being done now")
    if not report["tasks_scanned"]:
        if report.get("capability_failures"):
            return ("no trajectories yet, but the gateway log records "
                    f"{len(report['capability_failures'])} distinct "
                    "capability failure(s) -- route those")
        if report.get("audit_entries"):
            return (f"no trajectories yet; the gateway log has "
                    f"{report['audit_entries']} entries and "
                    f"{report['audited_outcomes']} of them record an outcome")
        return "no trajectories yet"

    window = report.get("window") or {}
    span = (f" over {report['tasks_scanned']} task(s) ending "
            f"{window.get('newest', 'at an unrecorded time')}")
    dirty = any(report[k] for k in ("repeated_commands", "consecutive_repeats",
                                    "oversized_events", "handoff_gaps"))
    dirty = dirty or bool(report["failure_hotspots"]) \
        or bool(report.get("capability_failures"))
    if dirty:
        return ("friction found" + span + " -- route each signal per the "
                "maintenance rules (docs/OPERATING_MANUAL.md)")
    if report.get("stale"):
        return ("no friction" + span + f", but that is "
                f"{window.get('days_since_newest')} day(s) old: this is a "
                "verdict about then, not a verdict about now")
    return "clean" + span
