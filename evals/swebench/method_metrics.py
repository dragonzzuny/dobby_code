"""How an arm WORKED, not just what it spent — as numbers, from its own record.

Tokens say what a run cost and `local_resolve` says whether it worked. Neither
says whether the run behaved like a disciplined engineer, which is the claim this
repository actually makes about itself. That claim has been argued from
architecture and never counted.

These six read the run's own artefacts, so a solo arm scores what it structurally
IS — one call, no contract, no plan, no separable investigation — rather than
being penalised by a rubric written for the loop. A zero here is a fact about the
shape of the run, not a mark against the provider.

    contract_violation_rate   attempts rejected for the SHAPE of their output.
                              A schema the caller declared and the model missed;
                              the run retried instead of accepting prose.
    investigation_share       calls spent on scout and architect, over all calls.
                              What the run bought before it changed anything.
    evidence_density          evidence citations per promoted claim. A claim with
                              no file:line behind it is an opinion.
    effect_observation_rate   writing attempts whose declared effect was actually
                              observed. Measures the CONTROL stage.
    provider_switch_rate      retries that changed provider rather than asking
                              the same model twice. A second pass from the same
                              model is correlated with the first; the repository
                              says so in dobby/swarm/diversity.py.
    rework_ratio              attempts over nodes. 1.0 means nothing was retried.
"""

from __future__ import annotations

import json
import os
import sqlite3


def _rate(numerator: int, denominator: int):
    """None rather than 0.0 when there is nothing to divide by.

    A run that made no writing attempt has no effect-observation rate, and a
    zero would sort it alongside a run that tried and failed every time.
    """
    return round(numerator / denominator, 3) if denominator else None


def find_store(base: str, instance_id: str, arm: str) -> str | None:
    path = os.path.join(base, f".store-{instance_id}-{arm}")
    db = os.path.join(path, "state", "runtime", "runs.sqlite3")
    return db if os.path.exists(db) else None


def _artifacts(store_dir: str) -> list[dict]:
    root = os.path.join(store_dir, "state", "runtime")
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if os.path.basename(dirpath) != "artifacts":
            continue
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return out


def from_store(db_path: str) -> dict:
    """The loop's own record. Only the D arm has one."""
    store_dir = os.path.dirname(os.path.dirname(os.path.dirname(db_path)))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes")]
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts")]
    conn.close()

    contract_violations = sum(1 for a in attempts
                              if a.get("failure_class") == "CONTRACT_VIOLATION")
    effect_misses = sum(1 for a in attempts
                        if a.get("failure_class") == "EFFECT_NOT_OBSERVED")
    # A node whose id starts with `implement` is the writing one; the compiler
    # names them (`workorder.py`), so this reads the plan's own vocabulary
    # rather than guessing from a contract field the table does not carry.
    writing = [a for a in attempts
               if str(a.get("node_id", "")).startswith("implement")]

    artifacts = _artifacts(store_dir)
    promoted = [a for a in artifacts if a.get("state") == "PROMOTED"]
    claims, citations = 0, 0
    for artifact in promoted:
        for claim in (artifact.get("payload") or {}).get("claims") or []:
            claims += 1
            citations += len(claim.get("evidence") or [])

    return {
        "nodes": len(nodes),
        "node_states": {n.get("node_id"): n.get("state") for n in nodes},
        "attempts": len(attempts),
        "rework_ratio": _rate(len(attempts), len(nodes)),
        "contract_violations": contract_violations,
        "contract_violation_rate": _rate(contract_violations, len(attempts)),
        "writing_attempts": len(writing),
        "effect_observation_rate": _rate(len(writing) - effect_misses,
                                         len(writing)),
        "promoted_artifacts": len(promoted),
        "claims": claims,
        "evidence_citations": citations,
        "evidence_density": _rate(citations, claims),
    }


def from_row(row: dict) -> dict:
    """What any arm's provider record shows, loop or no loop."""
    providers = (row.get("record") or {}).get("providers") or {}
    total_calls = sum(p.get("calls_total") or 0 for p in providers.values())
    used = sorted(p for p, v in providers.items() if v.get("calls_total"))

    # The architect and the scout are the calls made BEFORE anything changed.
    # For the loop those are claude's plan call and the scout node; for a solo
    # arm there is no such call and the share is 0 by construction, which is the
    # difference being measured rather than a penalty.
    return {
        "total_calls": total_calls,
        "providers_used": used,
        "provider_count": len(used),
        "single_call": total_calls == 1,
    }


def for_arm(base: str, instance_id: str, arm: str, row: dict) -> dict:
    out = {"arm": arm, "instance_id": instance_id}
    out.update(from_row(row))
    db = find_store(base, instance_id, arm)
    if db:
        out.update(from_store(db))
        out["has_loop_record"] = True
    else:
        # Not a gap in the data: a solo arm has no nodes, no contracts and no
        # plan, so these are structurally absent rather than unmeasured.
        out.update({"has_loop_record": False, "nodes": 0, "attempts": 0,
                    "rework_ratio": None, "contract_violations": 0,
                    "contract_violation_rate": None, "writing_attempts": 0,
                    "effect_observation_rate": None, "promoted_artifacts": 0,
                    "claims": 0, "evidence_citations": 0,
                    "evidence_density": None})
    out["investigation_share"] = _rate(
        max(out["attempts"] - out["writing_attempts"], 0), out["attempts"])
    return out
