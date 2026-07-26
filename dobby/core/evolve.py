"""Cross-project evolution: the kit improves with every project it serves.

Per-instance learning (improve.py) stays local to a host project. This module
adds the cross-project loop:

  instance A ── export_experience() ──► experience packet (JSON)
  instance B ── export_experience() ──► experience packet
                                            │
                        kit maintainer runs harvest() on the KIT
                                            ▼
   generic candidates re-validated against kit self-gold + EVERY archived
   project gold (federation regression) via the same ImprovementLoop gates
                promote → kit generic data · reject → shared negative memory

Guarantees:
  * Domain knowledge never enters the kit: a candidate qualifies as generic
    only if its target already exists in the kit's shipped artifacts
    (self-KG node ids, universal policy ids, config weights).
  * No cross-project pollution: promotion requires gain on the multi-gold dev
    mean with ZERO per-case regression across all archived golds (a lesson
    from project A must not hurt project B).
  * Dead ends are shared: every instance's rejected candidates merge (deduped)
    into the kit's negative memory, so no future project rediscovers them.
  * Failure lessons merge into bounded negative memory with project provenance.
"""

from __future__ import annotations

import json
import os
import re
import time

import yaml

from .improve import ImprovementLoop
from .memory import MemoryStore
from .optimizer import RetrievalFitness
from .kg import Ontology
from .bootstrap import merged_graph

GENERIC_CANDIDATE_KINDS = ("kg_keyword_add", "kg_edge_add",
                           "retrieval_weights", "policy_trigger_add")


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


