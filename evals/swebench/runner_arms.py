"""Five arms on real SWE-bench instances, priced on four token axes.

Why this replaces evals/ab
--------------------------
The S1/S2 corpus wrote its own fixtures, and they averaged 928 bytes — 232
tokens. One claude call costs about 30,000 tokens before it is told anything
(22,565 of system prompt with every tool disabled, plus up to 7,603 of tool
schemas; both measured on this machine 2026-08-24). So the fixed overhead was
129x the task, cost was `calls x 30,000`, and a decomposing arm that makes five
calls where a solo arm makes one loses by five to one ARITHMETICALLY. It did:
3.9x to 6.0x, measured. No change to the loop can win that, and no conclusion
about decomposition can be drawn from it.

These instances are real repositories at a real commit — astropy and django,
about 45 MB and 1,900 files each. The agent has to FIND the code before it can
change it, which is the work the old corpus skipped by handing over a 600-byte
tree. That is also where a plan can pay: `prefix_reread` is the tokens an
agentic loop spends re-reading its own context while it explores, and on the old
corpus it was already the largest axis for every arm.

What is scored, and by whom
---------------------------
Not `resolved`. Resolution needs the instance's pinned environment to run
FAIL_TO_PASS and PASS_TO_PASS, which needs the official Docker images; Docker,
podman and a WSL distro are all absent here, re-measured this session. Anything
in this file reported as a SWE-bench score would be a lie.

What is scored is FILE-LEVEL LOCALIZATION against the gold patch —
`swebench.score_instance` — which is graded rather than pass/fail: precision,
recall, and the count of files touched that the gold patch does not touch. Every
arm is graded by the same rubric and no arm can see it. The D arm's own
acceptance check (`check_syntax.py`) decides only when its loop stops, never what
it scored; keeping those apart is what stops the decomposing arm from grading its
own homework.

Clone economics
---------------
One network clone per INSTANCE, then a local copy per arm. Measured 14.9s and
15.7s for two astropy instances; five network clones per instance would spend
that five times over and hammer github for the same bytes.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")))

from dobby.providers.run import recording, run_by_id  # noqa: E402
from dobby.providers.usage import roll_up  # noqa: E402
from dobby.providers.usage_axes import axes_for_record  # noqa: E402
from dobby.swebench import (fetch_instances, gold_files, prepare_repo,  # noqa: E402
                            score_instance, write_extra_for)

import billing  # noqa: E402

ARM_CLAUDE = "A_claude"
ARM_CODEX = "B_codex"
ARM_AGY = "C_agy"
ARM_DOBBY = "D_dobby"
ARM_FABLE = "E_fable"

ARMS = (ARM_CLAUDE, ARM_CODEX, ARM_AGY, ARM_FABLE, ARM_DOBBY)

#: arm -> (provider id, model). The model is how the Fable arm exists at all: it
#: is the same CLI and the same argv as A_claude with `--model` appended, so a
#: difference between those two rows is the MODEL and nothing else.
#:
#: EVERY arm names its model, including the ones whose CLI would pick a default,
#: because two of these CLIs do not say which model they used and a token count
#: belonging to no model cannot be read. Discovered 2026-08-24 rather than
#: assumed:
#:
#:   codex  `codex exec` prints `model: gpt-5.6-sol` in its human header; the
#:          `--json` stream omits it entirely, which is how B_codex's 375,644
#:          tokens came to belong to no named model.
#:   agy    `~/.gemini/antigravity-cli/settings.json` holds
#:          `"model": "Gemini 3.5 Flash (High)"`. `agy models` also offers
#:          `claude-sonnet-4-6` and `claude-opus-4-6-thinking`, so an unpinned
#:          agy arm might have been running Claude and the row could not say so.
#:
#: These are the DEFAULTS this machine was already using, written down. Changing
#: one changes what the arm measures, so it is changed here and nowhere else.
SOLO = {
    ARM_CLAUDE: ("claude", None),          # the CLI reports its own model
    ARM_CODEX: ("codex", "gpt-5.6-sol"),
    ARM_AGY: ("agy", "gemini-3.5-flash-high"),
    ARM_FABLE: ("claude", "claude-fable-5"),
}

TASK_PROMPT = """Fix this issue in the repository you are in.

