"""The four real-provider smokes, run before any claim about codex or agy.

Every number this repository has about provider behaviour came from claude,
because claude is the only one that had ever been run through the harness. The
role policy names codex the default implementer and agy an isolated delegate on
the strength of what their catalogs SAY, and a policy resting on documentation is
the thing this session keeps finding to be wrong.

So: four smokes, separated so a failure names its own boundary rather than
discrediting the policy wholesale.

    1  codex direct-gated     does the default implementer actually implement,
                              and is claude's call count zero
    2  agy isolated delegate  does the original tree survive, byte for byte
    3  claude cap             does a cap of zero launch zero processes
    4  policy normal path     with no override, does the trace show the policy

A failure here does not widen a fallback. It is classified as permission,
effect-not-observed, schema, acceptance or isolation, and the boundary that
failed is what gets fixed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

from dobby.project.execution_policy import TaskProfile
from dobby.project.fastpath import direct_gated_graph
from dobby.project.models import WorkItem
from dobby.providers.policy import (PlacementContext, ProviderCap,
                                    ProviderPreferences)
from dobby.providers.run import recording
from dobby.providers.usage import roll_up
from dobby.runtime.runner import Runner
from dobby.runtime.scheduler import RunBudget

sys.path.insert(0, os.path.dirname(__file__))
from corpus_pilot import pilot_corpus          # noqa: E402
from runner import fingerprint, fresh_tree, run_check, tampered  # noqa: E402


def git_views(root: str) -> dict:
    """The three views a reviewer asked for, plus a content manifest.

    A HEAD comparison is not enough and neither is any one of these alone:
    `git diff` misses staged changes, `--cached` misses unstaged ones, and both
    miss untracked files entirely — which is the shape a delegate's output most
    often takes. `--untracked-files=all` catches those, and the content manifest
    catches a file rewritten to the same length with different bytes.
    """
    def run(*args):
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        return (proc.stdout or "").strip()

    return {
        "head": run("rev-parse", "HEAD"),
        "diff": run("diff", "--no-ext-diff"),
        "diff_cached": run("diff", "--cached", "--no-ext-diff"),
        "porcelain": run("status", "--porcelain", "--untracked-files=all"),
        "content_manifest": tree_hash(root),
    }


def views_identical(before: dict, after: dict) -> tuple:
    """`(unchanged, differing_views)` — named, not counted."""
    differing = sorted(k for k in before if before[k] != after.get(k))
    return (not differing), differing


def tree_hash(root: str) -> str:
    """Content hash of every file under `root`, for the isolation assertion.

    Comparing HEAD is not enough: agy writing an untracked file into the
    protected tree would leave HEAD identical. This walks content.
    """
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "__pycache__", ".dobby", ".omc"}]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            digest.update(rel.encode("utf-8"))
            try:
                with open(path, "rb") as fh:
                    digest.update(fh.read())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()


def item_for(task) -> WorkItem:
    return WorkItem(work_item_id="W001", project_id="smoke", title=task.task_id,
                    outcome=task.prompt, acceptance_checks=[task.check],
                    expected_paths=list(task.expected_paths))


def profile_for(task) -> TaskProfile:
    return TaskProfile(one_shot_plausible=True, acceptance_declared=True,
                       expected_paths=tuple(task.expected_paths),
                       side_effect_class="LOCAL_WRITE")


def run_once(task, root, *, provider, isolated=False, caps=None,
             override=None, timeout_s=600, data_dir=None,
             worker="provider") -> dict:
    """One gated node, one provider, everything recorded."""
    item, profile = item_for(task), profile_for(task)
    # `provider=None` with a provider WORKER is the policy path: the node names
    # a role and nobody names a CLI, so `ProviderPlacement.choose` decides. The
    # first version passed provider=None straight through, which built a static
    # node and measured nothing at all.
    graph = direct_gated_graph(item, profile, provider=provider or "unset",
                               timeout_s=timeout_s)
    if provider is None:
        node = graph.nodes["execute"]
        node.config = {k: v for k, v in node.config.items() if k != "provider"}
        node.config["provider_role"] = "implement"
    prefs = ProviderPreferences(caps=caps) if caps is not None else \
        ProviderPreferences()
    context = PlacementContext(isolated=isolated, original_root=root,
                               preferences=prefs)
    guard = fingerprint(root, task.immutable)

    # The run store goes OUTSIDE the tree under test. Writing it inside made
    # `.dobby/state/runtime/runs.sqlite3` and `__pycache__` show up in the
    # changed-path manifest, so the harness was measuring its own bookkeeping as
    # if the delegate had produced it.
    store_dir = data_dir or os.path.join(os.path.dirname(root.rstrip("/\\")),
                                         f".store-{os.path.basename(root)}")
    started = time.monotonic()
    with recording() as calls:
        runner = Runner(root, data_dir=store_dir,
                        sleep=lambda _s: None,
                        placement_context=context,
                        override_provider=override)
        run_id = runner.start(task.prompt, graph)
        result = runner.run(run_id, budget=RunBudget(max_attempts=3))
    wall = time.monotonic() - started

    by_provider: dict = {}
    for call in calls:
        by_provider[call.provider] = by_provider.get(call.provider, 0) + 1

    node = result.steps[0] if result.steps else None
    gamed = tampered(guard, fingerprint(root, task.immutable))
    return {
        "task": task.task_id,
        "run_id": run_id,
        "state": result.state,
        "verified": result.state == "SUCCEEDED" and not gamed,
        "acceptance_pass": run_check(root, task.check),
        "calls_total": len(calls),
        "calls_by_provider": by_provider,
        "claude_calls": by_provider.get("claude", 0),
        "wall_s": round(wall, 1),
        "selected_provider": graph.nodes["execute"].config.get(
            "selected_provider"),
        "failure": (node.failure or {}) if node else {},
        "evaluation_gaming": gamed,
        # The unified record: per-provider calls_total / succeeded / failed and
        # summed usage with its own denominator, so three CLIs that report three
        # different subsets are still comparable on what they all report.
        "record": roll_up(calls),
    }


def smoke_codex(base: str, corpus) -> dict:
    """1. The default implementer, on the original tree, with claude at zero."""
    rows = []
    for task in corpus:
        root = fresh_tree(base, task, "smoke_codex")
        rows.append(run_once(task, root, provider="codex",
                             override="codex"))
    return {"smoke": "codex_direct_gated", "rows": rows,
            "claude_calls_total": sum(r["claude_calls"] for r in rows),
            "verified": sum(1 for r in rows if r["verified"]),
            "n": len(rows)}


def smoke_agy_isolated(base: str, corpus) -> dict:
    """2. The delegate, and the assertion that matters: the tree did not move."""
    from dobby.project.workspace import changed_paths, isolated as isolate_tree

    rows = []
    for task in corpus[:2]:
        root = fresh_tree(base, task, "smoke_agy")
        subprocess.run(["git", "-C", root, "init", "-q"], capture_output=True)
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", root, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "fixture"],
                       capture_output=True)
        before = git_views(root)

        with isolate_tree(root) as (tree, why):
            if tree is None:
                rows.append({"task": task.task_id, "skipped": why})
                continue
            row = run_once(task, tree, provider="agy", isolated=True,
                           override="agy")
            manifest = changed_paths(tree)
            row["changed_paths"] = list(manifest.paths)
            row["worktree"] = tree
        unchanged, differing = views_identical(before, git_views(root))
        row["original_root_unchanged"] = unchanged
        row["views_that_differ"] = differing
        row["isolation_checks"] = sorted(before)
        rows.append(row)

    return {"smoke": "agy_isolated_delegate", "rows": rows,
            "original_root_mutations": sum(
                1 for r in rows
                if r.get("original_root_unchanged") is False),
            "n": len(rows)}


def smoke_claude_cap(base: str, corpus) -> dict:
    """3. A cap of zero must launch zero claude processes, not fall back."""
    task = corpus[0]
    root = fresh_tree(base, task, "smoke_cap")
    row = run_once(task, root, provider="claude",
                   caps={"claude": ProviderCap(max_calls=0)})
    return {"smoke": "claude_cap_zero", "row": row,
            "claude_calls": row["claude_calls"],
            "held": row["claude_calls"] == 0}


def smoke_policy_path(base: str, corpus) -> dict:
    """4. No override at all — does the trace show the policy's own choice."""
    task = corpus[0]
    root = fresh_tree(base, task, "smoke_policy")
    row = run_once(task, root, provider=None, override=None)
    return {"smoke": "policy_normal_path", "row": row,
            "selected": row["selected_provider"],
            "claude_calls": row["claude_calls"]}