# ------------------------------------------------------------- export -------
def export_experience(instance_dir: str, out_path: str | None = None,
                      include_gold: bool = True) -> str:
    """Bundle an instance's validated experience into a portable packet.

    instance_dir = the kit folder inside the host project."""
    data = os.path.join(instance_dir, ".dobby")
    imp = os.path.join(data, "state", "improvement")
    with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    failures = []
    traj_dir = os.path.join(data, "state", "trajectories")
    if os.path.isdir(traj_dir):
        for fn in sorted(os.listdir(traj_dir)):
            if fn.endswith(".jsonl"):
                failures += [e for e in _read_jsonl(os.path.join(traj_dir, fn))
                             if e.get("event") == "failure"]
    gold = None
    gold_path = os.path.join(instance_dir, "evals", "retrieval_gold.yaml")
    if include_gold and os.path.exists(gold_path):
        with open(gold_path, encoding="utf-8") as f:
            gold = yaml.safe_load(f)
    project = os.path.basename(os.path.dirname(os.path.abspath(instance_dir))) \
        or os.path.basename(os.path.abspath(instance_dir))
    packet = {
        "packet_version": 1,
        "project": re.sub(r"[^A-Za-z0-9_.가-힣-]", "_", project),
        "date": time.strftime("%Y-%m-%d"),
        "promoted": _read_jsonl(os.path.join(imp, "promoted.jsonl")),
        "rejected": _read_jsonl(os.path.join(imp, "rejected.jsonl")),
        "weights": {**config.get("retrieval_weights", {}),
                    "context_k": config.get("context_k", 8)},
        "failures": failures,
        "gold": gold,
    }
    out_path = out_path or os.path.join(
        instance_dir, "reports",
        f"experience_{packet['project']}_{time.strftime('%Y%m%d')}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(packet, f, ensure_ascii=False, indent=1)
    return out_path


# ---------------------------------------------------------- federation ------
def federation_gold_paths(kit_dir: str) -> list[str]:
    """Kit self-gold + every archived project gold."""
    paths = []
    self_gold = os.path.join(kit_dir, "evals", "retrieval_gold.yaml")
    if os.path.exists(self_gold):
        paths.append(self_gold)
    fed = os.path.join(kit_dir, "evals", "federation")
    if os.path.isdir(fed):
        paths += sorted(os.path.join(fed, f) for f in os.listdir(fed)
                        if f.endswith(".yaml"))
    return paths


def make_federation_fitness(kit_dir: str):
    """fitness(split) = mean score across all golds; per_case keys are
    '<goldname>/<caseid>' so the zero-regression gate spans every project.
    Cases whose required nodes are absent from the kit KG are skipped (they
    are domain cases; the kit is only accountable for generic knowledge)."""
    golds = []
    for p in federation_gold_paths(kit_dir):
        with open(p, encoding="utf-8") as f:
            golds.append((os.path.splitext(os.path.basename(p))[0]
                          if "federation" in p else "self",
                          yaml.safe_load(f)))

    def fitness(split: str) -> dict:
        data = os.path.join(kit_dir, ".dobby")
        onto = Ontology.load(os.path.join(data, "ontology.json"))
        kg = merged_graph(onto, data)
        with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
            config = json.load(f)
        cfg = dict(config.get("retrieval_weights", {}))
        cfg["context_k"] = config.get("context_k", 8)
        per_case, scores = {}, []
        for name, gold in golds:
            cases = [c for c in gold.get(split, [])
                     if all(n in kg.nodes for n in c["required_nodes"])]
            if not cases:
                continue
            res = RetrievalFitness(kg, {split: cases})(cfg, split=split)
            for cid, s in res["per_case"].items():
                per_case[f"{name}/{cid}"] = s
            scores.append(res["score"])
        return {"score": sum(scores) / len(scores) if scores else 0.0,
                "per_case": per_case, "split": split}
    return fitness


# ------------------------------------------------------------ harvest -------
def _retarget(candidate: dict, kit_dir: str) -> dict | None:
    """Return a kit-targeted copy of a generic candidate, or None if the
    candidate is domain-specific (its target does not exist in the kit)."""
    kind = candidate["kind"]
    if kind not in GENERIC_CANDIDATE_KINDS:
        return None
    payload = dict(candidate["payload"])
    data = os.path.join(kit_dir, ".dobby")
    if kind in ("kg_keyword_add", "kg_edge_add"):
        target = os.path.join(data, "knowledge", "kg.json")
        with open(target, encoding="utf-8") as f:
            kit_kg = json.load(f)
        node_ids = {n["id"] for n in kit_kg["nodes"]}
        if kind == "kg_keyword_add" and payload.get("node_id") not in node_ids:
            return None
        if kind == "kg_edge_add":
            e = payload.get("edge", {})
            if e.get("src") not in node_ids or e.get("dst") not in node_ids:
                return None
    elif kind == "policy_trigger_add":
        target = os.path.join(data, "policies", "policies.json")
        with open(target, encoding="utf-8") as f:
            ids = {p["id"] for p in json.load(f)["policies"]}
        if payload.get("policy_id") not in ids:
            return None
    else:  # retrieval_weights
        target = os.path.join(data, "config.json")
    payload["target_file"] = target
    return {"kind": kind, "payload": payload,
            "origin_failure": candidate.get("origin_failure", "harvested")}


def _load_packet(path: str) -> tuple[dict | None, str]:
    """Read one experience packet, or say why it cannot be used.

    Returns `(packet, "")` on success and `(None, reason)` otherwise. Nothing
    here raises: `harvest` processes a batch, and the caller needs the reason
    attached to the offending path rather than a traceback that names only the
    last file opened.

    `project` is required because every downstream record is attributed to it —
    negative memory, archived gold, promotion provenance. A packet without one
    cannot be traced back to its source, and untraceable imported knowledge is
    exactly what the genericity filter exists to prevent.
    """
    if not os.path.exists(path):
        return None, f"no such packet: {path}"
    try:
        with open(path, encoding="utf-8") as f:
            packet = json.load(f)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    if not isinstance(packet, dict):
        return None, f"packet is a {type(packet).__name__}, expected an object"
    project = packet.get("project")
    if not isinstance(project, str) or not project.strip():
        return None, ("packet has no 'project' name; imported knowledge must be "
                      "attributable to its source")
    return packet, ""


def harvest(kit_dir: str, packet_paths: list[str], min_gain: float = 0.005,
            fitness=None) -> dict:
    """Merge experience packets into the KIT through the improvement gates."""
    injected = fitness is not None
    fitness = fitness or make_federation_fitness(kit_dir)
    report = {"packets": [], "promoted": [], "rejected": [],
              "skipped_domain": 0, "negative_merged": 0, "lessons": 0,
              "golds_archived": []}
    data = os.path.join(kit_dir, ".dobby")
    memory = MemoryStore(data)

    for path in packet_paths:
        # Packets arrive from OTHER projects: they are external input, and this
        # function is the boundary. A malformed one previously crashed the whole
        # harvest — an empty object raised KeyError, a truncated file raised
        # JSONDecodeError, a wrong path raised FileNotFoundError — so one bad
        # packet destroyed the batch and lost every good packet alongside it.
        # Same principle as a provider fan-out: reject the item, keep the round.
        packet, why = _load_packet(path)
        if packet is None:
            report.setdefault("unreadable", []).append(
                {"path": path, "reason": why})
            continue
        project = packet["project"]
        report["packets"].append(project)

        # 1. archive the project's gold for federation regression (never as
        #    optimizer input in the instance sense — only as a regression set)
        if packet.get("gold"):
            fed_dir = os.path.join(kit_dir, "evals", "federation")
            os.makedirs(fed_dir, exist_ok=True)
            gp = os.path.join(fed_dir, f"{project}.yaml")
            if not os.path.exists(gp):
                with open(gp, "w", encoding="utf-8") as f:
                    yaml.safe_dump(packet["gold"], f, allow_unicode=True)
                report["golds_archived"].append(gp)
                if not injected:
                    fitness = make_federation_fitness(kit_dir)  # + new gold

        # rebuild loop AFTER fitness may have changed
        loop = ImprovementLoop(data, fitness)

        # 2. shared negative memory: merge this project's dead ends (dedup)
        for rej in packet.get("rejected", []):
            cand = {"kind": rej.get("kind"), "payload": rej.get("payload", {})}
            if cand["kind"] in GENERIC_CANDIDATE_KINDS \
                    and not loop.already_rejected(cand):
                loop._log(loop.rejected_path,
                          {**cand, "reason": f"imported from {project}: "
                                             f"{rej.get('reason', '')}"})
                report["negative_merged"] += 1

        # 3. failure lessons -> bounded negative memory with provenance
        for fail in packet.get("failures", []):
            content = (f"[{project}] {fail.get('level')}: "
                       f"{fail.get('symptom')} — {fail.get('root_cause')}")
            if not any(m["content"] == content
                       for m in memory.recall("negative", content, k=50)):
                memory.add("negative", content, source=f"harvest:{project}",
                           verification="strongly_supported")
                report["lessons"] += 1

        # 4. generic candidates from the instance's PROMOTED improvements,
        #    re-validated on the federation (promotion there ≠ promotion here)
        candidates = []
        for rec in packet.get("promoted", []):
            c = _retarget(rec.get("candidate", {}), kit_dir)
            if c is None:
                report["skipped_domain"] += 1
            else:
                candidates.append(c)
        w = {k: v for k, v in packet.get("weights", {}).items()}
        if w:
            candidates.append({"kind": "retrieval_weights",
                               "payload": {"target_file":
                                           os.path.join(data, "config.json"),
                                           "weights": w},
                               "origin_failure": f"instance weights ({project})"})
        for c in candidates:
            cand = loop.make_candidate(c["kind"], c["payload"],
                                       f"{project}: {c['origin_failure']}")
            rec = loop.run_once(cand, min_gain=min_gain)
            slim = {"project": project, "kind": c["kind"],
                    "decision": rec["decision"], "reason": rec.get("reason"),
                    "dev_before": rec.get("dev_before"),
                    "dev_after": rec.get("dev_after")}
            report["promoted" if rec["decision"] == "promoted"
                   else "rejected"].append(slim)
    return report
