#!/usr/bin/env python
"""Refresh the shipped self-knowledge graph against the code that exists.

Why this is a script and not a one-off edit
-------------------------------------------
The self-KG is what retrieval answers from before a host project has curated
anything. It had drifted badly: 9 of 40 nodes recorded paths under a `harness/`
directory that no longer exists, 8 were still named for the pre-rename CLI, and
thirteen subsystems added since — providers, swarm, memory tiers, sandbox,
review, mlops, tokens, research, design, search, spend, progress, style — had no
node at all, so `dobby context "compression leakage"` retrieved nothing about
compression or leakage.

Stale paths became actively harmful once `kg._lexical_score` started scoring the
`path` field: a wrong path is now a wrong signal rather than dead weight.

Keeping this as a re-runnable script means the next rename is a command instead
of an archaeology exercise, and every node it writes carries the same provenance
so a reviewer can see which knowledge is machine-refreshed and which is human
curation.

Every path written here is checked against the filesystem before it is stored.
The script refuses to write a node pointing at a file that does not exist —
recording an unverifiable path is what produced the drift in the first place.
"""

from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

KG_PATH = os.path.join(REPO, ".dobby", "knowledge", "kg.json")
ONTOLOGY = os.path.join(REPO, ".dobby", "ontology.json")

PROV = {
    "source": "self-audit refresh (tools/refresh_self_kg.py)",
    "method": "curated",
    "date": time.strftime("%Y-%m-%d"),
    "confidence": "verified",
}

#: Old path -> new path. Applied only when the new path exists.
PATH_MOVES = {
    "harness/cli.py": "dobby/cli.py",
    "harness/bootstrap.py": "dobby/core/bootstrap.py",
    "harness/router.py": "dobby/core/router.py",
    "harness/optimizer.py": "dobby/core/optimizer.py",
    "harness/improve.py": "dobby/core/improve.py",
    "harness/trajectory.py": "dobby/core/trajectory.py",
    "harness/kg.py": "dobby/core/kg.py",
    "harness/memory.py": "dobby/core/memory.py",
    "harness/skills.py": "dobby/core/skills.py",
    "harness/policies.py": "dobby/core/policies.py",
    "harness/evaluator.py": "dobby/core/evaluator.py",
    "harness/security.py": "dobby/core/security.py",
    "harness/evolve.py": "dobby/core/evolve.py",
    "harness/friction.py": "dobby/core/friction.py",
    "mcp/harness_mcp_server.py": "mcp/dobby_mcp_server.py",
    "docs/HARNESS_V2_ARCHITECTURE.md": "docs/RESEARCH_EVIDENCE_MATRIX.md",
}

