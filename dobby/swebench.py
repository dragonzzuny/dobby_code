"""SWE-bench instances, run with the model held fixed and the harness varying.

WHAT THIS MEASURES, AND WHAT IT CANNOT

The definitive validation of a harness is `resolved` on SWE-bench: apply the
agent's patch, apply the instance's `test_patch`, run `FAIL_TO_PASS` and
`PASS_TO_PASS`, and count the instances where all of them pass. That requires a
per-instance environment with version-pinned dependencies, which in practice means
the official Docker images.

Measured on this machine: **Docker is absent and WSL is not installed.** So
`resolved` is NOT measured here and no number produced by this module may be
reported as a SWE-bench score. Saying that plainly is the point; a partial
reimplementation reported as SWE-bench would be worse than no result.

What IS measured, on real instances from the real dataset:

  * **Did the agent edit anything at all.** A run that produces no diff resolves
    nothing, and it is a common failure of an agent whose loop breaks quietly.
  * **File-level localization** against the gold patch. Necessary for resolution
    and not sufficient: editing the right file badly still fails the tests.
  * **Extra files touched** — files changed that the gold patch does not touch.

That third one is why this is worth running here rather than only waiting for
Docker. This harness makes a specific claim about scope discipline
(`.claude/rules/scope-and-integrity.md`), and a SWE-bench gold patch is ground
truth for which files the change *should* touch. It converts a claim about
behaviour into a count, on tasks nobody here wrote.

METHOD

Model, CLI and sandbox held fixed across conditions; the only variable is whether
the prompt carries the harness preamble. That is the design the harness-evaluation
literature requires — a comparison across scaffolds with the model fixed — and the
reason it matters is the size of the effect it isolates: Claw-SWE-Bench reports
19.1% against 73.4% Pass@1 on one backbone from adapter changes alone.

Each (instance, condition) pair gets its **own fresh clone**. Sharing one would let
the first condition's edits be visible to the second, which is not a subtle bias:
the second condition would be solving a different problem.

The write mode comes from the provider's own spec (`write_extra`), not from a
constant here. For codex that is `-s workspace-write`, which confines edits to the
clone; `danger-full-access` exists and is deliberately unused, because an agent
that can write anywhere is a liability rather than a measurement. A provider with
no established write mode is REFUSED - see `write_extra_for`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from typing import Callable, Sequence

DATASETS_SERVER = "https://datasets-server.huggingface.co"
DEFAULT_DATASET = "princeton-nlp/SWE-bench_Verified"

#: `+++ b/path` lines of a unified diff, which is what the gold patch is.
_DIFF_TARGET_RE = re.compile(r"^\+\+\+ b/(\S+)", re.M)

#: Carried into every report, because a reader has to know how much the agent was
#: allowed to touch before a localization number means anything.
AGENT_SANDBOX_NOTE = (
    "The agent runs under the provider's own confined write mode — for codex, "
    "`-s workspace-write`, which permits edits inside the clone and refuses "
    "outside it. `danger-full-access` exists and is deliberately unused: an agent "
    "that can write anywhere is a liability rather than a measurement, and a "
    "benchmark is exactly where that distinction gets forgotten.")


class SweBenchError(RuntimeError):
    """A precondition this module refuses to work around."""


def write_extra_for(provider_id: str) -> tuple[str, ...]:
    """The argv that lets this provider EDIT FILES, or a refusal.

    A first version hardcoded codex's `-s workspace-write` and passed it to
    whatever `--provider` named. Measured consequence, on the real catalog:

        codex    codex exec PROMPT -s workspace-write          correct
        claude   claude -p PROMPT --permission-mode plan -s workspace-write
        agy      agy --print PROMPT --mode plan -s workspace-write
        gemini   gemini -p PROMPT --approval-mode plan -s workspace-write

    `-s` is not a flag those tools have, and the ones that matter already carry
    their own READ-ONLY default — `--permission-mode plan`, `--mode plan`,
    `--approval-mode plan`. So a SWE-bench run with `--provider claude` would have
    reported zero edits and read as a harness failure, when the provider had simply
    never been permitted to write. A benchmark that silently measures a read-only
    agent is worse than one that does not run.

    The write mode now lives on the spec, and an empty `write_extra` is a refusal
    rather than "no flag needed". Only `codex` and `claude` have one, because those
    are the two whose write mode has actually been established here.
    """
    from .providers import registry

    spec = registry().get(provider_id)
    if spec.kind != "cli":
        raise SweBenchError(
            f"{provider_id} is an api provider; this benchmark needs a CLI that "
            f"edits a working tree")
    if not spec.write_extra:
        raise SweBenchError(
            f"no verified write mode for {provider_id!r}. Its catalog argv is "
            f"read-only by default, so it would produce no edits and the run "
            f"would look like a harness failure rather than a missing permission. "
            f"Establish the flag, record it as `write_extra` in "
            f"dobby/providers/catalog.py, then re-run.")
    return tuple(spec.write_extra)


#: The datasets-server caps one request at this many rows.
PAGE = 100


def fetch_instances(*, dataset: str = DEFAULT_DATASET, split: str = "test",
                    limit: int = PAGE, offset: int = 0) -> list[dict]:
    """Rows from the real dataset over the datasets-server HTTP API.

    Deliberately not via the `datasets` package: this repository depends on PyYAML
    and nothing else, and adding a multi-hundred-megabyte dependency tree to read
    500 JSON rows would be a poor trade. The rows are the published ones either
    way.

    Paginates to satisfy `limit`. A first version wrote `length={min(limit, 100)}`,
    so `fetch_instances(limit=250)` returned 100 rows and said nothing — measured,
    not hypothesised. Silent truncation in a sampling function is the worst place
    for it: every downstream rate is then computed over a subset nobody chose, and
    the number looks exactly as authoritative as a correct one.
    """
    collected: list[dict] = []
    while len(collected) < limit:
        want = min(PAGE, limit - len(collected))
        url = (f"{DATASETS_SERVER}/rows?dataset={dataset}&config=default"
               f"&split={split}&offset={offset + len(collected)}&length={want}")
        request = urllib.request.Request(url, headers={"User-Agent": "dobby"})
        with urllib.request.urlopen(request, timeout=120) as response:
            page = [row["row"] for row in json.load(response)["rows"]]
        if not page:
            break                       # end of split: fewer rows exist than asked
        collected += page
    return collected[:limit]


def find_instances(ids: Sequence[str], *, dataset: str = DEFAULT_DATASET,
                   split: str = "test", max_rows: int = 600
                   ) -> tuple[list[dict], list[str]]:
    """`(found, missing)` for explicit instance ids, scanning the whole split.

    The datasets-server caps a request at 100 rows, and requiring the caller to
    know which page an instance sits on is a defect rather than an interface: a
    first version rejected two valid ids with "not in the fetched pool", which
    tells the user about pagination instead of about their request. Stops as soon
    as everything asked for is found.
    """
    wanted = list(dict.fromkeys(ids))
    found: dict[str, dict] = {}
    for offset in range(0, max_rows, 100):
        rows = fetch_instances(dataset=dataset, split=split, limit=100,
                               offset=offset)
        if not rows:
            break
        for row in rows:
            if row["instance_id"] in wanted:
                found[row["instance_id"]] = row
        if len(found) == len(wanted):
            break
    ordered = [found[i] for i in wanted if i in found]
    return ordered, [i for i in wanted if i not in found]


def gold_files(patch: str) -> list[str]:
    """Files the reference patch modifies — the ground truth for localization."""
    return sorted(set(_DIFF_TARGET_RE.findall(patch or "")))


def changed_files(repo: str) -> list[str]:
    """Files the agent touched, including ones it created.

    `git status --porcelain` rather than `git diff --name-only`, because a new file
    is a change and `git diff` alone does not see an untracked one. An agent that
    solves an instance by adding a module would otherwise register as having done
    nothing.
    """
    # `-uall` is load-bearing. Plain `--porcelain` COLLAPSES an untracked
    # directory to a single `pkg/` entry and never names the files inside it, so an
    # agent that adds a module in a new package registers as having touched `pkg/`
    # — which matches no gold path, scores as a localization miss, and miscounts
    # extra files. Found by a test, not by reading: the assertion asked for
    # `pkg/deep/new.py` and got `['pkg/']`.
    proc = subprocess.run(["git", "-C", repo, "status", "--porcelain",
                           "--untracked-files=all"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    files: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:              # a rename reports both sides
            path = path.split(" -> ", 1)[1]
        files.add(path.replace("\\", "/"))
    return sorted(files)


def prepare_repo(instance: dict, dest: str, *, timeout_s: int = 900) -> str:
    """Clone the instance's repo at its `base_commit`. Returns the path.

    Blobless and single-commit: the agent needs the tree at one commit, not the
    project's history, and a full clone of these repositories is hundreds of
    megabytes each. This is the difference between a run that fits on this disk and
    one that does not.
    """
    os.makedirs(dest, exist_ok=True)
    repo_url = f"https://github.com/{instance['repo']}.git"
    subprocess.run(["git", "init", "--quiet", dest], check=True, timeout=120)
    subprocess.run(["git", "-C", dest, "remote", "add", "origin", repo_url],
                   check=True, timeout=120)
    subprocess.run(["git", "-C", dest, "fetch", "--quiet", "--depth", "1",
                    "--filter=blob:none", "origin", instance["base_commit"]],
                   check=True, timeout=timeout_s)
    subprocess.run(["git", "-C", dest, "checkout", "--quiet", "FETCH_HEAD"],
                   check=True, timeout=timeout_s)
    # A clean baseline is what makes `git status` afterwards mean "the agent did
    # this". Without it, checkout leftovers would count as agent edits.
    subprocess.run(["git", "-C", dest, "add", "-A"], check=True, timeout=120)
    subprocess.run(["git", "-C", dest, "-c", "user.email=eval@local",
                    "-c", "user.name=eval", "commit", "--quiet",
                    "-m", "baseline", "--allow-empty"], check=True, timeout=300)
    return dest


TASK_PROMPT = """Fix this issue in the repository you are in.