{problem}

Edit the files needed to fix it. Do not write tests. Report what you changed.
"""

CHECK_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "check_syntax.py")


# -- clone cache -------------------------------------------------------------

def instance_template(instance: dict, cache: str, *, timeout_s: int = 900) -> str:
    """Clone once per instance; later arms copy it. Returns the template path."""
    dest = os.path.join(cache, instance["instance_id"])
    marker = os.path.join(dest, ".dobby-template-ready")
    if os.path.exists(marker):
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    prepare_repo(instance, dest, timeout_s=timeout_s)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(instance["base_commit"])
    return dest


def _remove_tree(path: str, *, attempts: int = 3) -> None:
    """Delete `path`, and say so plainly if it is still there afterwards.

    `shutil.rmtree(ignore_errors=True)` swallowed a locked leftover from a
    killed run, git then refused to clone into the surviving directory, and the
    error the runner printed was an empty string — the failure had nothing to do
    with git and git therefore had nothing to say about it. Windows holds
    handles open for a moment after a process dies, so a retry usually clears
    it; what must not happen is proceeding as though the directory were gone.
    """
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(1.0 + attempt)
    raise RuntimeError(
        f"{path} still exists after {attempts} removal attempts; something is "
        f"holding it open. A leftover from a killed run is the usual cause — "
        f"delete it and rerun rather than scoring into a dirty tree")


def fresh_clone(template: str, dest: str) -> str:
    """A local clone of the template. Returns the path.

    Measured on this machine, one astropy instance (1,904 files, 45 MB):

        git clone --local   5.3s
        git clone --shared  5.8s
        shutil.copytree     6.7s
        network clone      14.9s

    `--local` is asked for because it hardlinks the object store instead of
    copying it — but `prepare_repo` fetches at depth 1, and git answers
    "source repository is shallow, ignoring --local" and copies anyway. It is
    kept because it costs nothing when it does not apply and pays when the
    template is ever made non-shallow. The measured django figure with it
    ignored is 17.3s for 6,138 files, still faster than fetching from github.

    What it does buy unconditionally is that `dest` is a repository in its own
    right, which `score_instance` needs: it reads `git status` to find what the
    agent touched.
    """
    _remove_tree(dest)
    proc = subprocess.run(["git", "clone", "--quiet", "--local",
                           template, dest],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    if proc.returncode != 0:
        # Both streams and the code. A previous version reported stderr alone
        # and printed an empty string, because the failure was a leftover
        # destination from a killed run rather than anything git had to say.
        raise RuntimeError(
            f"local clone of {template} -> {dest} failed with rc="
            f"{proc.returncode}; stderr={(proc.stderr or '').strip()[:300]!r} "
            f"stdout={(proc.stdout or '').strip()[:200]!r}")
    _exclude_harness_artefacts(dest)
    return dest


#: Directories that agent harnesses write into their working directory. None of
#: them is ever part of a SWE-bench gold patch, and every one of them is the
#: measuring instrument rather than the thing measured.
HARNESS_ARTEFACTS = (".omc/", ".dobby/", ".claude/", ".codex/", ".antigravity/")


def _exclude_harness_artefacts(repo: str) -> None:
    """Make the clone ignore what the harnesses write into it.

    MEASURED, on the first pilot run: A_claude edited exactly the five gold
    files and scored precision 0.556, because four `.omc/` session-state files
    counted as scope violations. D_dobby scored recall 0.0 and was rejected with
    a ReadOnlyViolation whose entire evidence was five `.omc/` files — the
    architect never touched the source, and its plan was thrown away for noise
    the measuring apparatus made.

    Gitignore is the right lever rather than a filter in the scorer, because it
    is the lever the code already relies on: `readonly.fingerprint` is
    `repo_digest`, which is HEAD plus porcelain status, and `readonly`'s own
    docstring explains that `.dobby/state/` stays invisible to it by being
    gitignored. In the dobby repository that is true; in a fresh django clone
    nobody has told git anything. `.git/info/exclude` is local to this clone and
    is never committed, so nothing about the instance's own tree changes.
    """
    path = os.path.join(repo, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n# added by evals/swebench/runner_arms.py: harness state is\n"
                 "# the instrument, not the change under measurement\n")
        for entry in HARNESS_ARTEFACTS:
            fh.write(entry + "\n")


# -- rows --------------------------------------------------------------------


def _reported_model(calls) -> list:
    """Models the PROVIDERS named, as opposed to the one this runner pinned.

    Kept beside `model_pinned` rather than merged with it: when a CLI reports a
    model, the two agreeing is evidence the pin took effect, and them differing
    is a finding. Merging them would hide exactly the case worth seeing.
    """
    seen = []
    for result in calls:
        named = (getattr(result, "usage", None) or {}).get("model")
        if named and named not in seen:
            seen.append(named)
    return seen


def _row(instance, arm, *, calls, wall, score, state, note="", extra=None):
    record = roll_up(calls)
    split = axes_for_record(record)
    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "difficulty": instance.get("difficulty"),
        "gold_file_count": len(gold_files(instance["patch"])),
        "arm": arm,
        "state": state,
        "wall_s": round(wall, 1),
        # Graded, not pass/fail. `localized_all_gold_files` is the strict form
        # and `recall` is the partial credit; both are reported because an arm
        # that finds two of four files has done something a boolean erases.
        **{k: v for k, v in score.items() if k != "resolved_note"},
        "axes": split["totals"],
        "axes_complete": split["complete"],
        "axes_note": split["note"],
        "calls_total": split["calls"],
        "record": record,
        "note": note,
        **(extra or {}),
    }


#: Belt to the gitignore's braces. `DISABLE_OMC` is the documented kill switch
#: for the orchestration layer this session happens to run under, and an eval
#: that lets its own wrapper write into the tree under test is measuring the
#: wrapper. Ignored harmlessly by any CLI that has never heard of it.
CLEAN_CHILD_ENV = {"DISABLE_OMC": "1", "OMC_SKIP_HOOKS": "all"}

#: Codex streams JSON events and the usage envelope is the LAST one. On a django
#: instance the default cap truncated the stream and the run recorded
#: `calls_measured: 0` — a real call whose tokens are simply unknown. Raised so
#: the measurement survives a verbose provider.
OUTPUT_CAP = 400_000


def run_solo(instance, root, arm, *, isolated=False, timeout_s=1800) -> dict:
    from dobby.providers.catalog import registry

    provider, model = SOLO[arm]
    spec = registry().get(provider)
    prompt = TASK_PROMPT.format(problem=instance["problem_statement"])

    # Three things beyond the prompt, and agy needs all three where claude and
    # codex need only the first:
    #   write_extra      may it edit at all
    #   workspace        WHICH tree — agy ignores cwd (see catalog, agy spec)
    #   isolated_extra   may it run commands without a human approving each one.
    #                    Measured 2026-08-24: with --add-dir but without this,
    #                    agy located the right files and then died on
    #                    "permission check failed for command
    #                    'python tests/runtests.py mail'".
    extra = tuple(write_extra_for(provider)) + spec.workspace(root)
    if isolated:
        extra += tuple(spec.isolated_extra)

    started = time.monotonic()
    with recording() as calls:
        result = run_by_id(provider, prompt, cwd=root, model=model,
                           extra=extra,
                           env_extra=CLEAN_CHILD_ENV,
                           output_cap=OUTPUT_CAP,
                           timeout_s=timeout_s)
    wall = time.monotonic() - started
    return _row(instance, arm, calls=calls, wall=wall,
                score=score_instance(instance, root),
                state=("ok" if result.ok else "provider_failed"),
                note=("" if result.ok else (result.error or "")[:300]),
                extra={"model_pinned": model,
                       "model_reported": _reported_model(calls),
                       "billing": billing.summarise(roll_up(calls))})


def run_dobby(instance, root, store_dir, *, timeout_s=1800) -> dict:
    """The project loop: adaptive policy, architect allowed, roles routed.

    `expected_paths` is left EMPTY on purpose. The gold patch names the files
    that should change and handing that to the arm would be handing it the
    answer to the thing being scored — the other four arms get the issue text
    and nothing else, and so does this one.
    """
    from dobby.project import ProjectStore, initialise
    from dobby.project import loop as L

    started = time.monotonic()
    with recording() as calls:
        initialise(store_dir, root,
                   smoke=('{python} -c "import sys; sys.exit(0)"',),
                   item_specs=[{
                       "outcome": TASK_PROMPT.format(
                           problem=instance["problem_statement"]),
                       "acceptance_checks": [f'{{python}} "{CHECK_SCRIPT}"'],
                       "expected_paths": [],
                       # A real issue across an unknown number of files in a
                       # 1,900-file tree is the case the decomposition is FOR.
                       "one_shot_plausible": False,
                       "uncertainty": 2}],
                   run_baseline=True)
        # `isolate=True` is what the design intends and what the first pilot
        # left off. Two things follow from it and neither is cosmetic:
        #
        #   the merge gate    changes come back only through `workspace.merge`,
        #                     checked against the plan's declared write_set
        #   agy becomes admissible  `policy.admissible` refuses agy for every
        #                     role while `isolated` is false, because agy was
        #                     measured writing under a read-only argv. With a
        #                     worktree it is a candidate for scout, implement
        #                     and critic — so the arm can actually route to the
        #                     third provider instead of only claude and codex.
        #
        # The plan supplies the write_set this needs: measured 2026-08-24, the
        # architect declared `django/utils/encoding.py`,
        # `django/core/mail/utils.py` and `django/core/mail/message.py`, all
        # three of them gold files it found by itself.
        result = L.advance(store_dir, policy="adaptive", architect=True,
                           compile_plans=True, max_steps=8, provider="codex",
                           isolate=True)
    wall = time.monotonic() - started

    item = ProjectStore(store_dir).load_project(None)["portfolio"].get("W001")
    return _row(instance, ARM_DOBBY, calls=calls, wall=wall,
                score=score_instance(instance, root),
                state=result["stopped"],
                note=(result.get("detail") or "")[:300],
                extra={"model_pinned": "(routed by role)",
                       "model_reported": _reported_model(calls),
                       "billing": billing.summarise(roll_up(calls)),
                       "loop_state": getattr(item, "state", None),
                       "planned_by": getattr(item, "planned_by", None),
                       "graph_shapes": [i.get("graph", "") for i in
                                        result.get("iterations", [])]})


def run_arm(instance, base, arm, cache, *, timeout_s=1800) -> dict:
    template = instance_template(instance, cache)
    root = fresh_clone(template,
                       os.path.join(base, f"{instance['instance_id']}__{arm}"))
    store = os.path.join(base, f".store-{instance['instance_id']}-{arm}")

    if arm == ARM_AGY:
        # No SECOND workspace. Every arm already runs in its own throwaway clone
        # of the instance, and that clone is the containment `isolated_extra`
        # asks for — agy may write in it because there is nothing there to lose.
        #
        # Wrapping it in `project.workspace.isolated` as well was worse than
        # redundant: agy edited the inner worktree, the context manager removed
        # it on exit, and the tree left behind for the test scorer was the
        # untouched clone. Measured: C_agy scored 0 edits and five regressions
        # that were the BASELINE's, not agy's, and the row said nothing about
        # agy at all.
        return run_solo(instance, root, arm, isolated=True,
                        timeout_s=timeout_s)

    if arm == ARM_DOBBY:
        return run_dobby(instance, root, store, timeout_s=timeout_s)
    return run_solo(instance, root, arm, timeout_s=timeout_s)


def mark_void(rows: list) -> list:
    """An arm that made no provider call never ran; its zero is not a score."""
    for row in rows:
        if row.get("void"):
            continue
        if not row.get("calls_total"):
            row["void"] = True
            row["note"] = ("no provider call was recorded: this arm never ran. "
                           + (row.get("note") or ""))[:400]
    return rows


def run(instances, *, base: str, cache: str, seed: int = 20260824,
        timeout_s: int = 1800, arms=ARMS, on_step=None,
        journal: str | None = None) -> dict:
    """Every (instance, arm), shuffled, each row written the moment it exists.

    The journal is append-only for the reason evals/ab/runner_s1s2 records: an
    aggregation that raised once took an hour of paid provider calls with it.
    """
    pairs = [(inst, arm) for inst in instances for arm in arms]
    random.Random(seed).shuffle(pairs)

    def append(row):
        if not journal:
            return
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    rows = []
    for index, (inst, arm) in enumerate(pairs, start=1):
        if on_step:
            on_step(index, len(pairs), inst["instance_id"], arm)
        try:
            row = run_arm(inst, base, arm, cache, timeout_s=timeout_s)
        except Exception as exc:                   # noqa: BLE001
            row = {"instance_id": inst["instance_id"], "arm": arm,
                   "void": True,
                   "note": f"{type(exc).__name__}: {exc}"[:300]}
        rows.append(row)
        append(row)
        shutil.rmtree(os.path.join(base, f"{inst['instance_id']}__{arm}"),
                      ignore_errors=True)

    return {"seed": seed, "arms": list(arms), "rows": mark_void(rows),
            "scored_by": ("file-level localization against the gold patch; "
                          "resolved is NOT measured (no Docker on this machine)")}


def select(instances, *, limit: int, min_gold_files: int = 2) -> list:
    """Instances whose gold patch spans several files, hardest first.

    `min_gold_files` is the whole point of the selection. A one-file fix is a
    task with nothing to decompose, and the old corpus already measured what
    happens when a decomposing arm is given one: it pays for a plan it cannot
    use. Instances are ordered by gold-file count so a small `limit` still gets
    the cases the question is about.
    """
    eligible = [i for i in instances
                if len(gold_files(i["patch"])) >= min_gold_files]
    eligible.sort(key=lambda i: (-len(gold_files(i["patch"])),
                                 i["instance_id"]))
    return eligible[:limit]


def main(base: str, out: str, limit: int = 4, pool: int = 200) -> None:
    os.makedirs(base, exist_ok=True)
    cache = os.path.join(base, "_templates")
    os.makedirs(cache, exist_ok=True)

    chosen = select(fetch_instances(limit=pool), limit=limit)
    print(f"selected {len(chosen)} instance(s) from a pool of {pool}:",
          flush=True)
    for inst in chosen:
        print(f"  {inst['instance_id']:<34} "
              f"{len(gold_files(inst['patch']))} gold file(s)  "
              f"{inst.get('difficulty')}", flush=True)

    def step(i, n, instance_id, arm):
        print(f"[{i}/{n}] {instance_id} :: {arm}", flush=True)

    journal = out.replace(".json", ".jsonl")
    payload = run(chosen, base=base, cache=cache, on_step=step,
                  journal=journal)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
    print("WROTE", out, "and", journal, flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2],
         limit=int(sys.argv[3]) if len(sys.argv) > 3 else 4)
