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
    """Strict read: a command that needs config must fail if it is unreadable."""
    path = os.path.join(_data(args), "config.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _config_tolerant(args) -> tuple[dict, str | None]:
    """(config, error). For `doctor`, whose job is to REPORT damage.

    `doctor` crashed with a JSONDecodeError on a corrupt `config.json` — the one
    command whose entire purpose is to say what is broken here, failing to say
    it. Every other command keeps the strict read, because a command that cannot
    load its configuration should stop rather than run against defaults it did
    not choose.
    """
    path = os.path.join(_data(args), "config.json")
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as exc:
        return {}, f"{path} is not valid JSON: {exc}"
    except OSError as exc:
        return {}, f"{path} is unreadable: {exc}"


#: Data files that must be present AND parseable. Existence alone was the
#: original check, and it reported "all checks pass" with a corrupt knowledge
#: graph, a corrupt policy book, and a corrupt skill registry — the same
#: existence-is-not-a-measurement mistake this kit keeps finding elsewhere.
_REQUIRED_DATA = (
    ("ontology.json", "the schema the whole knowledge layer depends on"),
    ("config.json", "retrieval weights, budgets, protected paths"),
    (os.path.join("knowledge", "kg.json"), "the curated knowledge graph"),
    (os.path.join("policies", "policies.json"), "the policy book"),
    (os.path.join("registry", "skills.json"), "the skill registry"),
    (os.path.join("registry", "capabilities.json"), "the capability allowlist"),
)


def _allow_network(args) -> bool:
    if getattr(args, "allow_network", False):
        return True
    config, error = _config_tolerant(args)
    if error:
        # Reaching here means the caller only wanted the network flag. A damaged
        # config must not be silently read as "network allowed", and it must not
        # surface as a parser traceback from a command that never asked about
        # JSON. Stop with the reason.
        _die(error)
    return bool((config.get("providers") or {}).get("allow_network"))


# ---------------------------------------------------------------- core ----
def _die(message: str) -> "NoReturn":
    """Stop with a message a user can act on, never a traceback.

    Damaged project data must stop the command — running against defaults
    nobody chose is worse than stopping. But it stopped with a raw
    `json.decoder.JSONDecodeError` stack, which tells the user which line of
    CPython's parser was unhappy and not which of THEIR files to fix. Four
    commands failed that way on a corrupt config.
    """
    print(f"dobby: {message}", file=sys.stderr)
    print("       run `dobby doctor` for the full list of what is broken here.",
          file=sys.stderr)
    raise SystemExit(2)


def _load_stack(repo: str):
    from .core.bootstrap import merged_graph
    from .core.kg import Ontology, OntologyError
    from .core.policies import PolicyBook
    from .core.skills import SkillRegistry
    data = os.path.join(repo, ".dobby")

    def _read(label: str, rel: str, loader):
        path = os.path.join(data, rel)
        if not os.path.exists(path):
            _die(f"{label} is missing: {path}")
        try:
            return loader(path)
        except json.JSONDecodeError as exc:
            _die(f"{label} is not valid JSON ({path}): {exc}")
        except OntologyError as exc:
            _die(f"{label} does not satisfy the ontology ({path}): {exc}")
        except OSError as exc:
            _die(f"{label} is unreadable ({path}): {exc}")

    onto = _read("the ontology", "ontology.json", Ontology.load)
    try:
        kg = merged_graph(onto, data)
    except json.JSONDecodeError as exc:
        _die(f"the knowledge graph is not valid JSON: {exc}")
    except OntologyError as exc:
        _die(f"the knowledge graph does not satisfy the ontology: {exc}")
    policies = _read("the policy book", os.path.join("policies", "policies.json"),
                     PolicyBook)
    registry = _read("the skill registry",
                     os.path.join("registry", "skills.json"), SkillRegistry)
    config = _read("the config", "config.json",
                   lambda p: json.load(open(p, encoding="utf-8")))
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

    def check(name: str, ok: bool, detail: str, fix: str = "",
              blocking: bool = True) -> None:
        """`blocking` distinguishes a BROKEN installation from a THIN one.

        Missing or corrupt data is broken: nothing works and the exit code must
        say so. No agent CLI installed is thin but legitimate — a CI runner has
        none by design, and failing there would make every pipeline red for a
        condition that is not a defect. Advisory checks are still reported; they
        just do not set the exit code.
        """
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix,
                       "blocking": blocking})

    check("data_dir", os.path.isdir(data), data,
          "run: dobby init --scan .")

    # PARSE each data file, do not merely stat it. Existence-only checking
    # reported "all checks pass" against a corrupt knowledge graph, a corrupt
    # policy book, and a corrupt skill registry — the user is then told the
    # project is healthy and meets the real failure several commands later,
    # with nothing connecting the two.
    for rel, purpose in _REQUIRED_DATA:
        p = os.path.join(data, rel)
        if not os.path.exists(p):
            check(f"data:{rel}", False, f"missing: {p} ({purpose})",
                  "restore from the distribution, or run: dobby init --scan .")
            continue
        try:
            with open(p, encoding="utf-8") as f:
                parsed = json.load(f)
        except json.JSONDecodeError as exc:
            check(f"data:{rel}", False, f"present but NOT valid JSON: {exc}",
                  "restore the file; the engine cannot read it")
            continue
        except OSError as exc:
            check(f"data:{rel}", False, f"unreadable: {exc}", "check permissions")
            continue
        if not isinstance(parsed, dict) or not parsed:
            check(f"data:{rel}", False,
                  f"parses but is empty or not an object ({purpose})",
                  "restore from the distribution")
            continue
        check(f"data:{rel}", True, f"{len(parsed)} top-level key(s)")

    boot = os.path.join(data, "knowledge", "kg.bootstrap.json")
    check("bootstrapped", os.path.exists(boot), boot,
          "run: dobby init --scan <host-root> (the project is not instantiated)",
          blocking=False)
    gold = os.path.join(repo, "evals", "retrieval_gold.yaml")
    check("retrieval_gold", os.path.exists(gold), gold,
          "author project gold with the author-evals skill", blocking=False)

    try:
        import yaml  # noqa: F401
        check("pyyaml", True, "importable")
    except ImportError:
        check("pyyaml", False, "not importable", "pip install PyYAML")

    config, config_error = _config_tolerant(args)
    if config_error:
        check("config_readable", False, config_error,
              "restore .dobby/config.json; every command that needs it will "
              "fail until then")
    allow_network = (bool(getattr(args, "allow_network", False))
                     or bool((config.get("providers") or {}).get("allow_network")))
    fleet = fleet_report(allow_network=allow_network)
    check("providers", fleet["usable_count"] > 0,
          f"{fleet['usable_count']} usable: {fleet['usable_ids']}",
          "install at least one agent CLI (claude / codex / gemini / agy)",
          blocking=False)
    check("multi_agent", fleet["multi_agent_ready"],
          f"panel size {fleet['max_panel_size']} "
          f"({'>=2 providers' if fleet['multi_agent_ready'] else 'need >=2'})",
          "install a second provider so a panel has independent members",
          blocking=False)

    failed = [c for c in checks if not c["ok"]]
    blocking_failures = [c for c in failed if c.get("blocking", True)]
    advisory = [c for c in failed if not c.get("blocking", True)]
    _out({
        "platform": describe_platform(),
        "version": __import__("dobby").__version__,
        "repo": repo,
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "blocking_failures": [c["check"] for c in blocking_failures],
        "advisory_gaps": [c["check"] for c in advisory],
        "fleet": fleet,
        "verdict": (
            "all checks pass" if not failed else
            f"{len(blocking_failures)} BLOCKING failure(s): "
            + ", ".join(c["check"] for c in blocking_failures)
            + (f"; {len(advisory)} advisory gap(s): "
               + ", ".join(c["check"] for c in advisory) if advisory else "")
            if blocking_failures else
            f"usable, with {len(advisory)} advisory gap(s): "
            + ", ".join(c["check"] for c in advisory)),
    })
    # Exit non-zero when a check fails. `doctor` previously exited 0 while
    # reporting "1 check(s) failed", so a script or CI step could not detect the
    # failure it had just been told about — the diagnosis was printed and
    # simultaneously denied.
    if blocking_failures:
        sys.exit(1)


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
                       # None keeps the catalog's own per-provider default.
                       timeout_s=args.timeout,
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

    from .progress import Tracker
    from .spend import record_round
    tracker = Tracker(label=f"panel:{protocol.id}", total=len(tasks))

    def report(result, done, total):
        # stderr, so stdout stays a clean JSON document for piping.
        tracker.complete_unit(result.duration_s, failed=not result.ok)
        print(tracker.bar(), file=sys.stderr, flush=True)

    round_ = run_round(tasks, cwd=_repo(args),
                       max_concurrency=args.concurrency,
                       on_complete=report if args.progress else None)
    record_round(_data(args), round_, role=args.role)
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
        "timing": {
            "wall_s": round(round_.wall_s, 2),
            "agent_s": round(round_.serial_s, 2),
            "parallelism": round_.speedup(),
            "slowest": max((r.duration_s for r in round_.results
                            if r is not None), default=0.0),
            "note": ("a round finishes when its SLOWEST member does; "
                     "parallelism is agent time over wall time"),
        },
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


