"""Repository archaeology: bootstrap a .dobby/ knowledge base for ANY repo.

This is the generalization mechanism (mission §1: repo knowledge is generated
data, not hard-coded harness). `bootstrap(repo)` scans a repository and emits:
  - kg.json nodes/edges (files, areas, tools, instruction docs, tests, CI)
    with provenance method="scan", confidence="verified" for existence facts
    and "weakly_inferred" for guessed conventions;
  - a skeleton policies.json (universal policies only);
  - a capabilities.json of runnable entry points it found.
Curated, domain-specific knowledge is then layered on top by humans/agents
with its own provenance — bootstrap never overwrites curated files.
"""

from __future__ import annotations

import json
import os
import time

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".dobby", ".idea", ".vscode", "dist", "build"}
LANG_EXT = {".py": "python", ".js": "javascript", ".ts": "typescript",
            ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby",
            ".sh": "shell", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
            ".md": "markdown", ".txt": "text"}
BUILD_FILES = {"Makefile": "make", "package.json": "npm", "pyproject.toml": "python-build",
               "setup.py": "python-build", "Cargo.toml": "cargo", "go.mod": "go",
               "requirements.txt": "pip", "Dockerfile": "docker"}
CI_HINTS = (".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "README.md",
                     "CONTRIBUTING.md")


def _prov(method: str, confidence: str, source: str) -> dict:
    return {"source": source, "method": method,
            "date": time.strftime("%Y-%m-%d"), "confidence": confidence}


def scan_repo(repo: str, max_files: int = 20000) -> dict:
    """Walk the repo and produce an evidence inventory (pure read-only)."""
    inv = {"languages": {}, "build": [], "ci": [], "instructions": [],
           "tests": [], "scripts": [], "skills": [], "rules": [],
           "top_dirs": [], "file_count": 0, "generated_hint": []}
    # In a HOST the installed engine is vendored tooling, not the work. Scanning
    # it buries the project: a fresh install into a folder containing ONE JPEG
    # inventoried 114 files and reported `languages: ['python', 'markdown']`, and
    # the knowledge graph came back describing `area:dobby`, `area:mcp` and
    # `area:tests`. `dobby context "<task>"` then returned `"items": []` — step one
    # of the README walkthrough answering nothing, because everything retrievable
    # was about the harness while the task was about the project.
    #
    # In the KIT those same directories ARE the product, so nothing is excluded
    # there. `core.scan_exclusions` is the single predicate; getting this right in
    # one scanner and wrong in another is how the two disagreed in the first place.
    from . import scan_exclusions

    skips = set(SKIP_DIRS) | set(scan_exclusions(repo))
    inv["excluded_as_harness"] = sorted(skips - set(SKIP_DIRS))
    root_entries = sorted(os.listdir(repo))
    inv["top_dirs"] = [d for d in root_entries
                       if os.path.isdir(os.path.join(repo, d)) and d not in skips]
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in skips]
        rel_dir = os.path.relpath(dirpath, repo)
        # Inventory paths are recorded in POSIX form on every OS. Two reasons:
        # (a) the hint tables below are written with "/" separators, and a
        # Windows "\" path would silently match none of them — CI, rules, and
        # skills would read as absent rather than as undetected; (b) the
        # inventory feeds the knowledge graph, which is committed and compared
        # ACROSS machines by evolve.harvest — the same repo must produce the
        # same node paths on Windows and Linux or federation diffs are noise.
        rel_dir_posix = "" if rel_dir in (".", "") else rel_dir.replace(os.sep, "/")
        dir_parts = rel_dir_posix.split("/") if rel_dir_posix else []
        for fn in filenames:
            if inv["file_count"] >= max_files:
                break
            inv["file_count"] += 1
            rel = f"{rel_dir_posix}/{fn}" if rel_dir_posix else fn
            ext = os.path.splitext(fn)[1].lower()
            if ext in LANG_EXT:
                inv["languages"][LANG_EXT[ext]] = inv["languages"].get(LANG_EXT[ext], 0) + 1
            if fn in BUILD_FILES:
                inv["build"].append({"path": rel, "system": BUILD_FILES[fn]})
            if any(h in rel for h in CI_HINTS):
                inv["ci"].append(rel)
            if fn in INSTRUCTION_FILES and not dir_parts:
                inv["instructions"].append(rel)
            if (fn.startswith("test_") or fn.endswith("_test.py")
                    or "tests" in dir_parts):
                inv["tests"].append(rel)
            if dir_parts[:1] == ["scripts"] and ext == ".py":
                inv["scripts"].append(rel)
            if fn == "SKILL.md":
                inv["skills"].append(rel)
            if ".claude" in dir_parts and "rules" in dir_parts and ext == ".md":
                inv["rules"].append(rel)
            if fn.endswith(".cache") or "generated" in fn.lower():
                inv["generated_hint"].append(rel)
    return inv


