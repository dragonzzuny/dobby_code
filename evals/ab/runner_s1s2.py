"""Four arms for the decomposition question: three solo models, and dobby.

The earlier runners compared how much HARNESS a task got. This compares who does
the work, and whether deciding the split first is worth one expensive call:

    A_claude   one prompt, claude does everything
    B_codex    one prompt, codex does everything
    C_agy      one prompt, agy does everything, isolated because it may not run
               anywhere else
    D_dobby    the project loop on `--policy adaptive --architect`: claude plans
               at most once, codex implements, the verify is deterministic and
               the report is assembled rather than generated

The headline is CLAUDE TOKENS PER VERIFIED TASK, not total tokens. Total would
make agy look ruinous and codex look free, which is a statement about which
vendors report what — only claude reports a cost at all — rather than about
efficiency.

Every arm gets a fresh copy of the fixture tree, the same task string, the same
acceptance command, and the same immutable-test hash check. A pass that edited
the test is void for every arm equally.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))

from dobby.providers.policy import PlacementContext, ProviderPreferences  # noqa: E402
from dobby.providers.run import recording  # noqa: E402
from dobby.providers.usage import roll_up  # noqa: E402

from runner import fingerprint, fresh_tree, run_check, tampered  # noqa: E402
from smoke_providers import git_views, item_for, profile_for, views_identical  # noqa: E402

ARM_CLAUDE = "A_claude"
ARM_CODEX = "B_codex"
ARM_AGY = "C_agy"
ARM_DOBBY = "D_dobby"
ARMS = (ARM_CLAUDE, ARM_CODEX, ARM_AGY, ARM_DOBBY)

SOLO = {ARM_CLAUDE: "claude", ARM_CODEX: "codex", ARM_AGY: "agy"}


def _row(task, arm, *, calls, wall, verified, gamed, state, note="",
         extra=None):
    record = roll_up(calls)
    claude = (record["providers"].get("claude") or {}).get("usage") or {}
    return {
        # `task_id`, not `task`: the shared `paired_tasks` reads that name, and
        # a mismatch here cost 48 real provider calls on 2026-08-23 — every run
        # finished and the aggregation crashed before writing any of them.
        "task_id": task.task_id,
        "stratum": "S1" if task.one_shot_plausible else "S2",
        "arm": arm,
        "verified": bool(verified) and not gamed,
        "state": state,
        "wall_s": round(wall, 1),
        "record": record,
        # Broken out because it is the resource the operator is actually short
        # of, and because burying it in a per-provider dict makes the headline
        # unreadable.
        "claude_calls": (record["providers"].get("claude") or {}).get(
            "calls_total", 0),
        "claude_input_tokens": claude.get("input_tokens"),
        "claude_output_tokens": claude.get("output_tokens"),
        "claude_thinking_tokens": claude.get("thinking_tokens"),
        "claude_cost_usd": claude.get("cost_usd"),
        "evaluation_gaming": gamed,
        "note": note,
        **(extra or {}),
    }


def run_solo(task, root, arm, *, timeout_s=900) -> dict:
    """One provider, one prompt, the check run once afterwards."""
    from dobby.providers.catalog import registry
    from dobby.providers.run import run_provider

    provider = SOLO[arm]
    spec = registry().get(provider)
    guard = fingerprint(root, task.immutable)
    extra = tuple(spec.write_extra)
    if arm == ARM_AGY:
        extra += tuple(spec.isolated_extra)

    started = time.monotonic()
    with recording() as calls:
        result = run_provider(spec, task.prompt, cwd=root, extra=extra,
                              timeout_s=timeout_s)
    wall = time.monotonic() - started

    verified = run_check(root, task.check)
    gamed = tampered(guard, fingerprint(root, task.immutable))
    return _row(task, arm, calls=calls, wall=wall, verified=verified,
                gamed=gamed, state=("ok" if result.ok else "provider_failed"),
                note=("" if result.ok else (result.error or "")[:200]))


def run_dobby(task, root, store_dir, *, timeout_s=900) -> dict:
    """The project loop: adaptive policy, architect allowed, roles routed.

    `--architect` is what lets an S2 item buy a plan. On an S1 item the adaptive
    policy sends it straight to the fast path and no architect is called, which
    is the behaviour the S1 stratum exists to price.
    """
    from dobby.project import ProjectStore, initialise
    from dobby.project import loop as L

    guard = fingerprint(root, task.immutable)
    started = time.monotonic()
    with recording() as calls:
        initialise(store_dir, root,
                   smoke=('{python} -c "import sys; sys.exit(0)"',),
                   item_specs=[{"outcome": task.prompt,
                                "acceptance_checks": [task.check],
                                "expected_paths": list(task.expected_paths),
                                # The corpus recorded this before anything ran.
                                # Without it an S2 fixture touching three files
                                # scores as "scoped", takes the fast path, and
                                # the decomposition under test never happens.
                                "one_shot_plausible": task.one_shot_plausible,
                                "uncertainty": 0}],
                   run_baseline=True)
        # A provider MUST be named or `direct_gated_graph` builds a static node
        # and the arm makes no call at all — measured: twelve D rows, zero
        # provider calls, every one recorded as a failure. What is named here is
        # only "there is a provider"; `ProviderPlacement` then routes by ROLE,
        # so the plan node still goes to claude and the implement node to codex.
        result = L.advance(store_dir, policy="adaptive", architect=True,
                           compile_plans=True, max_steps=6, provider="codex")
    wall = time.monotonic() - started

    item = ProjectStore(store_dir).load_project(None)["portfolio"].get("W001")
    gamed = tampered(guard, fingerprint(root, task.immutable))
    shapes = [i.get("graph", "") for i in result.get("iterations", [])]
    return _row(task, ARM_DOBBY, calls=calls, wall=wall,
                verified=(item.state == "DONE"), gamed=gamed,
                state=result["stopped"],
                note=(result.get("detail") or "")[:200],
                extra={"graph_shapes": shapes,
                       "planned_by": item.planned_by,
                       "blocked_reason": item.blocked_reason or ""})


def run_arm(task, base, arm, *, timeout_s=900) -> dict:
    root = fresh_tree(base, task, arm)
    store = os.path.join(base, f".store-{task.task_id}-{arm}")

    if arm == ARM_AGY:
        from dobby.project.workspace import isolated as isolate_tree

        for args in (("init", "-q"), ("add", "-A")):
            subprocess.run(["git", "-C", root, *args], capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "fx"],
                       capture_output=True)
        before = git_views(root)
        with isolate_tree(root) as (tree, why):
            if tree is None:
                return {"task_id": task.task_id, "arm": arm, "void": True,
                        "note": f"isolation unavailable: {why}"}
            row = run_solo(task, tree, arm, timeout_s=timeout_s)
        unchanged, differing = views_identical(before, git_views(root))
        row["original_root_unchanged"] = unchanged
        row["views_that_differ"] = differing
        return row

    if arm == ARM_DOBBY:
        return run_dobby(task, root, store, timeout_s=timeout_s)
    return run_solo(task, root, arm, timeout_s=timeout_s)


def run(corpus, *, base: str, seed: int = 20260823, timeout_s: int = 900,
        on_step=None, journal: str | None = None) -> dict:
    """Every (task, arm), in a randomised order, PERSISTED AS THEY COMPLETE.

    The journal is append-only JSONL and it is not an optimisation. The first
    attempt at this run held 48 finished rows in memory and wrote once at the
    end; the aggregation raised a KeyError and an hour of real provider calls
    went with it. A row costs a minute and a model call, so it is written the
    moment it exists.
    """
    pairs = [(task, arm) for task in corpus for arm in ARMS]
    random.Random(seed).shuffle(pairs)

    def append(row):
        if not journal:
            return
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    rows = []
    for index, (task, arm) in enumerate(pairs, start=1):
        if on_step:
            on_step(index, len(pairs), task.task_id, arm)
        try:
            row = run_arm(task, base, arm, timeout_s=timeout_s)
            rows.append(row)
            append(row)
        except Exception as exc:                 # noqa: BLE001
            row = {"task_id": task.task_id, "arm": arm, "void": True,
                   "note": f"{type(exc).__name__}: {exc}"[:300]}
            rows.append(row)
            append(row)

    from runner import paired_tasks

    rows = mark_void_solo(rows)
    # Aggregation happens AFTER the rows are safe on disk, and its failure is
    # reported rather than raised: a summary is derived data and losing it must
    # not lose the measurements it was derived from.
    try:
        complete, dropped = paired_tasks(rows, arms=ARMS)
        aggregation_error = ""
    except Exception as exc:                      # noqa: BLE001
        complete, dropped = [], []
        aggregation_error = f"{type(exc).__name__}: {exc}"

    return {"seed": seed, "arms": list(ARMS), "rows": rows,
            "paired_tasks": complete,
            "aggregation_error": aggregation_error,
            "dropped_tasks": [{"task_id": t, "void_arms": a}
                              for t, a in dropped],
            "note": ("only paired_tasks may be compared across arms; a task any "
                     "arm failed to run is void for all of them")}


def mark_void_solo(rows: list) -> list:
    """A row whose arm made no provider call never ran.

    The pilot's `mark_void` reads `provider_calls`; these rows carry the richer
    `record`, so the same rule is applied to that. Same reasoning: an absence
    scored as a loss credits whichever arm the provider happened not to fail on.
    """
    for row in rows:
        if row.get("void"):
            continue
        total = (row.get("record") or {}).get("calls_total", 0)
        if total == 0:
            row["void"] = True
            row["note"] = ("no provider call was recorded: this arm never ran. "
                           + (row.get("note") or ""))[:400]
    return rows


def main(base: str, out: str) -> None:
    from corpus_s1_s2 import corpus

    tasks = corpus()
    os.makedirs(base, exist_ok=True)

    def step(i, n, task_id, arm):
        print(f"[{i}/{n}] {task_id} :: {arm}", flush=True)

    journal = out.replace(".json", ".jsonl")
    payload = run(tasks, base=base, on_step=step, journal=journal)
    print("journal:", journal, flush=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
    print("paired:", payload["paired_tasks"], flush=True)
    print("dropped:", payload["dropped_tasks"], flush=True)
    print("WROTE", out, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
