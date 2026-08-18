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
    dobby agy check "task"              should this go to Antigravity at all?
    dobby agy run "task" --yes          delegate it, with a template and a budget
    dobby panel "task" [--size N]       decorrelated multi-agent round
    dobby search "task" --score-command tree search, scored by a command
    dobby graph --changed FILE...       who depends on what changed
    dobby endtask --split dev --yes     does the preamble change behaviour?
    dobby swebench --limit 2 --yes      real SWE-bench instances, harness varied
    dobby memory <stats|route|expire|integrity>
    dobby compress --file F             compression with a leakage audit
    dobby specialize [--status]         mastery level and its evidence
    dobby research plan "need"          decomposed search plan
    dobby design validate               DESIGN.md token check

    dobby runtime run "task"            a durable run: plan/execute/verify/report
    dobby runtime resume <run_id>       continue it; finished nodes are not re-run
    dobby runtime status <run_id>       nodes, attempts, artifacts, integrity
    dobby runtime list                  every run in this project

    dobby project init --smoke "..."    scan, baseline, and fix the contract
    dobby project open                  a shift: what is verified, what is next
    dobby project next                  the next work item, chosen arithmetically
    dobby project attach-run W001 <run> point an item at the run judging it
    dobby project close <session_id>    judge by the run, write the handover
    dobby project run --until empty     the loop: one verified item at a time

Almost every command prints JSON on stdout so the output is consumable by another
process without parsing prose. The exceptions are deliberate and are named here,
because a blanket promise that two commands break is worse than an accurate one —
a consumer piping `spend` into `json.loads` crashed on `no agent calls recorded`:

- `spend` renders a human-readable breakdown; pass `--json` for the machine form,
  or `--line` for a one-line status bar.

UTF-8 is pinned first (see core/platform.py): without it, any non-ASCII
knowledge-graph summary crashes the process on a non-UTF-8 Windows locale.
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


