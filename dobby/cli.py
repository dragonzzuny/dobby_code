"""dobby CLI — one entry point for the whole harness.

    dobby init --scan <host-root>       instantiate the harness for a project
    dobby doctor                        what works here, what does not, and why
    dobby context "task"                the routed briefing for a task
    dobby route "task"                  agency level, policies, budgets
    dobby slice --scenario SELF-CHECK   end-to-end loop on a fixture
    dobby optimize                      offline retrieval-weight search
    dobby improve-auto                  propose+validate one improvement
    dobby handoff-latest                newest session handoff
    dobby export-experience / harvest   cross-project evolution
    dobby friction-report               context-bloat / retry-loop signals

    dobby fleet [--probe]               provider availability (and live probe)
    dobby panel "task" [--size N]       decorrelated multi-agent round
    dobby memory <stats|route|expire|integrity>
    dobby compress --file F             compression with a leakage audit
    dobby specialize [--status]         mastery level and its evidence
    dobby research plan "need"          decomposed search plan
    dobby design validate               DESIGN.md token check

Every command prints JSON on stdout so the output is consumable by another
process without parsing prose. UTF-8 is pinned first (see core/platform.py):
without it, any non-ASCII knowledge-graph summary crashes the process on a
non-UTF-8 Windows locale.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .core.platform import describe_platform, force_utf8_io


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def _repo(args) -> str:
    return os.path.abspath(getattr(args, "repo", ".") or ".")


def _data(args) -> str:
    return os.path.join(_repo(args), ".dobby")


def _config(args) -> dict:
    path = os.path.join(_data(args), "config.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _allow_network(args) -> bool:
    if getattr(args, "allow_network", False):
        return True
    return bool((_config(args).get("providers") or {}).get("allow_network"))


# ---------------------------------------------------------------- core ----
def _load_stack(repo: str):
    from .core.bootstrap import merged_graph
    from .core.kg import Ontology
    from .core.policies import PolicyBook
    from .core.skills import SkillRegistry
    data = os.path.join(repo, ".dobby")
    onto = Ontology.load(os.path.join(data, "ontology.json"))
    kg = merged_graph(onto, data)
    policies = PolicyBook(os.path.join(data, "policies", "policies.json"))
    registry = SkillRegistry(os.path.join(data, "registry", "skills.json"))
    with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    return data, kg, policies, registry, config


def cmd_init(args):
    from .core.bootstrap import bootstrap
    kit = _repo(args)
    scan_root = os.path.abspath(args.scan) if args.scan else kit
    _out(bootstrap(scan_root, data_dir=os.path.join(kit, ".dobby"),
                   overwrite=args.overwrite))


def cmd_route(args):
    from .core.router import Router
    _, kg, policies, registry, config = _load_stack(_repo(args))
    _out(Router(policies, registry, kg, config).route(args.task).to_dict())


def cmd_context(args):
    _, kg, _, _, config = _load_stack(_repo(args))
    _out(kg.context_pack(args.task,
                         weights=config.get("retrieval_weights"),
                         k=config.get("context_k", 8)))


def cmd_slice(args):
    from .core.evaluator import Evaluator
    from .core.platform import child_env, resolve_command
    from .core.router import BudgetMeter, Router
    from .core.trajectory import Trajectory
    import subprocess

    repo = _repo(args)
    data, kg, policies, registry, config = _load_stack(repo)
    plans_path = os.path.join(data, "slice_plans.json")
    if not os.path.exists(plans_path):
        sys.exit(f"no {plans_path}")
    with open(plans_path, encoding="utf-8") as f:
        plans = json.load(f)["plans"]
    if args.scenario not in plans:
        sys.exit(f"no executor for {args.scenario}; available: {list(plans)}")
    spec = plans[args.scenario]
    task = spec["task"]

    router = Router(policies, registry, kg, config)
    plan = router.route(task)
    traj = Trajectory(data, task)
    traj.append("route", {"level": plan.level, "policies": plan.policies})

    meter = BudgetMeter(plan.budgets)
    if not meter.charge("tool_calls"):
        sys.exit("budget exhausted before execution")
    command = resolve_command(spec["command"])
    proc = subprocess.run(command, shell=True, cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=child_env(), timeout=900)
    ev = Evaluator(os.path.join(repo, spec["criteria"]), repo, config=config)
    evaluation = ev.evaluate()
    handoff = traj.handoff(
        done=[f"executed {spec['capability']} (exit {proc.returncode})",
              f"evaluator verdict: {evaluation['verdict']}"],
        remaining=[f"model-judgment criteria not runnable here "
                   f"({evaluation['not_evaluated']})"],
        decisions=[{"what": f"routed to level {plan.level}",
                    "why": "; ".join(plan.justification),
                    "rejected": "higher agency levels (unjustified cost)"}],
        evidence=[spec["criteria"], os.path.relpath(traj.path, repo)],
        next_steps=["run with a real provider panel: dobby panel"])
    _out({
        "scenario": args.scenario,
        "route": {"level": plan.level, "tier": plan.model_tier,
                  "policies": plan.policies, "budgets": plan.budgets},
        "execute": {"command": command, "exit_code": proc.returncode,
                    "stdout_tail": (proc.stdout or "")[-600:]},
        "evaluate": {"verdict": evaluation["verdict"],
                     "criteria_hash": evaluation["criteria_hash"],
                     "integrity": evaluation["criteria_integrity"],
                     "not_evaluated": evaluation["not_evaluated"],
                     "records": [{"criterion": r["criterion"],
                                  "passed": r["passed"]}
                                 for r in evaluation["records"]]},
        "trajectory": os.path.relpath(traj.path, repo),
        "handoff": os.path.relpath(handoff, repo),
    })


def _fitness(repo: str):
    import yaml
    from .core.optimizer import RetrievalFitness
    gold_path = os.path.join(repo, "evals", "retrieval_gold.yaml")
    if not os.path.exists(gold_path):
        sys.exit(f"no {gold_path} — author gold labels first (author-evals skill)")
    with open(gold_path, encoding="utf-8") as f:
        gold = yaml.safe_load(f)

    def fitness(split: str) -> dict:
        _, kg, _, _, config = _load_stack(repo)
        cfg = dict(config.get("retrieval_weights", {}))
        cfg["context_k"] = config.get("context_k", 8)
        return RetrievalFitness(kg, gold)(cfg, split=split)
    return fitness, gold


def cmd_optimize(args):
    from .core.optimizer import RetrievalFitness, compare
    repo = _repo(args)
    _, kg, _, _, config = _load_stack(repo)
    _, gold = _fitness(repo)
    fit = RetrievalFitness(kg, gold)
    res = compare(lambda x: fit(x, split="dev"), seeds=args.seeds,
                  pop_size=args.pop, iters=args.iters)
    default = dict(config.get("retrieval_weights", {}))
    default["context_k"] = config.get("context_k", 8)
    res["default_config"] = {s: round(fit(default, s)["score"], 4)
                             for s in ("dev", "val", "holdout")}
    for run in res["runs"]:
        for m in ("goa", "random", "hillclimb"):
            best = run[m]["best_config"]
            run[m]["val"] = round(fit(best, "val")["score"], 4)
            run[m]["holdout"] = round(fit(best, "holdout")["score"], 4)
    _out(res)


def cmd_improve_auto(args):
    from .core.improve import ImprovementLoop
    from .core.kg import tokenize
    repo = _repo(args)
    fitness, gold = _fitness(repo)
    base = fitness("dev")
    worst = min(base["per_case"], key=base["per_case"].get)
    case = next(c for c in gold["dev"] if c["id"] == worst)
    data, kg, _, _, config = _load_stack(repo)
    pack = kg.context_pack(case["task"],
                           weights=dict(config.get("retrieval_weights", {})),
                           k=config.get("context_k", 8))
    missed = [n for n in case["required_nodes"]
              if n not in {i["id"] for i in pack["items"]}]
    if not missed:
        _out({"decision": "no_candidate",
              "reason": f"worst dev case {worst} has no missed nodes "
                        f"(score {base['per_case'][worst]})"})
        return
    keywords = [t for t in tokenize(case["task"]) if len(t) > 3][:4]
    loop = ImprovementLoop(data, fitness)
    cand = loop.make_candidate(
        "kg_keyword_add",
        {"target_file": os.path.join(data, "knowledge", "kg.json"),
         "node_id": missed[0], "keywords": keywords},
        origin_failure=f"retrieval miss: {worst} missing {missed[0]}")
    rec = loop.run_once(cand)
    _out({"decision": rec["decision"], "node": missed[0],
          "keywords": keywords, "reason": rec.get("reason"),
          "dev_before": rec.get("dev_before"), "dev_after": rec.get("dev_after"),
          "val_before": rec.get("val_before"), "val_after": rec.get("val_after"),
          "holdout_after": rec.get("holdout_after")})


def cmd_handoff_latest(args):
    from .core.trajectory import Trajectory
    _out({"handoff": Trajectory.latest_handoff(_data(args))})


def cmd_export_experience(args):
    from .core.evolve import export_experience
    _out({"packet": export_experience(_repo(args), out_path=args.out,
                                      include_gold=not args.no_gold)})


def cmd_harvest(args):
    from .core.evolve import harvest
    _out(harvest(_repo(args), args.packets, min_gain=args.min_gain))


def cmd_friction(args):
    from .core.friction import friction_report
    _out(friction_report(_data(args)))


# ------------------------------------------------------------- doctor ----
def cmd_doctor(args):
    """Everything this machine can and cannot do, with the reason for each.

    Deliberately reports NEGATIVES prominently: a doctor that only lists what
    works leaves the user to discover the gaps during a real task.
    """
    from .providers import report as fleet_report
    repo = _repo(args)
    data = _data(args)
    checks = []

    def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    check("data_dir", os.path.isdir(data), data,
          "run: dobby init --scan .")
    for rel in ("ontology.json", "config.json",
                os.path.join("knowledge", "kg.json"),
                os.path.join("policies", "policies.json"),
                os.path.join("registry", "skills.json")):
        p = os.path.join(data, rel)
        check(f"data:{rel}", os.path.exists(p), p, "restore from distribution")
    boot = os.path.join(data, "knowledge", "kg.bootstrap.json")
    check("bootstrapped", os.path.exists(boot), boot,
          "run: dobby init --scan <host-root> (the project is not instantiated)")
    gold = os.path.join(repo, "evals", "retrieval_gold.yaml")
    check("retrieval_gold", os.path.exists(gold), gold,
          "author project gold with the author-evals skill")

    try:
        import yaml  # noqa: F401
        check("pyyaml", True, "importable")
    except ImportError:
        check("pyyaml", False, "not importable", "pip install PyYAML")

    fleet = fleet_report(allow_network=_allow_network(args))
    check("providers", fleet["usable_count"] > 0,
          f"{fleet['usable_count']} usable: {fleet['usable_ids']}",
          "install at least one agent CLI (claude / codex / gemini / agy)")
    check("multi_agent", fleet["multi_agent_ready"],
          f"panel size {fleet['max_panel_size']} "
          f"({'>=2 providers' if fleet['multi_agent_ready'] else 'need >=2'})",
          "install a second provider so a panel has independent members")

    failed = [c for c in checks if not c["ok"]]
    _out({
        "platform": describe_platform(),
        "version": __import__("dobby").__version__,
        "repo": repo,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "fleet": fleet,
        "verdict": ("all checks pass" if not failed
                    else f"{len(failed)} check(s) failed: "
                         + ", ".join(c["check"] for c in failed)),
    })


# -------------------------------------------------------------- fleet ----
def cmd_fleet(args):
    from .providers import probe, report as fleet_report
    allow = _allow_network(args)
    out = fleet_report(allow_network=allow)
    if args.probe:
        # Real calls: only on explicit request, because each one costs money.
        out["probes"] = {}
        for pid in out["usable_ids"]:
            res = probe(pid, cwd=_repo(args), timeout_s=args.timeout)
            out["probes"][pid] = {
                "ok": res.ok, "duration_s": res.duration_s,
                "matched_expected": res.meta.get("probe_matched"),
                "error": res.error,
                "text_head": res.text[:120] if res.ok else None,
            }
        out["probe_note"] = ("`matched_expected: false` with `ok: true` means the "
                            "invocation path works but the model ignored the "
                            "instruction — the provider is usable")
    _out(out)


def cmd_panel(args):
    """Run one decorrelated multi-agent round and report the diversity achieved."""
    from .providers import AgentTask, resolve_panel, run_round
    from .swarm import analyze, build_prompts, get, recommend

    allow = _allow_network(args)
    members = resolve_panel(args.role, args.size, allow_network=allow)
    if not members:
        _out({"error": f"no usable provider for role {args.role!r}",
              "fix": "install an agent CLI, or run: dobby doctor"})
        return
    protocol = get(args.protocol) if args.protocol else get(
        recommend(args.task, len(members)))

    context = ""
    if args.with_context:
        _, kg, _, _, config = _load_stack(_repo(args))
        pack = kg.context_pack(args.task,
                               weights=config.get("retrieval_weights"),
                               k=config.get("context_k", 8))
        context = "\n".join(f"- [{i['id']}] {i['summary']}"
                            for i in pack["items"])

    prompts = build_prompts(protocol, args.task, len(members),
                            shared_context=context)
    tasks = [AgentTask(provider_id=members[p["index"] % len(members)],
                       prompt=p["prompt"],
                       label=f"{members[p['index'] % len(members)]}:{p['lens'] or protocol.id}")
             for p in prompts]

    if args.dry_run:
        _out({"protocol": protocol.id, "protocol_display": protocol.display,
              "panel": members, "assignment_note": protocol.assignment_note(len(members)),
              "tasks": [{"provider": t.provider_id, "label": t.label,
                         "prompt_chars": len(t.prompt),
                         "prompt_head": t.prompt[:400]} for t in tasks],
              "note": "dry run: no provider was invoked"})
        return

    round_ = run_round(tasks, cwd=_repo(args),
                       max_concurrency=args.concurrency)
    diversity = analyze(round_.texts, round_.labels) if round_.texts else None
    _out({
        "protocol": protocol.id,
        "protocol_display": protocol.display,
        "panel": members,
        "assignment_note": protocol.assignment_note(len(members)),
        "round": round_.summary(),
        "answers": [{"provider": r.provider, "label": r.meta.get("label"),
                     "chars": len(r.text), "truncated": r.truncated,
                     "text": r.text} for r in round_.ok_results],
        "diversity": diversity.to_dict() if diversity else None,
        "next": ("feed the answers through the grounding gate before synthesis: "
                 "ungrounded ideation is novel-sounding and low-feasibility"),
    })


# ------------------------------------------------------------- memory ----
def _memory(args):
    from .memory import HierarchicalMemory
    return HierarchicalMemory(os.path.join(_data(args), "memory"))


def cmd_memory(args):
    mem = _memory(args)
    if args.action == "stats":
        _out(mem.stats())
    elif args.action == "integrity":
        _out(mem.integrity())
    elif args.action == "expire":
        _out(mem.expire())
    elif args.action == "route":
        if not args.query:
            sys.exit("memory route needs --query")
        _out(mem.route(args.query, beam=args.beam, per_tier=args.per_tier))
    else:
        sys.exit(f"unknown memory action {args.action!r}")


def cmd_compress(args):
    """Compress a file's text and REFUSE the result if leakage is too high."""
    from .memory import CompressionGuideline, MAX_LEAKAGE, leakage
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        original = f.read()
    guideline = CompressionGuideline.load(
        os.path.join(_data(args), "compression_guideline.json"))
    if args.compressed:
        with open(os.path.abspath(args.compressed), encoding="utf-8",
                  errors="replace") as f:
            compressed = f.read()
        audit = leakage(original, compressed)
        audit["accepted"] = audit["leakage_rate"] <= MAX_LEAKAGE
        _out({"source": path, "guideline_version": guideline.version,
              "audit": audit})
        return
    # No compressed candidate supplied: emit the guideline the compressor must
    # follow. Producing a compression here would require a model, and inventing
    # a heuristic summary would be the lossy step this command exists to police.
    _out({
        "source": path,
        "source_chars": len(original),
        "guideline_version": guideline.version,
        "guideline": guideline.render(),
        "max_leakage": MAX_LEAKAGE,
        "next": ("compress with a provider using this guideline, then re-run "
                 "with --compressed <file> to get the leakage audit"),
    })


