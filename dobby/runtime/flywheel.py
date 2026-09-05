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
#: Both live in `core.textsig` now: `core.friction` groups capability
#: failures the same way and `core` cannot import `runtime`. Re-exported
#: rather than moved outright -- `flywheel.signature` is what the tests
#: and the harvest call.
from ..core.textsig import _NOISE, signature  # noqa: F401,E402


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
    """Recurring failures across the recorded runs, most frequent first.

    One connection for the walk. This reads every recorded run and then its
    attempts, so a per-call connection costs one round trip per accumulated
    run. Measured on this machine: five runs took six connections and 0.283s,
    eighty took eighty-one and 0.780s.

    The third place in this repository with the shape, after `metrics.report`
    and `session._unconfirmed_by_run` -- reading about work already finished
    costing more the more of it there is.
    """
    with store.session():
        return _harvest(store, limit=limit, min_occurrences=min_occurrences)


def _harvest(store, *, limit: int, min_occurrences: int) -> list:
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
    """The proposals, their sample size, and an honest word when there are none.

    `runs_examined` is here because the note used to tell the reader to "check
    the run count" and the report did not carry it -- while the walk that
    produced the note had just read every run there was. An empty finding whose
    sample size the reader has to go and fetch is the same shape as a token
    total that does not say it is a floor: it reads as a measurement of zero.

    On this repository the answer was zero runs, so "nothing has failed twice"
    was true of nothing at all.
    """
    with store.session():
        runs_examined = len(store.list_runs(limit=500))
        candidates = _harvest(store, limit=500,
                              min_occurrences=min_occurrences)
    out = {"candidates": [c.to_dict() for c in candidates],
           "min_occurrences": min_occurrences,
           "runs_examined": runs_examined}
    if not candidates:
        out["note"] = (
            f"nothing has failed twice the same way across {runs_examined} "
            f"recorded run(s)"
            + (". There is nothing here to learn from yet -- this is not a "
               "finding about the system" if runs_examined < min_occurrences
               else ", which on this sample size is a real absence"))
    if write and candidates:
        out["written_to"] = write_candidates(data_dir, candidates)
    return out