def cmd_sandbox(args):
    """Run a command with its output withheld from context, or query a capture."""
    from .sandbox import Capture, extract, run as sandbox_run, sweep
    data = _data(args)
    if args.action == "sweep":
        _out(sweep(data, keep_hours=args.keep_hours))
        return
    if args.action == "run":
        if not args.command:
            sys.exit("sandbox run needs --command")
        result = sandbox_run(args.command, data_dir=data, cwd=_repo(args),
                             timeout_s=args.timeout,
                             allow_network=args.allow_network,
                             protected_paths=(_config(args).get("protected_paths")
                                              or []))
        _out(result.to_dict())
        return
    if args.action == "extract":
        if not args.handle:
            sys.exit("sandbox extract needs --handle")
        path = os.path.join(data, "state", "sandbox", args.handle)
        if not os.path.exists(path):
            sys.exit(f"no capture at {path}")
        with open(path, "rb") as f:
            total = sum(1 for _ in f)
        capture = Capture(handle=args.handle, path=path,
                          bytes_total=os.path.getsize(path),
                          lines_total=total, truncated=False)
        _out(extract(capture, pattern=args.pattern, head=args.head,
                     tail=args.tail, around=args.around,
                     max_lines=args.max_lines))
        return
    sys.exit(f"unknown sandbox action {args.action!r}")


