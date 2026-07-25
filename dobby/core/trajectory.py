"""Trajectory recording, checkpoints, and structured handoff.

One JSONL file per task under <data_dir>/state/trajectories/. The handoff
document is the machine+human readable contract for session continuity
(mission §2.5): done / remaining / decisions / evidence / next steps.
Failure events carry a multi-level classification (memory / retrieval /
planning / action / tool_selection / evaluator / orchestration / policy /
system) per the failure-reflection literature.
"""

from __future__ import annotations

import json
import os
import time
import uuid

FAILURE_LEVELS = ("action", "trajectory", "task_family", "cross_task",
                  "repository_policy", "retrieval", "tool_selection",
                  "evaluator", "memory", "orchestration", "system")


class Trajectory:
    def __init__(self, data_dir: str, task: str, task_id: str | None = None):
        self.dir = os.path.join(data_dir, "state", "trajectories")
        os.makedirs(self.dir, exist_ok=True)
        self.task_id = task_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.path = os.path.join(self.dir, f"{self.task_id}.jsonl")
        self.task = task
        if not os.path.exists(self.path):
            self.append("task_start", {"task": task})

    def append(self, event: str, payload: dict) -> dict:
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **payload}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def record_failure(self, level: str, symptom: str, root_cause: str,
                       evidence: str) -> dict:
        if level not in FAILURE_LEVELS:
            raise ValueError(f"unknown failure level '{level}'")
        return self.append("failure", {"level": level, "symptom": symptom,
                                       "root_cause": root_cause,
                                       "evidence": evidence})

    def events(self) -> list[dict]:
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
        return out

    @classmethod
    def resume(cls, data_dir: str, task_id: str) -> "Trajectory":
        t = cls.__new__(cls)
        t.dir = os.path.join(data_dir, "state", "trajectories")
        t.task_id = task_id
        t.path = os.path.join(t.dir, f"{task_id}.jsonl")
        if not os.path.exists(t.path):
            raise FileNotFoundError(t.path)
        first = t.events()[0]
        t.task = first.get("task", "")
        return t

    def handoff(self, done: list[str], remaining: list[str],
                decisions: list[dict], evidence: list[str],
                next_steps: list[str],
                runners: list[dict] | None = None) -> str:
        """Write the structured handoff markdown; returns its path.

        runners: optional per-run status ledger (who/what ran, ok/degraded/
        failed) — an honesty primitive for multi-tool or multi-agent runs."""
        path = os.path.join(self.dir, f"{self.task_id}.handoff.md")
        lines = [f"# Handoff — {self.task_id}", "",
                 f"Task: {self.task}", ""]
        if runners:
            lines += ["## Runners (status ledger)"]
            lines += [f"- {r.get('name')}: {r.get('status')}"
                      + (f" — {r['note']}" if r.get("note") else "")
                      for r in runners]
            lines += [""]
        lines += ["## Done (with evidence)"]
        lines += [f"- {d}" for d in done] or ["- (nothing)"]
        lines += ["", "## Remaining"]
        lines += [f"- {r}" for r in remaining] or ["- (nothing)"]
        lines += ["", "## Decisions (what/why/alternative rejected)"]
        for d in decisions:
            lines.append(f"- {d.get('what')} — why: {d.get('why')} — "
                         f"rejected: {d.get('rejected', 'n/a')}")
        lines += ["", "## Evidence paths"]
        lines += [f"- {e}" for e in evidence]
        lines += ["", "## Next steps (start here)"]
        lines += [f"1. {s}" for s in next_steps]
        lines += ["", f"Trajectory: {os.path.basename(self.path)}"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.append("handoff_written", {"path": path})
        return path

    @staticmethod
    def latest_handoff(data_dir: str) -> str | None:
        d = os.path.join(data_dir, "state", "trajectories")
        if not os.path.isdir(d):
            return None
        cands = [os.path.join(d, f) for f in os.listdir(d)
                 if f.endswith(".handoff.md")]
        return max(cands, key=os.path.getmtime) if cands else None
