"""Friction report (adopted from oh-my-claudecode's session friction mining):
scan this instance's trajectory files for operator-friction / context-bloat
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
    Amnesia risk).

Output feeds the improvement loop: each signal names the trajectory file so a
human/agent can turn it into a policy/skill/criteria candidate.
"""

from __future__ import annotations

import json
import os
from collections import Counter


def _events(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def friction_report(data_dir: str, oversize_chars: int = 8000) -> dict:
    traj_dir = os.path.join(data_dir, "state", "trajectories")
    report = {"tasks_scanned": 0, "repeated_commands": [],
              "consecutive_repeats": [], "oversized_events": [],
              "failure_hotspots": {}, "handoff_gaps": []}
    if not os.path.isdir(traj_dir):
        return report
    fail_counter: Counter = Counter()
    for fn in sorted(os.listdir(traj_dir)):
        if not fn.endswith(".jsonl"):
            continue
        path = os.path.join(traj_dir, fn)
        events = _events(path)
        report["tasks_scanned"] += 1

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
    report["verdict"] = ("clean" if not any(
        report[k] for k in ("repeated_commands", "consecutive_repeats",
                            "oversized_events", "handoff_gaps"))
        and not fail_counter else
        "friction found — route each signal per the maintenance rules "
        "(docs/OPERATING_MANUAL.md)") if report["tasks_scanned"] else "no trajectories yet"
    return report
