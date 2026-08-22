"""The A/B/C runner: same model, same task, same budget, three amounts of harness.

Three arms and not two, because a difference between two of them has at least
three explanations:

    A  direct      one provider call with the task. No contract, no gate, no
                   state. The acceptance check runs ONCE, afterwards, on
                   whatever the tree looks like. This is the operator's previous
                   habit: ask, take the answer, check it.
    B  gated       the same single call, now with the side-effect contract and
                   the acceptance gate. Isolates CHECKING from STRUCTURING.
    C  dobby       the project loop: structured item, acceptance criteria,
                   PROPOSED -> VERIFIED -> PROMOTED, classified failures, and a
                   retry that carries the failure into the next attempt.

Without B, a C-beats-A result cannot distinguish "the gate caught things" from
"the structure helped", and those are different claims with different fixes.

EVERY NUMBER IS TAKEN WHERE IT HAPPENS

Provider calls are counted by `providers.run.recording()`, at the adapter. An
orchestrator counting its own intentions misses a retry inside a worker, and the
retry is precisely what this exists to see. Token and cost come from the
provider's own envelope via `providers/usage.py`; a call that reported nothing
stays None and never becomes a zero.

FAIRNESS IS ENFORCED, NOT PROMISED

Same task string to all three arms. Same acceptance command. Same call budget,
declared per run and asserted. Each arm gets a FRESH COPY of the fixture tree, so
arm B never inherits what arm A wrote. Order is randomised over (task, arm) with
a recorded seed, so a provider warming up or degrading cannot land systematically
on one arm.

WHAT IT REFUSES TO DO

Declare a winner. It writes rows; `runtime/bench.py` holds the statistics and its
`MIN_TASKS` refusal, and a pilot of two or three tasks is below it by design.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import time
from dataclasses import asdict, dataclass, field

#: What the C arm takes as its baseline: the tree imports and compiles. NOT the
#: task's acceptance check — see `run_arm_dobby`.
SOUNDNESS_CHECK = (
    '{python} -c "import compileall, sys; '
    'sys.exit(0 if compileall.compile_dir(chr(46), quiet=2) else 1)"')

ARM_DIRECT = "A_direct"
ARM_GATED = "B_gated"
ARM_DOBBY = "C_dobby"
#: The proposed default: the profiler picks the shape, so a scoped gradeable item
#: gets ONE gated call instead of three. Named as its own arm rather than
#: replacing C, because a claim that it is cheaper has to be measured against the
#: thing it replaces, in the same run, on the same corpus.
ARM_ADAPTIVE = "D_adaptive"
ARMS = (ARM_DIRECT, ARM_GATED, ARM_DOBBY, ARM_ADAPTIVE)


@dataclass
class PilotTask:
    """One benchmark item, and the honest record of why it was chosen."""

    task_id: str
    prompt: str
    #: Command that exits 0 iff the task is done. Run in the arm's own tree.
    check: str
    #: Files copied into a fresh tree before each arm runs.
    fixture: dict = field(default_factory=dict)
    #: Paths the task is expected to touch, for the effect contract.
    expected_paths: list = field(default_factory=list)
    #: Fixture files the arm may NOT modify — the test that decides the task.
    #: An agent with write rights to the whole tree can make any check pass by
    #: editing the check, and `docs/FAILURE_CATALOG.md` calls that Evaluation
    #: Gaming and task failure. Hashed before and after; a modified asset voids
    #: the pass regardless of the exit code.
    immutable: list = field(default_factory=list)
    #: Recorded BEFORE running, so the result can be split by it afterwards. If
    #: every task is False the corpus is rigged toward the structured arms and
    #: the report has to say so.
    one_shot_plausible: bool = True
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArmRun:
    """What one (task, arm) pair produced. Raw, per run, never averaged here."""

    task_id: str
    arm: str
    run_id: str = ""
    verified: bool = False
    first_pass: bool = False
    provider_calls: int = 0
    retries: int = 0
    wall_s: float = 0.0
    agent_s: float = 0.0
    acceptance_failures: int = 0
    contract_violations: int = 0
    human_interventions: int = 0
    false_successes: int = 0
    usage: dict = field(default_factory=dict)
    stopped: str = ""
    note: str = ""
    void: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def fresh_tree(base: str, task: PilotTask, arm: str) -> str:
    """A clean copy of the fixture per arm. Contamination would be invisible.

    Arm A editing a file and arm B then finding it already correct is a corpus
    that reports the second arm as better at a task it never did.
    """
    root = os.path.join(base, f"{task.task_id}__{arm}")
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    for name, text in task.fixture.items():
        target = os.path.join(root, name)
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    return root


def fingerprint(root: str, names) -> dict:
    """Content hashes of the files an arm is forbidden to touch."""
    import hashlib

    out = {}
    for name in names:
        try:
            with open(os.path.join(root, name), "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            out[name] = None
    return out


def tampered(before: dict, after: dict) -> list:
    """Immutable assets that changed. Named, not counted."""
    return sorted(k for k in before if before[k] != after.get(k))


def run_check(root: str, command: str, timeout_s: int = 300) -> bool:
    """The acceptance command, run identically for every arm."""
    import subprocess
    import sys

    from dobby.core.platform import child_env

    rendered = command.replace("{python}", f'"{sys.executable}"')
    try:
        proc = subprocess.run(rendered, shell=True, cwd=root,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=child_env(), timeout=timeout_s)
    except Exception:
        return False
    return proc.returncode == 0


def _usage_of(calls) -> dict:
    from dobby.providers.usage import Usage, total

    rows = []
    for call in calls:
        if call.usage:
            rows.append(Usage(**{k: v for k, v in call.usage.items()
                                 if k in Usage.__dataclass_fields__}))
        else:
            rows.append(None)
    return total(rows)


def run_arm_direct(task: PilotTask, root: str, *, provider: str,
                   max_calls: int, timeout_s: int) -> ArmRun:
    """One call, edit rights, then the check — once, afterwards.

    This is deliberately the operator's previous habit and not a strawman: the
    same task text, the same permission the structured arms get, the same check.
    What it lacks is everything under test.
    """
    from dobby.providers.catalog import registry
    from dobby.providers.run import recording, run_provider

    spec = registry().get(provider)
    guard = fingerprint(root, task.immutable)
    started = time.monotonic()
    with recording() as calls:
        result = run_provider(spec, task.prompt, cwd=root,
                              extra=spec.write_extra, timeout_s=timeout_s)
    wall = time.monotonic() - started
    verified = run_check(root, task.check)
    gamed = tampered(guard, fingerprint(root, task.immutable))
    if gamed:
        verified = False

    return ArmRun(
        task_id=task.task_id, arm=ARM_DIRECT, run_id=f"direct:{task.task_id}",
        verified=verified,
        # One call by construction, so passing IS passing first time.
        first_pass=verified,
        provider_calls=len(calls),
        retries=0,
        wall_s=round(wall, 2),
        agent_s=round(sum(c.duration_s for c in calls), 2),
        acceptance_failures=0 if verified else 1,
        contract_violations=0,
        # Nobody is asked anything: the arm has no stop that requires a person.
        human_interventions=0,
        # The defect this benchmark was delayed by: a call that reports success
        # and changed nothing. Arm A has no effect contract, so it can only be
        # detected here, after the fact, by the check disagreeing with the call.
        false_successes=int(bool(result.ok) and not verified),
        usage=_usage_of(calls),
        stopped="single_call",
        note=(f"EVALUATION GAMING: modified {gamed}" if gamed else
              ("" if result.ok else f"provider failed: {result.error}")),
        void=not result.ok and not calls)


def run_arm_gated(task: PilotTask, root: str, data_dir: str, *, provider: str,
                  max_calls: int, timeout_s: int) -> ArmRun:
    """The same one call, with the side-effect contract and the acceptance gate."""
    from dobby.providers.run import recording
    from dobby.runtime import graph as G
    from dobby.runtime.contracts import LOCAL_WRITE, ArtifactContract
    from dobby.runtime.runner import Runner
    from dobby.runtime.scheduler import RunBudget

    node = G.TaskNode(
        node_id="execute", kind="execute", worker="provider",
        instruction=task.prompt,
        contract=ArtifactContract(side_effect_class=LOCAL_WRITE,
                                  expected_paths=list(task.expected_paths),
                                  acceptance_checks=[task.check]),
        config={"provider": provider, "timeout_s": timeout_s})

    guard = fingerprint(root, task.immutable)
    started = time.monotonic()
    with recording() as calls:
        runner = Runner(root, data_dir=data_dir, sleep=lambda _s: None)
        run_id = runner.start(f"[gated] {task.prompt}", G.TaskGraph([node]))
        result = runner.run(run_id, budget=RunBudget(max_attempts=max_calls))
    wall = time.monotonic() - started

    return _from_run(task, ARM_GATED, run_id, result, calls, wall, root,
                     gamed=tampered(guard, fingerprint(root, task.immutable)))


def run_arm_adaptive(task: PilotTask, root: str, data_dir: str, *,
                     provider: str, max_calls: int, timeout_s: int) -> ArmRun:
    """The same loop with `policy="adaptive"`. Everything else identical to C."""
    return _run_loop(task, root, data_dir, ARM_ADAPTIVE, provider=provider,
                     max_calls=max_calls, policy="adaptive")


def run_arm_dobby(task: PilotTask, root: str, data_dir: str, *, provider: str,
                  max_calls: int, timeout_s: int) -> ArmRun:
    """The project loop with its gates, state and repair-carrying retry."""
    return _run_loop(task, root, data_dir, ARM_DOBBY, provider=provider,
                     max_calls=max_calls, policy="")


def _run_loop(task: PilotTask, root: str, data_dir: str, arm: str, *,
              provider: str, max_calls: int, policy: str) -> ArmRun:
    """C and D differ by ONE argument, and this is where that is made obvious."""
    from dobby.project import initialise
    from dobby.project.reattempt import persevere
    from dobby.providers.run import recording

    guard = fingerprint(root, task.immutable)
    started = time.monotonic()
    with recording() as calls:
        # SOUNDNESS, NOT ACCEPTANCE. The first pilot passed `task.check` here and
        # every C arm stopped at `baseline_failed` without making a single
        # provider call — correctly, because PK-1 refuses to start work on a tree
        # that fails its own checks, and the task's own failing test WAS the
        # tree's check. That is the invariant working and the harness misusing
        # it. A baseline says "this tree is sound enough to work in"; the
        # acceptance check says "the work is done", and conflating them makes
        # every unfinished task look like a broken repository.
        initialise(data_dir, root, smoke=(SOUNDNESS_CHECK,),
                   item_specs=[{"outcome": task.prompt,
                                "acceptance_checks": [task.check]}],
                   run_baseline=True)
        outcome = persevere(data_dir, max_attempts=2, provider=provider,
                            max_steps=max_calls, policy=policy)
    wall = time.monotonic() - started

    from dobby.project import ProjectStore
    item = ProjectStore(data_dir).load_project(None)["portfolio"].get("W001")
    iterations = [i for a in outcome["attempts"] for i in a["iterations"]]
    gamed = tampered(guard, fingerprint(root, task.immutable))

    return ArmRun(
        task_id=task.task_id, arm=arm,
        run_id=";".join(i["run_id"] for i in iterations) or "none",
        verified=(item.state == "DONE") and not gamed,
        first_pass=(item.state == "DONE" and outcome["attempts_used"] == 1
                    and outcome["repairs_applied"] == 0),
        provider_calls=len(calls),
        retries=max(0, outcome["attempts_used"] - 1),
        wall_s=round(wall, 2),
        agent_s=round(sum(c.duration_s for c in calls), 2),
        acceptance_failures=sum(1 for i in iterations
                                if i["item_state"] != "DONE"),
        contract_violations=0,
        # A stop only a person can cross. The loop names them; this counts them
        # rather than inferring from a failure.
        human_interventions=int(outcome["stopped"] in (
            "needs_architect", "needs_discovery", "needs_human_approval",
            "plan_rejected", "needs_reconciliation", "no_repair_derived",
            "replan_not_applied", "isolation_unavailable")),
        false_successes=0,
        usage=_usage_of(calls),
        stopped=outcome["stopped"],
        note=(f"EVALUATION GAMING: modified {gamed}. "
              if gamed else "") + outcome["detail"][:200])


def _from_run(task, arm, run_id, result, calls, wall, root, *,
              gamed=()) -> ArmRun:
    from dobby.runtime import graph as G
    from dobby.runtime.failures import CONTRACT_VIOLATION, EFFECT_NOT_OBSERVED

    steps = list(result.steps)
    attempts = sum(s.attempts for s in steps)
    violations = sum(1 for s in steps
                     if (s.failure or {}).get("failure_class")
                     == CONTRACT_VIOLATION)
    effect_misses = sum(1 for s in steps
                        if (s.failure or {}).get("failure_class")
                        == EFFECT_NOT_OBSERVED)
    verified = (result.state == G.SUCCEEDED) and not gamed
    return ArmRun(
        task_id=task.task_id, arm=arm, run_id=run_id,
        verified=verified,
        first_pass=verified and attempts <= len(steps),
        provider_calls=len(calls),
        retries=max(0, attempts - len(steps)),
        wall_s=round(wall, 2),
        agent_s=round(sum(c.duration_s for c in calls), 2),
        acceptance_failures=0 if verified else 1,
        contract_violations=violations,
        human_interventions=0,
        # Caught rather than counted after the fact: the arm HAS an effect
        # contract, so a call that reported success and changed nothing is a
        # classified failure instead of a silent pass.
        false_successes=effect_misses,
        usage=_usage_of(calls),
        stopped=result.state,
        note=(f"EVALUATION GAMING: modified {list(gamed)}" if gamed else ""))


RUNNERS = {ARM_DIRECT: run_arm_direct, ARM_GATED: run_arm_gated,
           ARM_DOBBY: run_arm_dobby, ARM_ADAPTIVE: run_arm_adaptive}


def run_pilot(corpus, *, base: str, provider: str = "claude", seed: int = 20260822,
              max_calls: int = 4, timeout_s: int = 600, arms=ARMS,
              on_step=None) -> dict:
    """Every task under every arm, in a randomised order, with the seed recorded.

    Returns raw rows and nothing derived. Averaging happens in the report, where
    the denominator can be shown beside it.
    """
    pairs = [(task, arm) for task in corpus for arm in arms]
    rng = random.Random(seed)
    rng.shuffle(pairs)

    rows: list[ArmRun] = []
    for index, (task, arm) in enumerate(pairs, start=1):
        root = fresh_tree(base, task, arm)
        data_dir = os.path.join(root, ".dobby")
        if on_step:
            on_step(index, len(pairs), task.task_id, arm)
        try:
            if arm == ARM_DIRECT:
                row = RUNNERS[arm](task, root, provider=provider,
                                   max_calls=max_calls, timeout_s=timeout_s)
            else:
                row = RUNNERS[arm](task, root, data_dir, provider=provider,
                                   max_calls=max_calls, timeout_s=timeout_s)
        except Exception as exc:                  # noqa: BLE001
            # Recorded as void for THIS arm and reported; the report drops the
            # whole task from the paired statistic, because dropping one arm
            # only is how a corpus tilts.
            row = ArmRun(task_id=task.task_id, arm=arm, void=True,
                         note=f"{type(exc).__name__}: {exc}"[:300])
        rows.append(row)

    raw = mark_void([r.to_dict() for r in rows])
    complete, dropped = paired_tasks(raw, arms=tuple(arms))
    return {"seed": seed, "provider": provider, "max_calls": max_calls,
            "order": [(t.task_id, a) for t, a in pairs],
            "corpus": [t.to_dict() for t in corpus],
            "rows": raw,
            "paired_tasks": complete,
            "dropped_tasks": [{"task_id": t, "void_arms": a}
                              for t, a in dropped],
            "note": ("only `paired_tasks` may be compared across arms; a task "
                     "any arm failed to run is void for all of them")}


def mark_void(rows: list) -> list:
    """A row with no provider call never ran, whatever else it recorded.

    DESIGN.md's stopping rule says an environmentally failed task is void for
    EVERY arm, because dropping it from one arm only is how a corpus tilts. This
    applies it mechanically: zero calls on a provider-driven arm is not a loss,
    it is an absence, and scoring it as a loss would credit whichever arm the
    provider happened not to fail on.
    """
    for row in rows:
        if row.get("provider_calls", 0) == 0 and not row.get("void"):
            row["void"] = True
            row["note"] = (("no provider call was recorded: this arm never ran. "
                            + (row.get("note") or ""))[:400])
    return rows


def paired_tasks(rows: list, arms=ARMS) -> tuple:
    """`(complete, dropped)` — task ids where EVERY arm produced a real run.

    An unpaired comparison across arms of uneven luck measures which arm drew
    the working provider.
    """
    by_task: dict = {}
    for row in rows:
        by_task.setdefault(row["task_id"], {})[row["arm"]] = row
    complete, dropped = [], []
    for task_id, got in sorted(by_task.items()):
        if all(arm in got and not got[arm].get("void") for arm in arms):
            complete.append(task_id)
        else:
            dropped.append((task_id, sorted(a for a in arms
                                            if a not in got
                                            or got[a].get("void"))))
    return complete, dropped


def save(payload: dict, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    return path