{problem}

Edit the files needed to fix it. Do not write tests. Report what you changed.
"""


def build_prompt(instance: dict, condition: str, repo_root: str) -> str:
    """`bare` is the issue; `harness` prepends what this harness supplies."""
    task = TASK_PROMPT.format(problem=instance["problem_statement"])
    if condition == "bare":
        return task
    from .endtask import harness_preamble
    preamble = harness_preamble(repo_root, {"prompt": instance["problem_statement"]})
    return f"{preamble}\n\n---\n\nTASK:\n{task}"


def score_instance(instance: dict, repo: str) -> dict:
    """Localization and scope, against the gold patch. Never `resolved`."""
    gold = gold_files(instance["patch"])
    changed = [f for f in changed_files(repo)
               # The agent is told not to write tests; a test file it adds anyway
               # is neither localization nor a scope violation of the fix, so it is
               # counted separately rather than silently folded into either.
               if not os.path.basename(f).startswith("test_")]
    gold_set, changed_set = set(gold), set(changed)
    hit = bool(gold_set) and gold_set <= changed_set
    extra = sorted(changed_set - gold_set)
    return {
        "gold_files": gold,
        "changed_files": changed,
        "made_any_edit": bool(changed_set),
        "localized_all_gold_files": hit,
        "localized_any_gold_file": bool(gold_set & changed_set),
        "extra_files": extra,
        "extra_file_count": len(extra),
        "precision": (round(len(gold_set & changed_set) / len(changed_set), 3)
                      if changed_set else 0.0),
        "recall": (round(len(gold_set & changed_set) / len(gold_set), 3)
                   if gold_set else 0.0),
        "resolved": None,
        "resolved_note": ("NOT MEASURED: resolution needs the instance's pinned "
                          "environment to run FAIL_TO_PASS/PASS_TO_PASS, which in "
                          "practice needs the official Docker images. Docker is "
                          "absent on this machine."),
    }


def run_instance(instance: dict, condition: str, *, workdir: str,
                 provider_id: str, repo_root: str, timeout_s: int = 900,
                 keep_clone: bool = False) -> dict:
    """One (instance, condition) trial in its own fresh clone."""
    from .providers import run_by_id

    clone = os.path.join(workdir, f"{instance['instance_id']}__{condition}")
    shutil.rmtree(clone, ignore_errors=True)
    record: dict = {"instance_id": instance["instance_id"],
                    "repo": instance["repo"], "condition": condition,
                    "provider": provider_id,
                    "difficulty": instance.get("difficulty")}
    started = time.monotonic()
    try:
        prepare_repo(instance, clone)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        record.update(ok=False, error=f"clone failed: {exc}",
                      duration_s=round(time.monotonic() - started, 2))
        return record

    prompt = build_prompt(instance, condition, repo_root)
    result = run_by_id(provider_id, prompt, cwd=clone, timeout_s=timeout_s,
                       extra=write_extra_for(provider_id))
    record["duration_s"] = round(time.monotonic() - started, 2)
    record["prompt_chars"] = len(prompt)
    record["ok"] = bool(result.ok)
    if not result.ok:
        record["error"] = result.error
    record.update(score_instance(instance, clone))
    if not keep_clone:
        shutil.rmtree(clone, ignore_errors=True)
    return record


def summarize(trials: Sequence[dict], *, conditions: Sequence[str]) -> dict:
    """Per-condition rates, with `resolved` absent rather than estimated."""
    import statistics

    per_condition = {}
    for condition in conditions:
        cell = [t for t in trials if t["condition"] == condition and t.get("ok")]
        n = len(cell)
        per_condition[condition] = {
            "trials": n,
            "made_any_edit": (round(sum(t["made_any_edit"] for t in cell) / n, 3)
                              if n else None),
            "localized_all_gold_files": (
                round(sum(t["localized_all_gold_files"] for t in cell) / n, 3)
                if n else None),
            "mean_extra_files": (round(statistics.fmean(
                t["extra_file_count"] for t in cell), 2) if n else None),
            "mean_precision": (round(statistics.fmean(
                t["precision"] for t in cell), 3) if n else None),
            "agent_seconds": round(sum(t.get("duration_s", 0.0)
                                       for t in trials
                                       if t["condition"] == condition), 1),
            "mean_prompt_chars": (round(statistics.fmean(
                t["prompt_chars"] for t in cell)) if n else None),
        }
    failures = [{"instance_id": t["instance_id"], "condition": t["condition"],
                 "error": (t.get("error") or "")[:200]}
                for t in trials if not t.get("ok")]
    return {
        "dataset": DEFAULT_DATASET,
        "instances": sorted({t["instance_id"] for t in trials}),
        "conditions": list(conditions),
        "per_condition": per_condition,
        "failed_trials": failures,
        "resolved_rate": None,
        "what_this_is_not": (
            "NOT a SWE-bench score. `resolved` requires running the instance's "
            "FAIL_TO_PASS and PASS_TO_PASS tests in its pinned environment, which "
            "needs the official Docker images; Docker is absent on this machine. "
            "These are localization and scope measurements on real instances, both "
            "necessary conditions for resolution and neither sufficient for it."),
        "sandbox": AGENT_SANDBOX_NOTE,
        "method": (
            "Model, CLI and sandbox held fixed; the only variable is the harness "
            "preamble. One fresh clone per (instance, condition), because sharing "
            "one would let the first condition's edits change the second's task."),
    }