# --------------------------------------------------------- specialize ----
def cmd_specialize(args):
    from .specialize import MasteryEvidence, SpecializationLedger
    ledger = SpecializationLedger(
        os.path.join(_data(args), "specialization.json"))
    if args.recount:
        # Counts come from the knowledge graph and the ledger itself: mastery is
        # derived from evidence, never asserted.
        repo = _repo(args)
        _, kg, _, _, _ = _load_stack(repo)
        nodes = list(getattr(kg, "nodes", {}).values()) if hasattr(kg, "nodes") else []
        verified = sum(1 for n in nodes
                       if (n.get("provenance") or {}).get("confidence") == "verified")
        steps = ledger.data.get("steps", [])
        gold_path = os.path.join(repo, "evals", "retrieval_gold.yaml")
        cases, score = 0, 0.0
        if os.path.exists(gold_path):
            fitness, gold = _fitness(repo)
            cases = sum(len(gold.get(s) or []) for s in ("dev", "val", "holdout"))
            score = round(fitness("dev")["score"], 4)
        ev = MasteryEvidence(
            verified_domain_nodes=verified,
            unverified_domain_nodes=len(nodes) - verified,
            promoted_candidates=sum(1 for s in steps if s["accepted"]),
            rejected_candidates=sum(1 for s in steps if not s["accepted"]),
            domain_gold_cases=cases, domain_gold_score=score)
        ledger.set_evidence(ev)
        ledger.save()
    _out(ledger.summary())