def cmd_spend(args):
    """Where the session's agent time went."""
    from .spend import render_detail, statusline, summarize
    data = _data(args)
    window = args.window * 60 if args.window else None
    if args.line:
        print(statusline(data, window_s=window))
        return
    print(render_detail(data, window_s=window))
    if args.json:
        _out(summarize(data, window_s=window))


def cmd_style(args):
    """Detect the generated-prose signature in a file or a string."""
    from .style import analyze, rewrite_budget, rewrite_instruction
    if args.file:
        with open(os.path.abspath(args.file), encoding="utf-8",
                  errors="replace") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        sys.exit("style needs --file or --text")
    report = analyze(text)
    if args.rewritten:
        with open(os.path.abspath(args.rewritten), encoding="utf-8",
                  errors="replace") as f:
            report["rewrite_budget"] = rewrite_budget(text, f.read())
    report["rewrite_instruction"] = rewrite_instruction(report)
    _out(report)


def cmd_prompt(args):
    """Compile a casual request into an executable prompt, naming what is unsaid."""
    from .prompt import clarifying_question, compile_prompt
    compiled = compile_prompt(
        args.request,
        objective=args.objective or "", acceptance=args.acceptance or "",
        scope=args.scope or "", output_contract=args.contract or "",
        role=args.role or "",
        context=[c for c in (args.context or "").split("|") if c.strip()])
    out = compiled.to_dict()
    out["ask"] = clarifying_question(args.request)
    _out(out)


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
    # `fleet --probe` had this and `panel` did not, so the only way to bound a
    # round was the catalog's per-provider default of 900s. A round finishes when
    # its slowest member does, so one stalled provider held the whole panel for
    # fifteen minutes with nothing the caller could do about it.
    p.add_argument("--timeout", type=int, default=None,
                   help="per-provider seconds; overrides the catalog default "
                        "(a round is only as fast as its slowest member)")
    p.add_argument("--with-context", action="store_true",
                   help="include the routed knowledge pack in each prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="show the assignments and prompts without invoking")
    p.add_argument("--progress", action="store_true",
                   help="print a progress bar to stderr as agents finish")
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

    p = sub.add_parser("sandbox", parents=[common],
                       help="run with output withheld from context; query it")
    p.add_argument("action", choices=["run", "extract", "sweep"])
    p.add_argument("--command", default=None)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--allow-network", action="store_true",
                   help="keep proxy routes (network is NOT truly blocked either "
                        "way; see the module docstring)")
    p.add_argument("--handle", default=None,
                   help="capture handle, e.g. ab12cd34ef.out")
    p.add_argument("--pattern", default=None)
    p.add_argument("--head", type=int, default=None)
    p.add_argument("--tail", type=int, default=None)
    p.add_argument("--around", type=int, default=0)
    p.add_argument("--max-lines", type=int, default=200)
    p.add_argument("--keep-hours", type=float, default=24.0)
    p.set_defaults(fn=cmd_sandbox)

    p = sub.add_parser("spend", parents=[common],
                       help="agent time per provider; --line for a status bar")
    p.add_argument("--line", action="store_true",
                   help="one-line form, for a host statusLine command")
    p.add_argument("--window", type=float, default=None,
                   help="restrict to the last N minutes")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_spend)

    p = sub.add_parser("style", parents=[common],
                       help="detect the generated-prose signature (EN + KO)")
    p.add_argument("--file", default=None)
    p.add_argument("--text", default=None)
    p.add_argument("--rewritten", default=None,
                   help="a candidate rewrite, to score against the change budget")
    p.set_defaults(fn=cmd_style)

    p = sub.add_parser("prompt", parents=[common],
                       help="compile a casual request into an executable prompt")
    p.add_argument("request")
    p.add_argument("--objective", default=None)
    p.add_argument("--acceptance", default=None)
    p.add_argument("--scope", default=None)
    p.add_argument("--contract", default=None)
    p.add_argument("--role", default=None)
    p.add_argument("--context", default=None,
                   help="pipe-separated established facts")
    p.set_defaults(fn=cmd_prompt)
    return ap


def main(argv=None) -> None:
    force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
