"""Recurring failures become test cases — as candidates, never automatically.

The loop this closes: a run fails, the failure is recorded with its class and
its node kind, and nothing ever looks at the pile again. Every harness has this
pile. The value in it is that a failure which happens *repeatedly* is the one
worth a permanent test, and repetition is exactly the thing a human reading one
run at a time cannot see.

Why candidates and not golden tasks
-----------------------------------
A recurring failure is evidence that something recurs. It is NOT evidence that
the system is wrong: the three most common causes of a repeated
`QUALITY_FAILURE` are a real defect, a check that is broken, and a task nobody
should have asked for. Promoting automatically would enshrine the second and
third as requirements, and a golden set with a wrong entry is worse than a small
one — every future change has to argue with it.

So this proposes, with the evidence attached, and a person promotes. The
proposal is machine-readable so the promotion is one command rather than a
retyping exercise.

Grouping
--------
By `(node_kind, failure_class, signature)`. The signature is the failure detail
with the volatile parts removed — paths, numbers, ids — because
`timeout after 120s` and `timeout after 300s` are one failure mode and counting
them separately is how a pile of twenty identical problems looks like twenty
unrelated ones.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from . import graph as G

#: How many times a failure must recur before it is worth proposing. Two is
#: deliberate: one is an incident, and waiting for five means the proposal
#: arrives after somebody has already fixed it by hand.
MIN_OCCURRENCES = 2

#: Volatile fragments, replaced before grouping. Order matters — paths first,
#: because a path contains digits that the number rule would otherwise eat.
_NOISE = (
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"/[^\s'\"]{2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hash>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:]+"), "<time>"),
    # No word boundaries. `\b\d+\b` refuses `120s` (no boundary before `s`) and
    # takes only the `0` out of `0.03s`, so two runs of the same failure that
    # merely took different amounts of time produced different signatures and
    # were counted as unrelated — which is precisely the miscount this grouping
    # exists to prevent.
    (re.compile(r"\d+(?:\.\d+)*"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def signature(detail: str) -> str:
    """A failure detail with the parts that differ between runs removed."""
    text = detail or ""
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()[:200]


@dataclass
class Candidate:
    """A repeated failure, and everything needed to argue about it."""

    node_kind: str
    failure_class: str
    signature: str
    occurrences: int = 0
    runs: list = field(default_factory=list)
    examples: list = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    def to_dict(self) -> dict:
        return {"node_kind": self.node_kind,
                "failure_class": self.failure_class,
                "signature": self.signature,
                "occurrences": self.occurrences,
                "runs": self.runs[:20], "examples": self.examples[:3],
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "status": "candidate",
                "promote_by": ("a human decides. A repeated failure is evidence "
                               "that something recurs, not that the system is "
                               "wrong — a broken check and a task nobody should "
                               "have asked for both look like this")}


def harvest(store, *, limit: int = 500,
            min_occurrences: int = MIN_OCCURRENCES) -> list[Candidate]:
    """Recurring failures across the recorded runs, most frequent first."""
    groups: dict[tuple, Candidate] = {}
    for run in store.list_runs(limit=limit):
        loaded = None
        for row in store.attempts(run["run_id"]):
            if row["outcome"] not in (G.RETRYABLE_FAILURE, G.PERMANENT_FAILURE):
                continue
            if row["detail"] == G.INTERRUPTED_DETAIL:
                # An interrupted attempt says something about the machine, not
                # about the task. Counting it would fill the pile with the one
                # failure that is already measured by `recovery_success_rate`.
                continue
            if loaded is None:
                loaded = store.load_run(run["run_id"])["graph"]
            node = loaded.nodes.get(row["node_id"])
            key = (node.kind if node else "unknown",
                   row["failure_class"] or "UNCLASSIFIED",
                   signature(row["detail"]))
            entry = groups.get(key)
            if entry is None:
                entry = Candidate(node_kind=key[0], failure_class=key[1],
                                  signature=key[2],
                                  first_seen=row["started"])
                groups[key] = entry
            entry.occurrences += 1
            entry.last_seen = row["started"]
            if run["run_id"] not in entry.runs:
                entry.runs.append(run["run_id"])
            if len(entry.examples) < 3:
                entry.examples.append({"run_id": run["run_id"],
                                       "node_id": row["node_id"],
                                       "detail": (row["detail"] or "")[:300]})
    return sorted((c for c in groups.values()
                   if c.occurrences >= min_occurrences),
                  key=lambda c: (-c.occurrences, c.node_kind))


def golden_path(data_dir: str) -> str:
    return os.path.join(data_dir, "state", "runtime", "golden_candidates.json")


def write_candidates(data_dir: str, candidates: list[Candidate]) -> str:
    """Persist the proposals, MERGING with what is already there.

    Merging rather than overwriting, because a candidate a human already
    promoted or rejected must not come back as new next week. The decision is
    the valuable part of the file and it is the part a rewrite would lose.
    """
    path = golden_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for entry in json.load(handle).get("candidates", []):
                existing[_key(entry)] = entry

    for candidate in candidates:
        row = candidate.to_dict()
        key = _key(row)
        if key in existing:
            # Keep the human's decision; refresh only what was counted.
            existing[key].update({
                "occurrences": row["occurrences"], "runs": row["runs"],
                "last_seen": row["last_seen"], "examples": row["examples"]})
        else:
            existing[key] = row

    payload = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "candidates": sorted(existing.values(),
                                    key=lambda c: -c["occurrences"])}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _key(entry: dict) -> str:
    return "|".join((entry.get("node_kind", ""), entry.get("failure_class", ""),
                     entry.get("signature", "")))


def report(store, data_dir: str, *, write: bool = False,
           min_occurrences: int = MIN_OCCURRENCES) -> dict:
    """The proposals, and an honest word when there are none."""
    candidates = harvest(store, min_occurrences=min_occurrences)
    out = {"candidates": [c.to_dict() for c in candidates],
           "min_occurrences": min_occurrences}
    if not candidates:
        out["note"] = (
            "nothing has failed twice the same way. That is either a healthy "
            "system or a young one — check the run count before reading it as "
            "the first")
    if write and candidates:
        out["written_to"] = write_candidates(data_dir, candidates)
    return out