# ----------------------------------------------------------- research ----
def cmd_research(args):
    from .research import (extract_claims, plan_queries, reproducibility_report,
                           verify_citations)
    if args.action == "plan":
        _out(plan_queries(args.need, year_hint=args.year).to_dict())
    elif args.action == "claims":
        with open(os.path.abspath(args.file), encoding="utf-8",
                  errors="replace") as f:
            text = f.read()
        claims = extract_claims(text)
        _out({
            "source": args.file,
            "claims": [{"text": c.text[:220], "strength": c.strength,
                        "locator": c.source_locator,
                        "required_artifacts": list(c.required_artifacts())}
                       for c in claims],
            "by_strength": {s: sum(1 for c in claims if c.strength == s)
                            for s in {c.strength for c in claims}},
            "reproducibility": reproducibility_report(claims),
        })
    elif args.action == "citations":
        with open(os.path.abspath(args.file), encoding="utf-8",
                  errors="replace") as f:
            refs = [line.strip() for line in f if line.strip()]
        corpus = []
        if args.corpus:
            with open(os.path.abspath(args.corpus), encoding="utf-8") as f:
                corpus = json.load(f)
        _out(verify_citations(refs, corpus))
    else:
        sys.exit(f"unknown research action {args.action!r}")


# ------------------------------------------------------------- design ----
def cmd_design(args):
    from .design import validate_design_md
    path = os.path.join(_repo(args), "DESIGN.md")
    if args.file:
        path = os.path.abspath(args.file)
    _out(validate_design_md(path))