#: (id, type, name, summary, path, keywords, authority)
#:
#: Summaries are written for RETRIEVAL, not for documentation: they carry the
#: words a user would actually type when they want this thing. A summary that
#: reads well and shares no vocabulary with the question is invisible.
NEW_NODES = [
    ("tool:cli-doctor", "Tool", "dobby doctor",
     "what works on this machine and what does not, with the fix for each; "
     "blocking failures set the exit code, advisory gaps do not",
     "dobby/cli.py", ["doctor", "diagnose", "health", "broken", "check",
                      "진단", "점검"], 0.85),
    ("tool:cli-fleet", "Tool", "dobby fleet",
     "which agent providers are available, absent, or blocked here, and which "
     "roles can be filled; --probe makes one real call each",
     "dobby/providers/detect.py",
     ["fleet", "provider", "available", "claude", "codex", "gemini", "agy",
      "프로바이더"], 0.8),
    ("tool:cli-panel", "Tool", "dobby panel",
     "decorrelated multi-agent round: isolated generation, assigned lenses, and "
     "a measured diversity verdict so agreement is not mistaken for consensus",
     "dobby/cli.py", ["panel", "multi-agent", "fan-out", "swarm", "parallel",
                      "패널", "멀티에이전트"], 0.85),
    ("tool:cli-sandbox", "Tool", "dobby sandbox",
     "run a command with its output withheld from context and held on disk; "
     "extract only the lines you need with a pattern, head, or tail",
     "dobby/sandbox.py", ["sandbox", "output", "capture", "extract", "grep",
                          "withhold", "샌드박스"], 0.8),
    ("tool:cli-review", "Tool", "dobby review",
     "perspective-based review plan over ISO 25010 characteristics, with the "
     "uncovered perspectives reported alongside the verdict",
     "dobby/review.py", ["review", "code review", "inspect", "perspective",
                         "defect", "리뷰", "코드리뷰"], 0.85),
    ("tool:cli-ml", "Tool", "dobby ml",
     "leakage, reproducibility, statistical rigor, and interpretation gates for "
     "a machine-learning result; confirmed leakage invalidates everything after",
     "dobby/mlops.py", ["ml", "leakage", "experiment", "reproducibility",
                        "holdout", "baseline", "머신러닝", "누수"], 0.85),
    ("tool:cli-spend", "Tool", "dobby spend",
     "agent time per provider, with wall time and agent time reported "
     "separately; --line renders a one-line host status bar",
     "dobby/spend.py", ["spend", "time", "cost", "parallelism", "statusline",
                        "시간", "비용"], 0.75),
    ("component:providers", "Component", "provider fleet",
     "drives claude, codex, gemini, agy, qwen, ollama and OpenAI-compatible "
     "APIs as child processes; availability is measured, never assumed",
     "dobby/providers/catalog.py",
     ["provider", "fleet", "cli", "adapter", "role", "routing"], 0.8),
    ("component:fanout", "Component", "parallel fan-out",
     "bounded-concurrency agent execution with git worktree isolation for "
     "providers that write files; one failure never loses the round",
     "dobby/providers/fanout.py",
     ["fanout", "parallel", "concurrency", "worktree", "isolation"], 0.8),
    ("component:diversity", "Component", "diversity measurement",
     "effective_n answers how many independent opinions a panel actually "
     "bought; six identical answers report as one",
     "dobby/swarm/diversity.py",
     ["diversity", "collapse", "coupling", "consensus", "effective",
      "다양성"], 0.8),
    ("component:protocols", "Component", "ideation protocols",
     "NGT, Double Diamond, Six Thinking Hats, dialectic and adversarial "
     "protocols; each isolates generation and assigns a distinct lens",
     "dobby/swarm/protocols.py",
     ["protocol", "ideation", "brainstorm", "scamper", "ngt", "dialectic",
      "아이디어"], 0.8),
    ("component:grounding", "Component", "grounding gate",
     "no ideation before prior art; every idea needs an evidence id that "
     "resolves and a falsifiable test, or it is rejected before synthesis",
     "dobby/swarm/grounding.py",
     ["grounding", "evidence", "prior art", "falsifiable", "hallucination",
      "근거"], 0.85),
    ("component:memory-tiers", "Component", "six-tier memory",
     "nation, mountain, forest, tree, branch, leaf: five hops to any leaf, each "
     "tier remembering by a different mechanism",
     "dobby/memory/tiers.py",
     ["memory", "tier", "hierarchical", "routing", "promotion", "기억",
      "계층"], 0.85),
    ("component:memory-gates", "Component", "memory gates and compression",
     "forget, admit and expose decisions per item, and a leakage audit that "
     "refuses a summary which drops a file path, a number, or a negation",
     "dobby/memory/gates.py",
     ["compression", "leakage", "gate", "forget", "promote", "summary",
      "압축", "누수"], 0.85),
    ("component:specialize", "Component", "specialization gate",
     "a domain gain never licenses a loss of general competence: the project "
     "gold must improve and the generic gold must not regress on any case",
     "dobby/specialize.py",
     ["specialize", "domain", "expert", "mastery", "generalist", "전문화"],
     0.8),
    ("component:tokens", "Component", "token accounting",
     "per-command output condensers, priority-tiered snapshots inside a hard "
     "byte budget, and blast radius; savings are estimates, labelled as such",
     "dobby/tokens.py",
     ["token", "compress", "condense", "budget", "snapshot", "blast radius",
      "토큰"], 0.8),
    ("component:research", "Component", "research and claim verification",
     "search plans that always include a refutation query, claim strength "
     "driving the evidence bar, and citation resolution at three severities",
     "dobby/research.py",
     ["research", "claim", "citation", "paper", "verify", "evidence",
      "논문", "검증"], 0.8),
    ("component:search", "Component", "solution-tree search",
     "draft, debug, improve over candidate solutions with a bounded debug "
     "depth; refuses to report a selection score as performance without a "
     "holdout",
     "dobby/search.py",
     ["search", "tree", "solution", "draft", "debug", "improve", "탐색"], 0.8),
    ("component:prompt", "Component", "prompt compiler",
     "turns a casual request into an executable prompt and names what is "
     "unspecified instead of guessing it; returns one question, not five",
     "dobby/prompt.py",
     ["prompt", "compile", "ambiguous", "clarify", "question", "프롬프트"],
     0.8),
    ("component:style", "Component", "prose style detector",
     "the generated-text signature in English and Korean: uniform sentence "
     "length, comma density, stacked hedges, connective commas",
     "dobby/style.py",
     ["style", "prose", "writing", "ai text", "humanize", "문체", "글쓰기"],
     0.75),
    ("component:jsonl", "Component", "atomic JSONL append",
     "every ledger writes through one locked, unbuffered append; the obvious "
     "open-and-write idiom lost and corrupted records under concurrency",
     "dobby/core/jsonl.py",
     ["jsonl", "ledger", "append", "concurrency", "atomic", "corruption"],
     0.8),
    ("component:design", "Component", "DESIGN.md validation",
     "token frontmatter, an explicit aesthetic, WCAG contrast, and the check "
     "that matters most: tokens declared without the prose saying when to use "
     "them",
     "dobby/design.py",
     ["design", "tokens", "aesthetic", "contrast", "typography", "디자인"],
     0.75),
    ("component:progress", "Component", "progress and ETA",
     "refuses to estimate below three samples, reports a range from observed "
     "spread, and extrapolates parallel work on waves rather than items",
     "dobby/progress.py",
     ["progress", "eta", "estimate", "remaining", "진행률"], 0.7),
    ("risk:diversity-collapse", "Risk", "Diversity Collapse",
     "a panel converging before the minority view is stated, so the "
     "orchestrator reads collapse as consensus and therefore as confidence",
     "dobby/swarm/diversity.py",
     ["collapse", "consensus", "coupling", "agreement", "panel"], 0.85),
    ("risk:silent-leakage", "Risk", "Silent Data Leakage",
     "an ML result whose split is clean and whose answer is publicly available "
     "elsewhere, so every pipeline check passes and the score means nothing",
     "dobby/mlops.py",
     ["leakage", "contamination", "holdout", "external", "누수"], 0.85),
    ("risk:existence-not-measurement", "Risk", "Existence Is Not Measurement",
     "trusting that a declared thing works: a name that resolves, a file that "
     "exists, a control that is defined but never called",
     "docs/THREAT_MODEL.md",
     ["existence", "assumption", "declared", "verify", "measure"], 0.85),
]