def _read_json(path: str):
    """Read one JSON file and CLOSE it.

    This was `lambda p: json.load(open(p, encoding="utf-8"))`, which leaks the
    handle. On Windows an unclosed handle makes `shutil.rmtree` fail with
    PermissionError, so it is a flaky failure waiting for a slower machine — and
    it is how the leak surfaced: a ResourceWarning from an unrelated test.
    """
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


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
    config = _read("the config", "config.json", _read_json)
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
    # The artifact a model_judgment criterion grades is the executed step's own
    # output. Passing it only when --judge is set keeps the paid path explicit.
    artifact = None
    if args.judge:
        artifact = (f"COMMAND: {command}\nEXIT: {proc.returncode}\n\n"
                    f"--- STDOUT ---\n{proc.stdout}\n"
                    f"--- STDERR ---\n{proc.stderr}\n")
    ev = Evaluator(os.path.join(repo, spec["criteria"]), repo, config=config,
                   judge=args.judge, artifact=artifact,
                   judge_provider=args.judge_provider)
    evaluation = ev.evaluate()
    handoff = traj.handoff(
        done=[f"executed {spec['capability']} (exit {proc.returncode})",
              f"evaluator verdict: {evaluation['verdict']}"],
        remaining=([f"model-judgment criteria not evaluated "
                    f"({evaluation['not_evaluated']})"]
                   if evaluation["not_evaluated"] else
                   [f"advisory judgments recorded but excluded from the "
                    f"verdict ({len(evaluation['advisory'])})"]),
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


def cmd_search(args):
    """Tree search over provider-produced candidates, scored by a command.

    The score comes from running something, never from a model judging its own
    output. A search told to maximise self-assessment reports steady improvement
    while producing nothing better, and the failure is invisible because the
    metric and the artifact come from the same place.
    """
    from .search import search
    from .search_driver import command_scorer, driver_report, provider_expander

    repo = _repo(args)
    data = _data(args)
    config = _config(args)

    scorer = None
    if args.score_command:
        try:
            scorer = command_scorer(args.score_command, data_dir=data, cwd=repo,
                                    timeout_s=args.score_timeout, config=config)
        except ValueError as exc:
            _die(str(exc))

    expander = provider_expander(
        args.task, score=scorer, provider_id=args.provider,
        timeout_s=args.timeout, cwd=repo)

    result = search(expand=expander, max_nodes=args.max_nodes,
                    min_drafts=args.min_drafts, debug_depth=args.debug_depth,
                    patience=args.patience,
                    higher_is_better=not args.lower_is_better)
    # `result.summary()`, named directly. The first version wrote
    # `result.to_dict() if hasattr(result, "to_dict") else dict(result)` — a guess
    # at the API wrapped in a guard that hid the guess until runtime. SearchResult
    # has neither method, so three real provider calls were spent and then thrown
    # away by a TypeError while formatting the output. A defensive fallback around
    # an API nobody checked is worse than no fallback: it converts "read the
    # dataclass" into "lose the run".
    payload = result.summary()
    payload["driver"] = driver_report(result, expander.calls)
    if result.best is not None:
        payload["best"] = {"id": result.best.id, "score": result.best.score,
                           "action": result.best.action,
                           "content": result.best.content}
    if scorer is None:
        payload["driver"]["warning"] = (
            "no --score-command: every candidate is uncomparable, so the search "
            "produced no viable node. This is the honest outcome, not a bug.")
    _out(payload)


def cmd_graph(args):
    """Import edges for this repo, or the blast radius of specific changed files.

    Closes the recorded gap that `blast_radius` had no edge source: it consumed a
    supplied edge list and nothing in the kit produced one.
    """
    from .codegraph import import_edges, radius_for

    repo = _repo(args)
    if args.changed:
        _out(radius_for(repo, args.changed, max_hops=args.max_hops,
                        max_nodes=args.max_nodes))
        return

    edges, report = import_edges(repo, internal_only=not args.include_external)
    payload = dict(report)
    if args.edges:
        payload["edge_list"] = [list(e) for e in edges]
    else:
        payload["edge_list_omitted"] = (
            f"{len(edges)} edges; pass --edges to include them")
    _out(payload)


def cmd_endtask(args):
    """The compliance experiment. See docs/EVAL_DESIGN.md before reading a number.

    Measures whether the harness preamble changes output in the direction it
    specifies. That is close to circular by construction, and the informative
    outcome is a NULL one - it would mean the rules are being ignored. Compliance
    is not benefit.
    """
    import sys as _sys

    from .endtask import (CONDITIONS, append_trials, deduplicate, load_tasks,
                          read_trials, run_experiment, summarize)

    repo = _repo(args)
    if args.from_trials:
        # Re-summarize saved trials without calling anything. A reporting fix
        # should not cost another half hour of provider time, and batches run
        # separately have to be poolable or a long run cannot be split at all.
        pooled, problems = read_trials(args.from_trials)
        pooled, dropped = deduplicate(pooled)
        tasks_all = load_tasks(
            args.tasks or os.path.join(repo, "evals", "endtask", "tasks.json"))
        present = {t["task"] for t in pooled}
        tasks_seen = [t for t in tasks_all if t["id"] in present]
        conditions = tuple(dict.fromkeys(t["condition"] for t in pooled))
        report = summarize(pooled, tasks_seen, conditions=conditions,
                          reps=args.reps, declared_threshold=args.declare)
        report["pooled_from"] = list(args.from_trials)
        report["trials_read"] = len(pooled)
        report["duplicate_trials_dropped"] = dropped
        report["malformed_lines"] = problems
        _out(report)
        return
    tasks_path = args.tasks or os.path.join(repo, "evals", "endtask", "tasks.json")
    if not os.path.exists(tasks_path):
        _die(f"no task file at {tasks_path}")
    tasks = load_tasks(tasks_path, split=args.split)
    if not tasks:
        _die(f"no tasks in {tasks_path}"
             + (f" for split {args.split!r}" if args.split else ""))

    for condition in args.conditions:
        if condition not in CONDITIONS:
            _die(f"unknown condition {condition!r}; expected {list(CONDITIONS)}")

    trials = len(tasks) * len(args.conditions) * args.reps
    print(f"{trials} provider call(s): {len(tasks)} task(s) x "
          f"{len(args.conditions)} condition(s) x {args.reps} rep(s), "
          f"provider {args.provider}", file=_sys.stderr)
    if not args.yes:
        _die(f"this spends {trials} real provider calls; pass --yes to run. "
             "An eval that spends silently is one nobody re-runs.")

    done = {"n": 0}

    def progress(record):
        done["n"] += 1
        if args.trials_out:
            append_trials(args.trials_out, [record])
        mark = "ok " if record["ok"] else "FAIL"
        score = record.get("total")
        print(f"  [{done['n']}/{trials}] {mark} {record['task']}/"
              f"{record['condition']}#{record['rep']} "
              f"score={score} {record['duration_s']}s", file=_sys.stderr)

    report = run_experiment(
        tasks, repo=repo, provider_id=args.provider,
        conditions=tuple(args.conditions), reps=args.reps,
        timeout_s=args.timeout, declared_threshold=args.declare,
        on_trial=progress)
    report["split"] = args.split or "all"
    report["tasks_file"] = tasks_path.replace("\\", "/")
    if not args.keep_outputs:
        report["outputs_note"] = ("model outputs omitted; pass --keep-outputs to "
                                 "include them for manual inspection")
    _out(report)


def cmd_swebench(args):
    """Real SWE-bench instances, model fixed, harness as the variable.

    NOT a SWE-bench score. `resolved` needs the instance's pinned environment to
    run FAIL_TO_PASS/PASS_TO_PASS, which in practice needs the official Docker
    images, and Docker is absent here. What is measured is localization against the
    gold patch and how many files outside it were touched - both necessary for
    resolution, neither sufficient.
    """
    import sys as _sys

    from .endtask import append_trials, deduplicate, read_trials
    from .swebench import (SweBenchError, fetch_instances, find_instances,
                          run_instance, summarize, write_extra_for)

    repo_root = _repo(args)
    if args.from_trials:
        pooled, problems = read_trials(args.from_trials)
        pooled = [t for t in pooled if "instance_id" in t]
        # Keyed on (instance, condition) here, not (task, condition, rep).
        seen, kept = set(), []
        for trial in pooled:
            key = (trial["instance_id"], trial["condition"])
            if key in seen and trial.get("ok"):
                kept = [t for t in kept
                        if not ((t["instance_id"], t["condition"]) == key
                                and not t.get("ok"))]
            if key in seen:
                continue
            seen.add(key)
            kept.append(trial)
        conditions = tuple(dict.fromkeys(t["condition"] for t in kept))
        report = summarize(kept, conditions=conditions)
        report["malformed_lines"] = problems
        report["trials_read"] = len(kept)
        _out(report)
        return

    if args.instances:
        # Scan the split rather than making the caller know which 100-row page an
        # instance sits on. Telling a user about pagination is not an answer.
        instances, missing = find_instances(args.instances, dataset=args.dataset,
                                            split=args.split)
        if missing:
            _die(f"not in {args.dataset} split {args.split!r}: {missing}")
    else:
        instances = fetch_instances(dataset=args.dataset, split=args.split,
                                    limit=args.pool, offset=args.offset)
        instances = instances[:args.limit]
    if not instances:
        _die("no instances selected")

    try:
        write_extra_for(args.provider)
    except SweBenchError as exc:
        _die(str(exc))

    calls = len(instances) * len(args.conditions)
    print(f"{calls} agent run(s): {len(instances)} instance(s) x "
          f"{len(args.conditions)} condition(s), provider {args.provider}, "
          f"sandbox workspace-write", file=_sys.stderr)
    if not args.yes:
        _die(f"this spends {calls} real agent runs and clones {calls} "
             f"repositories; pass --yes to run")

    workdir = args.workdir or os.path.join(_data(args), "state", "swebench")
    os.makedirs(workdir, exist_ok=True)
    trials = []
    for index, instance in enumerate(instances, 1):
        for condition in args.conditions:
            print(f"  [{len(trials)+1}/{calls}] {instance['instance_id']}/"
                  f"{condition} ...", file=_sys.stderr, flush=True)
            record = run_instance(instance, condition, workdir=workdir,
                                 provider_id=args.provider, repo_root=repo_root,
                                 timeout_s=args.timeout,
                                 keep_clone=args.keep_clones)
            trials.append(record)
            if args.trials_out:
                append_trials(args.trials_out, [record])
            print(f"      ok={record.get('ok')} "
                  f"edited={record.get('made_any_edit')} "
                  f"localized={record.get('localized_all_gold_files')} "
                  f"extra={record.get('extra_file_count')} "
                  f"{record.get('duration_s')}s", file=_sys.stderr, flush=True)

    _out(summarize(trials, conditions=tuple(args.conditions)))


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
    elif args.action == "run":
        # A plan that is never executed is the failure this action exists to fix.
        # It spends money — one provider call per query shape — so it is opt-in,
        # the same stance `fleet --probe` and `endtask` take.
        from .research_runner import ResearchError, research, web_provider
        from .research import plan_queries
        if not args.need:
            sys.exit("research run needs the information need as an argument")
        try:
            chosen = web_provider(args.provider)
        except ResearchError as exc:
            sys.exit(f"cannot search: {exc}")
        plan = plan_queries(args.need, year_hint=args.year)
        if not args.yes:
            _out({
                "action": "run",
                "refused": "not confirmed",
                "would_call": chosen,
                "calls": len(plan.queries),
                "queries": [q["query"] for q in plan.queries],
                "why": ("each query is a real provider call and costs money, so "
                        "this is opt-in. Re-run with --yes to search."),
            })
            return
        _out(research(args.need, year_hint=args.year, provider_id=args.provider,
                      timeout_s=args.timeout, cwd=_repo(args)))
    else:
        sys.exit(f"unknown research action {args.action!r}")


# ------------------------------------------------------------- design ----
def cmd_design(args):
    from .design import validate_design_md
    path = os.path.join(_repo(args), "DESIGN.md")
    if args.file:
        path = os.path.abspath(args.file)
    _out(validate_design_md(path))


# -------------------------------------------------------------- skills ----
def cmd_skills(args):
    """List the registry, or merge factory skills the host has never seen.

    The installer calls `--sync-from` on every upgrade. Without it the SKILL.md
    files land and the router stays blind to them, because `.dobby/` is preserved
    on upgrade and the registry lives inside it.
    """
    from .core.skills import SkillError, SkillRegistry
    repo = _repo(args)
    host = os.path.join(repo, ".dobby", "registry", "skills.json")
    registry = SkillRegistry(host)

    if args.sync_from:
        try:
            result = registry.merge_factory(os.path.abspath(args.sync_from),
                                            repo_root=repo)
        except SkillError as exc:
            sys.exit(str(exc))
        if result["added"]:
            registry.save()
        _out(result)
        return

    _out([{"name": entry["name"],
           "state": registry.skills[entry["name"]].get("state"),
           "path": registry.skills[entry["name"]].get("path"),
           "origin_ok": registry.verify_origin(entry["name"], repo_root=repo)[0]}
          for entry in registry.index()])


# ---------------------------------------------------------------- agy ----
def cmd_agy(args):
    """Delegate to Antigravity, or decide not to.

    `check`, `prompt`, `caps`, `templates` and `triggers` spend nothing — they are
    the whole point of the lane, because most delegations should not happen. `run`
    makes a real call and therefore requires `--yes`, the same gate `research` and
    `fleet --probe` use.
    """
    from . import agy as agy_mod

    if args.action == "caps":
        _out(agy_mod.capabilities())
        return
    if args.action == "templates":
        _out(agy_mod.templates())
        return
    if args.action == "triggers":
        _out(agy_mod.triggers())
        return
    if args.action == "models":
        _out(agy_mod.models(timeout_s=args.timeout or 60))
        return
    if args.action == "agents":
        _out(agy_mod.agents(timeout_s=args.timeout or 60))
        return

    if not args.task:
        sys.exit(f"`dobby agy {args.action}` needs a task: "
                 f"dobby agy {args.action} \"<task>\"")

    if args.action == "check":
        _out(agy_mod.assess(args.task,
                            estimated_tool_calls=args.tool_calls).to_dict())
        return

    if args.action not in ("prompt", "run"):
        sys.exit(f"unknown agy action {args.action!r}")

    verdict = agy_mod.assess(args.task, estimated_tool_calls=args.tool_calls)
    try:
        envelope = agy_mod.delegate(
            args.task,
            template=args.template or verdict.template,
            project_root=_repo(args),
            files=args.file or (),
            stack=args.stack or "",
            requirements=[r for r in (args.require or "").split("|") if r.strip()],
            model=args.model, effort=args.effort,
            add_dirs=args.add_dir or (),
            timeout_s=args.timeout,
            allow_writes=args.write,
            skip_permissions=args.skip_permissions,
            continue_conversation=args.continue_conversation,
            conversation=args.conversation,
            output_format=args.output_format,
            json_schema=args.json_schema,
            sandbox=args.sandbox,
            agent=args.agent,
            cwd=_repo(args),
            # `prompt` is the review surface: it builds and validates the whole
            # call, including every flag value, and spends nothing.
            dry_run=(args.action == "prompt" or not args.yes))
    except agy_mod.AgyError as exc:
        sys.exit(str(exc))

    envelope["verdict"] = verdict.to_dict()
    if args.action == "run" and not args.yes:
        envelope["why_nothing_ran"] = (
            "a delegation is a real provider call and costs money; re-run with "
            "--yes. The prompt above is exactly what would be sent.")
    _out(envelope)


# ---------------------------------------------------------------- hwp ----
def cmd_hwp(args):
    """Read and edit 한글 documents, dispatching on the file's magic bytes.

    The extension is a claim; the first eight bytes are a measurement. 한글 saves
    legacy format under whatever name was typed, so a `.hwpx` that is really an
    OLE compound file is common and must be named rather than crashed on.
    """
    from . import hwp5, hwpx
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    try:
        kind = hwpx.detect_format(path)
    except hwpx.HwpxError as exc:
        sys.exit(str(exc))

    if args.action == "info":
        _out(hwpx.summarize(path) if kind == "hwpx" else hwp5.summarize(path))
        return
    if args.action == "text":
        text = (hwpx.document_text(path) if kind == "hwpx"
                else hwp5.document_text(path))
        sys.stdout.write(text + "\n")
        return

    # These drive 한글 itself and so work on either format, including the
    # binary one this file cannot write directly.
    if args.action in ("pages", "export", "shapes", "edit"):
        from . import hwpcom
        try:
            if args.action == "pages":
                _out({"file": path, "pages": hwpcom.page_count(path)})
            elif args.action == "export":
                if not args.out:
                    sys.exit("export needs --out (.pdf, .hwpx or .hwp)")
                _out(hwpcom.export(path, args.out))
            elif args.action == "shapes":
                _out(hwpcom.paragraph_shapes(path, list_id=args.list))
            else:
                if args.text is None or args.with_ is None:
                    sys.exit("edit needs --text and --with")
                _out(hwpcom.replace(
                    path, [(args.text, args.with_)], out=args.out,
                    overwrite=args.overwrite, list_id=args.list,
                    apply=not args.dry))
        except hwpcom.HwpComError as exc:
            sys.exit(str(exc))
        return

    if kind != "hwpx":
        # Stated as a limit with its reason and a way forward, not as a bug.
        sys.exit(
            f"{os.path.basename(path)} is HWP 5.0 (binary). The reader here "
            f"({os.path.basename(__file__)} → hwp5.py) will not write it: the "
            f"body is compressed records inside a compound file whose sector "
            f"allocation would have to be rebuilt, and a half-correct writer "
            f"corrupts documents in ways that only appear when 한글 opens them. "
            f"Two ways forward: `dobby hwp edit` drives 한글 over COM and edits "
            f"this file as it is (Windows, 한글 installed — see `dobby hwp "
            f"pages` to check the setup), or save it as HWPX in 한글 and use "
            f"the actions here.")

    doc = hwpx.HwpxDocument(path)
    if args.action == "paragraphs":
        _out([{"index": p.index, "location": p.location, "text": p.text}
              for p in doc.paragraphs if p.text.strip() or args.all])
        return
    if args.action == "tables":
        _out(doc.tables())
        return
    if args.action == "find":
        if not args.text:
            sys.exit("find needs --text")
        _out([{"index": p.index, "location": p.location, "text": p.text}
              for p in doc.find(args.text)])
        return
    if args.action == "replace":
        if args.text is None or args.with_ is None:
            sys.exit("replace needs --text and --with")
        if not args.out:
            sys.exit("replace needs --out; this never writes over the source")
        result = doc.replace_text(args.text, args.with_, count=args.count)
        if not result["applied"]:
            # Absence and refusal are different answers to "did it work".
            _out({**result, "written": None,
                  "why": ("nothing was written because nothing was replaced"
                          if not result["straddled"] else
                          "nothing was written; every match crosses two runs")})
            return
        _out({**result, **doc.save(args.out, overwrite=args.overwrite)})
        return
    if args.action == "set":
        if args.index is None or args.text is None:
            sys.exit("set needs --index and --text")
        if not args.out:
            sys.exit("set needs --out; this never writes over the source")
        try:
            changed = doc.set_paragraph_text(args.index, args.text)
        except hwpx.HwpxError as exc:
            sys.exit(str(exc))
        _out({**changed, **doc.save(args.out, overwrite=args.overwrite)})
        return
    sys.exit(f"unknown hwp action {args.action!r}")


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


def cmd_runtime(args):
    """Durable execution: a run that survives the process that started it.

    `run` creates a `TaskRun` and drives it. `resume` attaches to an existing
    one and continues from what the store says already happened — the same code
    path, deliberately, because a recovery route that differs from the normal
    one only ever executes when it is least affordable for it to be wrong.
    """
    from .runtime import RunBudget, Runner, RunStore, default_graph

    repo = _repo(args)
    data = _data(args)

    if args.action == "list":
        _out({"runs": RunStore(data).list_runs(limit=args.limit)})
        return

    if args.action == "status":
        store = RunStore(data)
        state = store.load_run(args.run_id)
        _out({"run_id": state["run_id"], "task": state["task"],
              "state": state["state"], "graph": state["graph"].summary(),
              "attempts": store.attempts(args.run_id),
              "artifacts": store.artifacts(args.run_id),
              "effects": store.effects(args.run_id),
              "integrity": store.rebuild(args.run_id)})
        return

    if args.action == "events":
        _out({"run_id": args.run_id,
              "events": RunStore(data).events(args.run_id)})
        return

    if args.action == "metrics":
        from .runtime.metrics import report as metrics_report
        _out(metrics_report(RunStore(data)))
        return

    if args.action == "scorecard":
        from .runtime.metrics import scorecard
        card = scorecard(RunStore(data))
        _out({"scorecard": card,
              "note": ("empty means no provider node has run yet. An unmeasured "
                       "provider is not a bad one — placement tries it, which is "
                       "how the first row gets written")
              if not card else ""})
        return

    if args.action == "harvest":
        from .runtime.flywheel import report as flywheel_report
        _out(flywheel_report(RunStore(data), data, write=args.write))
        return

    if args.action == "bench":
        from .runtime import bench as bench_mod
        corpus = (bench_mod.load_corpus(args.corpus) if args.corpus
                  else bench_mod.example_corpus())
        outcomes = bench_mod.run_corpus(repo, data, corpus)
        payload = bench_mod.report(outcomes)
        if not args.corpus:
            payload["warning"] = (
                "no --corpus given, so this ran the EXAMPLE shape: two "
                "trivially satisfiable tasks that exist to exercise the "
                "harness. Its numbers are not a benchmark result")
        payload["written_to"] = bench_mod.save_report(data, payload)
        _out(payload)
        return

    if args.action == "trace":
        from .runtime.trace import render_timeline, to_otlp
        spans = RunStore(data).spans(args.run_id)
        if args.otlp:
            _out(to_otlp(spans))
            return
        print("\n".join(render_timeline(spans)))
        return

    runner = Runner(repo, data_dir=data, max_parallel=args.parallel,
                    allow_network=_allow_network(args))
    approvals = {a for a in (args.approve or "").split(",") if a.strip()}

    if args.action == "resume":
        result = runner.run(args.run_id, approvals=approvals,
                            max_steps=args.max_steps)
        _out(result.to_dict())
        return

    # `run`
    checks = [c for c in (args.check or "").split("|") if c.strip()]
    graph = default_graph(args.task, provider=args.provider,
                          execute_command=args.execute,
                          acceptance_checks=checks,
                          static=not (args.provider or args.execute))
    route = {}
    if not args.no_route:
        from .core.router import Router
        _, kg, policies, registry, config = _load_stack(repo)
        plan = Router(policies, registry, kg, config).route(args.task)
        route = {"level": plan.level, "model_tier": plan.model_tier,
                 "policies": plan.policies, "skills": plan.skills,
                 "justification": plan.justification}
        budget = RunBudget.from_router_budgets(
            plan.budgets, max_irreversible=args.allow_irreversible)
    else:
        budget = RunBudget(max_irreversible=args.allow_irreversible)

    run_id = runner.start(args.task, graph, budget=budget, route=route)
    result = runner.run(run_id, budget=budget, approvals=approvals,
                        max_steps=args.max_steps)
    payload = result.to_dict()
    payload["route"] = route
    payload["resume_with"] = f"dobby runtime resume {run_id}"
    _out(payload)


def cmd_project(args):
    """The unit above a run: a portfolio that survives the session working it.

    Nothing here calls a provider. `init` scans and runs the smoke checks;
    `next` ranks arithmetically. The one judgement left to a model — whether an
    item is well enough understood to implement — is REPORTED as
    `needs_architect` rather than made here.
    """
    from .project import (ProjectStore, advance, attach_run, close_session,
                          initialise, open_session, select_next)

    repo = _repo(args)
    data = _data(args)

    if args.action == "init":
        specs = _read_json(args.items) if args.items else []
        if isinstance(specs, dict):
            specs = specs.get("items", [])
        _out(initialise(data, args.root or repo,
                        smoke=tuple(c for c in (args.smoke or "").split("|")
                                    if c.strip()),
                        item_specs=specs,
                        allow_network=_allow_network(args),
                        run_baseline=not args.no_baseline))
        return

    store = ProjectStore(data)

    if args.action == "list":
        _out({"projects": store.list_projects()})
        return

    project = store.load_project(args.project)

    if args.action == "status":
        baseline = project["baseline"]
        _out({"project_id": project["project_id"], "root": project["root"],
              "manifest_digest": project["manifest_digest"],
              "portfolio_version": project["portfolio"].version,
              "coverage": project["portfolio"].coverage(),
              "baseline": baseline.to_dict() if baseline else None,
              "items": [i.to_dict() for i in project["portfolio"].items],
              "sessions": store.sessions(project["project_id"], limit=5)})
        return

    if args.action == "events":
        _out({"project_id": project["project_id"],
              "events": store.events(project["project_id"])})
        return

    if args.action == "next":
        from .runtime.store import RunStore
        from .project.session import _unconfirmed_by_run
        unconfirmed = _unconfirmed_by_run(
            RunStore(data),
            [i.latest_run_id for i in project["portfolio"].items])
        selection = select_next(project["portfolio"],
                                baseline=project["baseline"],
                                unconfirmed_effects=unconfirmed)
        _out(selection.to_dict())
        return

    if args.action == "attach-run":
        if not (args.work_item and args.run_id):
            _die("attach-run needs a work item id and a run id: "
                 "dobby project attach-run W001 <run_id>")
        _out(attach_run(data, args.work_item, args.run_id,
                        project_id=project["project_id"]))
        return

    if args.action == "open":
        _out(open_session(data, project_id=project["project_id"],
                          rebaseline=args.rebaseline).to_dict())
        return

    if args.action == "run":
        # The one command that acts on its own, so it is the one that has to be
        # explicit about where it stopped. `stopped` is a closed set of reasons
        # (dobby/project/loop.py), not prose, because a caller deciding whether
        # to escalate to a human cannot parse a sentence.
        _out(advance(data, project_id=project["project_id"],
                     provider=args.provider, execute_command=args.execute,
                     max_items=(0 if args.until == "empty" else args.max_items),
                     max_steps=args.max_steps))
        return

    if args.action == "close":
        if not args.work_item:
            _die("close needs a session id: dobby project close <session_id>")
        _out(close_session(data, args.work_item,
                           promote=not args.no_promote).to_dict())
        return


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
    # Opt-in, like `fleet --probe`: judging spends money and leaves the machine.
    # A model verdict stays ADVISORY and can never move the PASS/FAIL verdict,
    # because .dobby/ontology.json forbids a model_assertion from counting as
    # verification. See dobby/judge.py.
    p.add_argument("--judge", action="store_true",
                   help="grade model_judgment criteria with a provider "
                        "(advisory only; costs money and calls out)")
    p.add_argument("--judge-provider", default=None,
                   help="force one provider as the judge instead of the "
                        "critic-role default")
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

    p = sub.add_parser("search", parents=[common])
    p.add_argument("task")
    p.add_argument("--score-command", default=None,
                   help="command that scores a candidate; must contain "
                        "{candidate}, which becomes the candidate's file path. "
                        "Without it nothing is comparable and no node is viable.")
    p.add_argument("--score-timeout", type=int, default=300)
    p.add_argument("--provider", default=None)
    p.add_argument("--timeout", type=int, default=None,
                   help="per-provider seconds")
    p.add_argument("--max-nodes", type=int, default=8)
    p.add_argument("--min-drafts", type=int, default=2)
    p.add_argument("--debug-depth", type=int, default=2)
    p.add_argument("--patience", type=int, default=4)
    p.add_argument("--lower-is-better", action="store_true",
                   help="for loss-like metrics")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("graph", parents=[common])
    p.add_argument("--changed", nargs="*", default=None,
                   help="changed FILE paths; prints who depends on them")
    p.add_argument("--max-hops", type=int, default=2)
    p.add_argument("--max-nodes", type=int, default=40)
    p.add_argument("--edges", action="store_true",
                   help="include the full edge list, not just the counts")
    p.add_argument("--include-external", action="store_true",
                   help="keep imports of modules outside this repo (they can "
                        "never originate a blast radius here)")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("endtask", parents=[common])
    p.add_argument("--tasks", default=None,
                   help="task JSON (default evals/endtask/tasks.json)")
    p.add_argument("--split", default=None,
                   help="dev | holdout. holdout is run ONCE per reported claim")
    p.add_argument("--provider", default="codex",
                   help="held fixed across conditions; the harness is the "
                        "variable being tested")
    p.add_argument("--conditions", nargs="+", default=["bare", "harness"],
                   help="bare | harness | padded. `padded` is the "
                        "length-matched control")
    p.add_argument("--reps", type=int, default=3,
                   help="repetitions per cell; pass^k needs more than one")
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--declare", type=float, default=None,
                   help="expected minimum effect, recorded BEFORE the run. "
                        "Without it the verdict is marked exploratory")
    p.add_argument("--keep-outputs", action="store_true",
                   help="include raw model outputs in the report")
    p.add_argument("--trials-out", default=None,
                   help="append each trial to this JSONL as it completes, so an "
                        "interrupted run keeps what it paid for")
    p.add_argument("--from-trials", nargs="+", default=None,
                   help="summarize saved trial files and call nothing; pools "
                        "batches and re-reports without new spend")
    p.add_argument("--yes", action="store_true",
                   help="confirm the provider spend")
    p.set_defaults(fn=cmd_endtask)

    p = sub.add_parser("swebench", parents=[common])
    p.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    p.add_argument("--split", default="test")
    p.add_argument("--pool", type=int, default=100,
                   help="rows to fetch before selecting")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--instances", nargs="+", default=None,
                   help="explicit instance_ids")
    p.add_argument("--limit", type=int, default=2,
                   help="instances to run when none are named")
    p.add_argument("--provider", default="codex",
                   help="held fixed; the harness is the variable")
    p.add_argument("--conditions", nargs="+", default=["bare", "harness"])
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--workdir", default=None)
    p.add_argument("--keep-clones", action="store_true")
    p.add_argument("--trials-out", default=None,
                   help="append each trial as it completes")
    p.add_argument("--from-trials", nargs="+", default=None,
                   help="summarize saved trials, clone and call nothing")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_swebench)

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
    p.add_argument("action", choices=["plan", "run", "claims", "citations"])
    p.add_argument("need", nargs="?", default="")
    p.add_argument("--file", default=None)
    p.add_argument("--corpus", default=None)
    p.add_argument("--year", default=None)
    p.add_argument("--provider", default=None,
                   help="web-capable provider for `run` (default: first usable)")
    p.add_argument("--yes", action="store_true",
                   help="`run` spends money; without this it only shows the calls")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(fn=cmd_research)

    p = sub.add_parser("skills", parents=[common],
                       help="list registered skills; merge new factory ones")
    p.add_argument("--sync-from", default=None,
                   help="a kit registry to merge missing skills FROM (add-only)")
    p.set_defaults(fn=cmd_skills)

    p = sub.add_parser(
        "agy", parents=[common],
        help="delegate to Antigravity (`agy`) — or find out you should not")
    p.add_argument("action",
                   choices=["check", "prompt", "run", "caps", "templates",
                            "triggers", "models", "agents"])
    p.add_argument("task", nargs="?", default=None)
    p.add_argument("--template", default=None,
                   help="research|investigate|review|generate|refactor|"
                        "websearch|image|science (default: whatever the "
                        "capability triggers picked)")
    p.add_argument("--tool-calls", type=int, default=None,
                   help="how many tool calls doing it HERE would take; this is "
                        "the quantity the delegate/self decision turns on")
    p.add_argument("--file", action="append", default=None,
                   help="a file the delegate must look at (repeatable); made "
                        "absolute, because a relative path resolves against the "
                        "delegate's cwd, not yours")
    p.add_argument("--add-dir", action="append", default=None,
                   help="directory to add to the delegate's workspace "
                        "(repeatable)")
    p.add_argument("--stack", default=None)
    p.add_argument("--require", default=None,
                   help="pipe-separated numbered requirements")
    p.add_argument("--model", default=None,
                   help="one of `dobby agy models` — verbatim")
    p.add_argument("--effort", default=None, choices=["low", "medium", "high"])
    p.add_argument("--timeout", type=int, default=None,
                   help="seconds; becomes --print-timeout, and the process "
                        "ceiling is set above it so agy reports its own timeout")
    p.add_argument("--write", action="store_true",
                   help="allow the delegate to EDIT FILES (--mode accept-edits); "
                        "off by default")
    p.add_argument("--skip-permissions", action="store_true",
                   help="--dangerously-skip-permissions: auto-approve every tool "
                        "call the delegate makes, shell included")
    p.add_argument("--sandbox", action="store_true",
                   help="run the delegate with terminal restrictions")
    p.add_argument("--continue", dest="continue_conversation",
                   action="store_true", help="continue agy's most recent "
                                             "conversation")
    p.add_argument("--conversation", default=None, help="resume one by id")
    p.add_argument("--output-format", default="text",
                   choices=["text", "json", "stream-json"])
    p.add_argument("--json-schema", default=None,
                   help="schema string or path constraining the final result "
                        "(requires --output-format json)")
    p.add_argument("--agent", default=None, help="one of `dobby agy agents`")
    p.add_argument("--yes", action="store_true",
                   help="actually make the call (it costs money and leaves "
                        "the machine)")
    p.set_defaults(fn=cmd_agy)

    p = sub.add_parser("hwp", parents=[common],
                       help="read .hwp / .hwpx; edit .hwpx directly, .hwp via 한글")
    p.add_argument("action", choices=["info", "text", "paragraphs", "tables",
                                      "find", "replace", "set",
                                      "pages", "export", "shapes", "edit"])
    p.add_argument("file")
    p.add_argument("--text", default=None, help="needle, or the new text")
    p.add_argument("--with", dest="with_", default=None,
                   help="replacement for --text")
    p.add_argument("--index", type=int, default=None,
                   help="paragraph index for `set`")
    p.add_argument("--out", default=None,
                   help="destination; edits never overwrite the source")
    p.add_argument("--count", type=int, default=None,
                   help="stop after this many replacements")
    p.add_argument("--list", type=int, default=0,
                   help="text list for `shapes`/`edit`: 0 is the body; title "
                        "blocks, page headers and table cells have their own")
    p.add_argument("--dry", action="store_true",
                   help="`edit`: locate and verify every occurrence, write nothing")
    p.add_argument("--overwrite", action="store_true",
                   help="permit --out to replace an existing file")
    p.add_argument("--all", action="store_true",
                   help="include empty paragraphs in `paragraphs`")
    p.set_defaults(fn=cmd_hwp)

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

    p = sub.add_parser("runtime", parents=[common, net],
                       help="durable runs: plan -> execute -> verify -> report,"
                            " resumable after a crash")
    p.add_argument("action",
                   choices=["run", "resume", "status", "list", "events",
                            "trace", "metrics", "scorecard", "harvest",
                            "bench"])
    p.add_argument("task", nargs="?", default="",
                   help="the task (for `run`), or the run id (resume/status/"
                        "events)")
    p.add_argument("--provider", default=None,
                   help="agent CLI for the plan/execute/report nodes")
    p.add_argument("--execute", default=None,
                   help="a deterministic command for the execute node")
    p.add_argument("--check", default=None,
                   help="pipe-separated acceptance checks the verify node must "
                        "pass before anything is promoted")
    p.add_argument("--approve", default=None,
                   help="comma-separated node ids a human approves; required "
                        "before any EXTERNAL_IRREVERSIBLE node runs")
    p.add_argument("--allow-irreversible", type=int, default=0,
                   help="how many irreversible nodes this run may perform "
                        "(default 0: the right is granted, never inherited)")
    p.add_argument("--max-steps", type=int, default=100)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--parallel", type=int, default=1,
                   help="nodes to run at once; only helps a graph with a real "
                        "fan-out, so the default is sequential")
    p.add_argument("--otlp", action="store_true",
                   help="trace: emit OpenTelemetry JSON instead of a timeline")
    p.add_argument("--write", action="store_true",
                   help="harvest: persist the golden-task candidates")
    p.add_argument("--corpus", default=None,
                   help="bench: JSON file of tasks. Without it the example "
                        "SHAPE runs, whose numbers are not a result")
    p.add_argument("--no-route", action="store_true",
                   help="skip the router (budgets come from defaults)")
    p.set_defaults(fn=_runtime_dispatch)

    p = sub.add_parser("project", parents=[common, net],
                       help="the unit above a run: manifest, portfolio, "
                            "baseline, session envelope")
    p.add_argument("action",
                   choices=["init", "status", "list", "next", "attach-run",
                            "open", "close", "events", "run"])
    p.add_argument("work_item", nargs="?", default=None,
                   help="work item id for attach-run; session id for close")
    p.add_argument("run_id", nargs="?", default=None,
                   help="the runtime run id, for attach-run")
    p.add_argument("--project", default=None,
                   help="project id; optional while this store holds only one")
    p.add_argument("--root", default=None, help="init: repository to scan")
    p.add_argument("--smoke", default=None,
                   help="init: pipe-separated commands that prove the tree is "
                        "sound. Without it, a cheap default is derived and "
                        "recorded as derived")
    p.add_argument("--items", default=None,
                   help="init: JSON file of work item specs. Absent means an "
                        "empty portfolio — this command does not invent one")
    p.add_argument("--no-baseline", action="store_true",
                   help="init: skip the first baseline (it will be required "
                        "before any item may start)")
    p.add_argument("--rebaseline", action="store_true",
                   help="open: re-take the baseline instead of refusing")
    p.add_argument("--no-promote", action="store_true",
                   help="close: do not judge the active item by its run")
    p.add_argument("--until", choices=["item", "empty"], default="item",
                   help="run: stop after one item (default) or drain the "
                        "portfolio until a boundary only a person can cross")
    p.add_argument("--max-items", type=int, default=1,
                   help="run: hard ceiling on items in one invocation")
    p.add_argument("--max-steps", type=int, default=100,
                   help="run: node steps per work item")
    p.add_argument("--provider", default=None,
                   help="run: agent CLI that does the work")
    p.add_argument("--execute", default=None,
                   help="run: deterministic command that does the work")
    p.set_defaults(fn=cmd_project)
    return ap


def _runtime_dispatch(args):
    """`task` carries a run id for every action except `run`.

    One positional, because `dobby runtime resume <id>` and
    `dobby runtime run "<task>"` are the two things anybody types, and a
    required `--run-id` flag on one of them would be noise.
    """
    args.run_id = args.task
    if args.action == "run" and not args.task:
        _die("dobby runtime run needs a task: dobby runtime run \"<task>\"")
    if args.action in ("resume", "status", "events", "trace") and not args.run_id:
        _die(f"dobby runtime {args.action} needs a run id — "
             f"`dobby runtime list` shows them")
    cmd_runtime(args)


def main(argv=None) -> None:
    force_utf8_io()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