# ------------------------------------------------------------- review ----
def cmd_review(args):
    """Produce a perspective-based review plan for a change."""
    from .review import review_plan
    risk = [r.strip() for r in (args.risk or "").split(",") if r.strip()]
    plan = review_plan(args.reviewers, risk_areas=risk)
    if args.changed:
        # Blast radius turns "review the diff" into "review the diff and what
        # depends on it" — the part a diff-only review structurally cannot see.
        from .tokens import blast_radius
        _, kg, _, _, _ = _load_stack(_repo(args))
        edges = [(e.get("src") or e.get("from"), e.get("dst") or e.get("to"))
                 for e in getattr(kg, "edges", [])
                 if isinstance(e, dict)]
        edges = [(a, b) for a, b in edges if a and b]
        changed = [c.strip() for c in args.changed.split(",") if c.strip()]
        plan["blast_radius"] = blast_radius(edges, changed,
                                           max_hops=args.hops)
    _out(plan)


def cmd_tokens(args):
    """Condense a captured command output, or show the snapshot tier policy."""
    from .tokens import SNAPSHOT_TIERS, condense, estimate_savings
    if args.action == "policy":
        _out({"snapshot_tiers": {f"P{t}": list(k)
                                 for t, k in SNAPSHOT_TIERS.items()},
              "handlers": sorted(__import__("dobby.tokens",
                                            fromlist=["HANDLERS"]).HANDLERS)})
        return
    if not args.file:
        sys.exit("tokens condense needs --file <captured output>")
    with open(os.path.abspath(args.file), encoding="utf-8",
              errors="replace") as f:
        output = f.read()
    result = condense(args.command or "", output, exit_code=args.exit_code,
                      data_dir=_data(args))
    _out({"condensed": result.to_dict(),
          "savings": estimate_savings([result]),
          "text": result.text if args.show else None})


