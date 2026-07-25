"""Machine-readable policy layer (KnowAgent-style action knowledge base).

Policies mirror docs/AGENT_OPERATING_MANUAL.md POLICY-* blocks as data:
trigger patterns, required actions, forbidden shortcuts, completion evidence,
escalation. The prose manual stays canonical for humans; this file makes the
same rules retrievable, matchable, and testable. On conflict: re-derive from
the manual, fix the JSON, add a `contradicts` KG edge until resolved.
"""

from __future__ import annotations

import json
import re

REQUIRED_FIELDS = ("id", "trigger_keywords", "required_actions",
                   "forbidden", "evidence_of_completion", "severity")


class PolicyError(ValueError):
    pass


class PolicyBook:
    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.policies: list[dict] = data["policies"]
        for p in self.policies:
            for k in REQUIRED_FIELDS:
                if k not in p:
                    raise PolicyError(f"policy {p.get('id','?')} missing '{k}'")

    def match(self, task_text: str) -> list[dict]:
        """Return policies whose triggers fire for this task, severity-sorted.

        Matching is deliberately recall-biased: keyword OR regex hit fires.
        A policy that fires needlessly costs a little context; one that fails
        to fire costs an invariant."""
        text = task_text.lower()
        hits = []
        for p in self.policies:
            fired = [kw for kw in p["trigger_keywords"] if kw.lower() in text]
            for rx in p.get("trigger_regex", []):
                if re.search(rx, task_text, re.IGNORECASE):
                    fired.append(f"regex:{rx}")
            if p.get("always_on"):
                fired.append("always_on")
            if fired:
                hits.append({**p, "fired_on": fired})
        sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        hits.sort(key=lambda p: (sev.get(p["severity"], 9), p["id"]))
        return hits

    def get(self, pid: str) -> dict:
        for p in self.policies:
            if p["id"] == pid:
                return p
        raise PolicyError(f"unknown policy '{pid}'")