#: (src, rel, dst)
NEW_EDGES = [
    ("tool:cli-panel", "invokes", "component:fanout"),
    ("tool:cli-panel", "requires", "component:protocols"),
    ("component:fanout", "requires", "component:providers"),
    ("tool:cli-fleet", "invokes", "component:providers"),
    ("component:protocols", "prevents", "risk:diversity-collapse"),
    ("component:diversity", "evaluated_by", "risk:diversity-collapse"),
    ("component:grounding", "requires", "component:research"),
    ("component:memory-gates", "scoped_to", "component:memory-tiers"),
    ("tool:cli-sandbox", "produces", "component:tokens"),
    ("tool:cli-ml", "prevents", "risk:silent-leakage"),
    ("tool:cli-spend", "consumes", "component:progress"),
    ("component:specialize", "constrained_by", "component:search"),
    ("tool:cli-doctor", "prevents", "risk:existence-not-measurement"),
    ("component:jsonl", "available_to", "component:memory-tiers"),
]


def main() -> int:
    from dobby.core.kg import KnowledgeGraph, Ontology

    with open(KG_PATH, encoding="utf-8") as f:
        kg = json.load(f)
    by_id = {n["id"]: n for n in kg["nodes"]}
    changes = {"paths": 0, "names": 0, "added_nodes": 0, "added_edges": 0,
               "refused": []}

    # 1. repoint stale paths, but only at files that actually exist
    for node in kg["nodes"]:
        old = node.get("path")
        if old in PATH_MOVES:
            new = PATH_MOVES[old]
            if os.path.exists(os.path.join(REPO, new)):
                node["path"] = new
                changes["paths"] += 1
            else:
                changes["refused"].append(f"{node['id']}: {new} does not exist")
        if node.get("name", "").startswith("harness "):
            node["name"] = "dobby " + node["name"][len("harness "):]
            changes["names"] += 1

    # 2. add the subsystems that had no node at all
    for nid, ntype, name, summary, path, keywords, authority in NEW_NODES:
        if nid in by_id:
            continue
        if path and not os.path.exists(os.path.join(REPO, path)):
            # Recording an unverifiable path is what produced the drift.
            changes["refused"].append(f"{nid}: {path} does not exist")
            continue
        node = {"id": nid, "type": ntype, "name": name, "summary": summary,
                "keywords": keywords, "path": path, "authority": authority,
                "provenance": dict(PROV), "created": PROV["date"]}
        kg["nodes"].append(node)
        by_id[nid] = node
        changes["added_nodes"] += 1

    existing = {(e["src"], e["rel"], e["dst"]) for e in kg["edges"]}
    for src, rel, dst in NEW_EDGES:
        if (src, rel, dst) in existing or src not in by_id or dst not in by_id:
            continue
        kg["edges"].append({"src": src, "rel": rel, "dst": dst,
                            "provenance": dict(PROV)})
        changes["added_edges"] += 1

    # 3. validate through the ontology BEFORE writing. A KG that fails
    #    validation is worse than a stale one: nothing loads at all.
    onto = Ontology.load(ONTOLOGY)
    KnowledgeGraph(onto, kg["nodes"], kg["edges"])

    tmp = KG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kg, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, KG_PATH)

    print(json.dumps({**changes, "total_nodes": len(kg["nodes"]),
                      "total_edges": len(kg["edges"])},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