def cmd_ml(args):
    """Run the ML gates over a JSON-described experiment."""
    from .mlops import ExperimentSetup, ml_gate
    with open(os.path.abspath(args.file), encoding="utf-8") as f:
        spec = json.load(f)
    setup_fields = {f.name for f in __import__(
        "dataclasses").fields(ExperimentSetup)}
    setup_kwargs = {k: (tuple(v) if isinstance(v, list) else v)
                    for k, v in spec.items() if k in setup_fields}
    gate_kwargs = {k: v for k, v in spec.items() if k not in setup_fields}
    _out(ml_gate(ExperimentSetup(**setup_kwargs), **gate_kwargs))


def cmd_pipeline(args):
    """Suggest and validate an inference-time layer stack for a call budget."""
    from .search import suggest_pipeline, validate_pipeline
    out = suggest_pipeline(budget_calls=args.budget, task_kind=args.kind)
    _out({
        "task_kind": out["task_kind"],
        "rationale": out["rationale"],
        "layers": [{"kind": l.kind, "n": l.n, "keep": l.keep} for l in out["layers"]],
        "validation": out["validation"],
    })


# --------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dobby",
                                 description="portable agent harness")
    ap.add_argument("--repo", default=".")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=argparse.SUPPRESS)
    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--allow-network", action="store_true",
                     help="permit api-kind providers (adds a network egress "
                          "path; see docs/THREAT_MODEL.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", parents=[common])
    p.add_argument("--scan", default=None)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", parents=[common, net])
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("route", parents=[common]); p.add_argument("task")
    p.set_defaults(fn=cmd_route)
    p = sub.add_parser("context", parents=[common]); p.add_argument("task")
    p.set_defaults(fn=cmd_context)

    p = sub.add_parser("slice", parents=[common])
    p.add_argument("--scenario", required=True)
    p.set_defaults(fn=cmd_slice)

    p = sub.add_parser("optimize", parents=[common])
    p.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4])
    p.add_argument("--iters", type=int, default=30)
    p.add_argument("--pop", type=int, default=12)
    p.set_defaults(fn=cmd_optimize)

    p = sub.add_parser("improve-auto", parents=[common])
    p.set_defaults(fn=cmd_improve_auto)
    p = sub.add_parser("handoff-latest", parents=[common])
    p.set_defaults(fn=cmd_handoff_latest)

    p = sub.add_parser("export-experience", parents=[common])
    p.add_argument("--out", default=None)
    p.add_argument("--no-gold", action="store_true")
    p.set_defaults(fn=cmd_export_experience)
    p = sub.add_parser("harvest", parents=[common])
    p.add_argument("packets", nargs="+")
    p.add_argument("--min-gain", type=float, default=0.005)
    p.set_defaults(fn=cmd_harvest)
    p = sub.add_parser("friction-report", parents=[common])
    p.set_defaults(fn=cmd_friction)

    p = sub.add_parser("fleet", parents=[common, net])
    p.add_argument("--probe", action="store_true",
                   help="make one real cheap call per provider (costs money)")
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(fn=cmd_fleet)

    p = sub.add_parser("panel", parents=[common, net])
    p.add_argument("task")
    p.add_argument("--size", type=int, default=3)
    p.add_argument("--role", default="draft")
    p.add_argument("--protocol", default=None,
                   help="ngt | double_diamond | six_hats | dialectic | adversarial")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--with-context", action="store_true",
                   help="include the routed knowledge pack in each prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="show the assignments and prompts without invoking")
    p.set_defaults(fn=cmd_panel)

    p = sub.add_parser("memory", parents=[common])
    p.add_argument("action", choices=["stats", "route", "expire", "integrity"])
    p.add_argument("--query", default=None)
    p.add_argument("--beam", type=int, default=2)
    p.add_argument("--per-tier", type=int, default=3)
    p.set_defaults(fn=cmd_memory)

    p = sub.add_parser("compress", parents=[common])
    p.add_argument("--file", required=True)
    p.add_argument("--compressed", default=None)
    p.set_defaults(fn=cmd_compress)

    p = sub.add_parser("specialize", parents=[common])
    p.add_argument("--recount", action="store_true",
                   help="recompute mastery evidence from the knowledge graph")
    p.set_defaults(fn=cmd_specialize)

    p = sub.add_parser("research", parents=[common])
    p.add_argument("action", choices=["plan", "claims", "citations"])
    p.add_argument("need", nargs="?", default="")
    p.add_argument("--file", default=None)
    p.add_argument("--corpus", default=None)
    p.add_argument("--year", default=None)
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("design", parents=[common])
    p.add_argument("--file", default=None)
    p.set_defaults(fn=cmd_design)

    p = sub.add_parser("review", parents=[common],
                       help="perspective-based review plan (PBR)")
    p.add_argument("--reviewers", type=int, default=3)
    p.add_argument("--risk", default=None,
                   help="comma-separated perspectives to guarantee coverage of "
                        "(e.g. security,reliability)")
    p.add_argument("--changed", default=None,
                   help="comma-separated changed nodes/files; adds a blast radius")
    p.add_argument("--hops", type=int, default=2)
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("tokens", parents=[common],
                       help="condense captured tool output; show tier policy")
    p.add_argument("action", choices=["condense", "policy"])
    p.add_argument("--file", default=None)
    p.add_argument("--command", default=None)
    p.add_argument("--exit-code", type=int, default=0)
    p.add_argument("--show", action="store_true")
    p.set_defaults(fn=cmd_tokens)

    p = sub.add_parser("ml", parents=[common],
                       help="leakage / reproducibility / rigor gates")
    p.add_argument("--file", required=True,
                   help="JSON describing the experiment setup and results")
    p.set_defaults(fn=cmd_ml)

    p = sub.add_parser("pipeline", parents=[common],
                       help="compose an inference-time layer stack for a budget")
    p.add_argument("--budget", type=int, default=8,
                   help="total model calls available")
    p.add_argument("--kind", default="general",
                   choices=["general", "verifiable", "open_ended"])
    p.set_defaults(fn=cmd_pipeline)
    return ap


def main(argv=None) -> None:
    force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