def inventory_to_kg(inv: dict, repo_name: str) -> dict:
    """Turn the inventory into ontology-valid nodes/edges."""
    nodes, edges = [], []

    def node(nid, ntype, name, summary, confidence="verified",
             method="scan", path=None, keywords=None, authority=0.6):
        nodes.append({"id": nid, "type": ntype, "name": name, "summary": summary,
                      "path": path, "keywords": keywords or [],
                      "authority": authority,
                      "provenance": _prov(method, confidence, f"bootstrap scan of {repo_name}")})

    node("repo", "RepositoryArea", repo_name,
         f"repository root; {inv['file_count']} files scanned; "
         f"languages: {sorted(inv['languages'], key=inv['languages'].get, reverse=True)[:5]}")
    for d in inv["top_dirs"]:
        node(f"area:{d}", "RepositoryArea", d, f"top-level directory {d}/")
        edges.append({"src": f"area:{d}", "rel": "scoped_to", "dst": "repo",
                      "provenance": _prov("scan", "verified", "bootstrap")})
    for b in inv["build"]:
        node(f"tool:build:{b['system']}", "Tool", f"{b['system']} build",
             f"build system detected at {b['path']}", path=b["path"])
    for s in inv["scripts"]:
        sid = f"tool:script:{os.path.basename(s)}"
        node(sid, "Tool", os.path.basename(s),
             f"executable script at {s}", path=s,
             keywords=[os.path.splitext(os.path.basename(s))[0].replace("_", " ")])
        edges.append({"src": sid, "rel": "scoped_to", "dst": "repo",
                      "provenance": _prov("scan", "verified", "bootstrap")})
    for i in inv["instructions"]:
        node(f"doc:{i}", "Convention", i,
             f"agent/human instruction file at repo root ({i})", path=i,
             authority=0.9)
    for sk in inv["skills"]:
        name = os.path.basename(os.path.dirname(sk))
        node(f"skill:{name}", "Skill", name, f"skill procedure at {sk}", path=sk)
    for r in inv["rules"]:
        name = os.path.splitext(os.path.basename(r))[0]
        node(f"rule:{name}", "Constraint", name, f"scoped rule file at {r}",
             path=r, authority=0.85)
    if inv["tests"]:
        node("area:tests", "Test", "test suite",
             f"{len(inv['tests'])} test-like files found",
             confidence="strongly_supported")
    if inv["generated_hint"]:
        node("risk:generated", "Risk", "generated artifacts present",
             f"{len(inv['generated_hint'])} generated-looking files "
             "(caches etc.) — do not hand-edit; regenerate",
             confidence="weakly_inferred", method="scan")
    return {"nodes": nodes, "edges": edges}


def bootstrap(repo: str, data_dir: str | None = None,
              overwrite: bool = False) -> dict:
    """Create <repo>/.dobby/{knowledge/kg.bootstrap.json, inventory.json}.

    Writes to kg.bootstrap.json — NEVER to the curated kg.json — so human/agent
    curation is never clobbered (anti Stale-Memory Override)."""
    data_dir = data_dir or os.path.join(repo, ".dobby")
    inv = scan_repo(repo)
    kg = inventory_to_kg(inv, os.path.basename(os.path.abspath(repo)))
    os.makedirs(os.path.join(data_dir, "knowledge"), exist_ok=True)
    inv_path = os.path.join(data_dir, "inventory.json")
    kg_path = os.path.join(data_dir, "knowledge", "kg.bootstrap.json")
    if os.path.exists(kg_path) and not overwrite:
        raise FileExistsError(f"{kg_path} exists; pass overwrite=True to refresh")
    with open(inv_path, "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    with open(kg_path, "w", encoding="utf-8") as f:
        json.dump(kg, f, ensure_ascii=False, indent=1)
    return {"inventory": inv_path, "kg": kg_path,
            "nodes": len(kg["nodes"]), "edges": len(kg["edges"]),
            "files_scanned": inv["file_count"]}


def merged_graph(ontology, data_dir: str):
    """Load curated kg.json + kg.bootstrap.json into one graph.

    Curated nodes win on id collision (higher authority by policy)."""
    from .kg import KnowledgeGraph
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for fname in ("kg.bootstrap.json", "kg.json"):   # curated loaded last -> wins
        p = os.path.join(data_dir, "knowledge", fname)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            for n in data.get("nodes", []):
                nodes[n["id"]] = n
            edges.extend(data.get("edges", []))
    g = KnowledgeGraph(ontology)
    for n in nodes.values():
        g.add_node(n)
    # The comment here used to say "drop silently but count" and nothing
    # counted. Measured on a two-node graph with three declared edges, one of
    # them valid: two vanished and `merged_graph` returned a graph that looked
    # complete. A knowledge graph that thins itself between a write and a read,
    # with no reader able to tell, is the shape this repository refuses
    # everywhere else -- a missing check reads exactly like a passing one.
    #
    # The loss rides on the GRAPH rather than in the return type, because eight
    # call sites unpack this and none of them asked for a tuple. `dropped_edges`
    # is a list of `(edge, reason)`, so a caller can name what it lost instead
    # of being told a number.
    dropped: list = []
    for e in edges:
        try:
            g.add_edge(e)
        except Exception as exc:
            dropped.append((e, f"{type(exc).__name__}: {exc}"))
    g.dropped_edges = dropped
    return g
