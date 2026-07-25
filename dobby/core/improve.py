"""Bounded self-improvement loop: observe -> diagnose -> propose -> validate
-> promote/reject -> monitor, with rollback.

Improvement candidates here are DATA edits (KG keywords/edges, retrieval
weights, policy trigger keywords) — never edits to evaluation gold, criteria,
or holdout sets (anti Evaluation-Gaming; enforced by path checks). Promotion
gates (mission §8.5): original case improves, no regression on the regression
set, generalization on neighbors, reversible (snapshot), threshold exceeded.
Rejected candidates are recorded so the same bad idea is not rediscovered.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid

FORBIDDEN_TARGETS = ("retrieval_gold", "criteria", "holdout", "scenarios")

CANDIDATE_KINDS = ("kg_keyword_add", "kg_edge_add", "retrieval_weights",
                   "policy_trigger_add")


class ImprovementError(ValueError):
    pass


class ImprovementLoop:
    def __init__(self, data_dir: str, fitness_fn):
        """fitness_fn(split) -> dict with keys: score (float), per_case (dict).
        split in {"dev", "val", "holdout"}. The loop never optimizes on
        holdout; it is measured once at promotion time and recorded."""
        self.data_dir = data_dir
        self.fitness = fitness_fn
        self.state_dir = os.path.join(data_dir, "state", "improvement")
        os.makedirs(self.state_dir, exist_ok=True)
        self.rejected_path = os.path.join(self.state_dir, "rejected.jsonl")
        self.promoted_path = os.path.join(self.state_dir, "promoted.jsonl")

    # -- propose ---------------------------------------------------------------
    def make_candidate(self, kind: str, payload: dict, origin_failure: str) -> dict:
        if kind not in CANDIDATE_KINDS:
            raise ImprovementError(f"unknown candidate kind '{kind}'")
        target = payload.get("target_file", "")
        if any(t in target for t in FORBIDDEN_TARGETS):
            raise ImprovementError(
                f"candidate may not modify evaluation assets: {target}")
        return {"id": uuid.uuid4().hex[:10], "kind": kind, "payload": payload,
                "origin_failure": origin_failure,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S")}

    def already_rejected(self, candidate: dict) -> bool:
        key = json.dumps({"kind": candidate["kind"],
                          "payload": candidate["payload"]}, sort_keys=True)
        if not os.path.exists(self.rejected_path):
            return False
        with open(self.rejected_path, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if json.dumps({"kind": rec["kind"], "payload": rec["payload"]},
                              sort_keys=True) == key:
                    return True
        return False

    # -- apply / rollback -----------------------------------------------------
    def _snapshot(self, target_file: str) -> str:
        snap = os.path.join(self.state_dir,
                            f"snap_{uuid.uuid4().hex[:8]}_{os.path.basename(target_file)}")
        shutil.copy2(target_file, snap)
        return snap

    def apply(self, candidate: dict) -> str:
        """Apply the data edit; returns snapshot path for rollback."""
        payload = candidate["payload"]
        target = payload["target_file"]
        snap = self._snapshot(target)
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
        kind = candidate["kind"]
        if kind == "kg_keyword_add":
            for n in data["nodes"]:
                if n["id"] == payload["node_id"]:
                    n["keywords"] = sorted(set(n.get("keywords", [])
                                               + payload["keywords"]))
                    break
            else:
                raise ImprovementError(f"node {payload['node_id']} not found")
        elif kind == "kg_edge_add":
            data["edges"].append(payload["edge"])
        elif kind == "retrieval_weights":
            data.setdefault("retrieval_weights", {}).update(payload["weights"])
        elif kind == "policy_trigger_add":
            for p in data["policies"]:
                if p["id"] == payload["policy_id"]:
                    p["trigger_keywords"] = sorted(set(p["trigger_keywords"]
                                                       + payload["keywords"]))
                    break
            else:
                raise ImprovementError(f"policy {payload['policy_id']} not found")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
        return snap

    def rollback(self, candidate: dict, snapshot: str) -> None:
        shutil.copy2(snapshot, candidate["payload"]["target_file"])

    # -- the bounded loop --------------------------------------------------------
    def run_once(self, candidate: dict, min_gain: float = 0.005,
                 max_regression: float = 0.0) -> dict:
        """Validate one candidate. Returns a decision record; applies
        promotion or rolls back. Never touches holdout during search."""
        if self.already_rejected(candidate):
            return {"decision": "rejected", "reason": "previously rejected "
                    "(negative memory)", "candidate": candidate}
        before_dev = self.fitness("dev")
        before_val = self.fitness("val")
        snap = self.apply(candidate)
        after_dev = self.fitness("dev")
        after_val = self.fitness("val")
        gain_dev = after_dev["score"] - before_dev["score"]
        gain_val = after_val["score"] - before_val["score"]
        regressed = [cid for cid, v in before_val["per_case"].items()
                     if after_val["per_case"].get(cid, v) < v]
        decision = None
        reasons = []
        if gain_dev < min_gain:
            decision, r = "rejected", f"dev gain {gain_dev:.4f} < threshold {min_gain}"
            reasons.append(r)
        if gain_val < -abs(max_regression) or regressed:
            decision = "rejected"
            reasons.append(f"validation regression: Δ={gain_val:.4f}, "
                           f"regressed cases={regressed}")
        rec = {"candidate": candidate, "snapshot": snap,
               "dev_before": before_dev["score"], "dev_after": after_dev["score"],
               "val_before": before_val["score"], "val_after": after_val["score"],
               "regressed_cases": regressed,
               "date": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if decision == "rejected":
            self.rollback(candidate, snap)
            rec.update(decision="rejected", reason="; ".join(reasons))
            self._log(self.rejected_path, {**candidate, "reason": rec["reason"]})
        else:
            holdout = self.fitness("holdout")   # measured once, recorded, not optimized
            rec.update(decision="promoted", holdout_after=holdout["score"])
            self._log(self.promoted_path, rec)
        return rec

    def _log(self, path: str, rec: dict) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