def compare_providers(base: str, corpus, providers=("claude", "codex", "agy")
                      ) -> dict:
    """The same three fixtures through each provider, one gated call each.

    Same harness, same gate, same acceptance command, fresh tree per provider.
    The earlier comparison put claude's numbers from a different run beside
    codex's from this one, which measures the harness as much as the provider.

    agy runs isolated because it may not run anywhere else; that is a real
    asymmetry and it is reported rather than hidden, since isolation costs a
    worktree creation the other two do not pay.
    """
    from dobby.project.workspace import isolated as isolate_tree

    out: dict = {}
    for provider in providers:
        rows = []
        for task in corpus:
            root = fresh_tree(base, task, f"cmp_{provider}")
            if provider == "agy":
                subprocess.run(["git", "-C", root, "init", "-q"],
                               capture_output=True)
                subprocess.run(["git", "-C", root, "add", "-A"],
                               capture_output=True)
                subprocess.run(["git", "-C", root, "-c", "user.email=t@t",
                                "-c", "user.name=t", "commit", "-qm", "fx"],
                               capture_output=True)
                before = git_views(root)
                with isolate_tree(root) as (tree, why):
                    if tree is None:
                        rows.append({"task": task.task_id, "skipped": why})
                        continue
                    row = run_once(task, tree, provider=provider,
                                   isolated=True, override=provider)
                unchanged, differing = views_identical(before, git_views(root))
                row["original_root_unchanged"] = unchanged
                row["views_that_differ"] = differing
            else:
                row = run_once(task, root, provider=provider,
                               override=provider)
            rows.append(row)
        out[provider] = {"provider": provider, "rows": rows}
    return out


def main(base: str, out: str) -> None:
    corpus = pilot_corpus()
    os.makedirs(base, exist_ok=True)
    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if os.environ.get("SMOKE_COMPARE"):
        report["compare"] = compare_providers(base, corpus)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
        print("WROTE", out, flush=True)
        return

    for name, fn in (("codex", smoke_codex),
                     ("agy", smoke_agy_isolated),
                     ("cap", smoke_claude_cap),
                     ("policy", smoke_policy_path)):
        print(f"=== smoke: {name}", flush=True)
        try:
            report[name] = fn(base, corpus)
        except Exception as exc:                # noqa: BLE001
            report[name] = {"smoke": name, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(report[name], ensure_ascii=False, default=str)[:900],
              flush=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
    print("WROTE", out, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
