"""Does the runtime actually help? A harness that can answer, and will refuse to.

The claim this exists to make possible is narrow and it is the only one worth
making: *on this corpus, under these conditions, the runtime finished more tasks
verified, or the same number for less.* Not "the harness makes models better" —
`docs/EVAL_DESIGN.md` forbids that claim in this repository and nothing here
weakens it.

Three conditions, because a difference between two of them has at least three
explanations and only a third arm separates them:

    baseline     one node, no contract, no gate. What you get by just asking.
    gated        one node WITH a contract and the verifier gate, no graph.
                 Isolates "checking the output" from "structuring the work".
    runtime      the full graph: plan -> execute -> verify -> report, with
                 retries classified and artifacts promoted.

Paired, always. Each task runs under every condition, and the statistic is the
per-task difference. Unpaired comparison across a corpus of uneven difficulty
measures which arm drew the easier tasks.

**It ships no corpus.** A benchmark whose tasks come with the tool measures the
tool's authors' imagination. You supply the corpus; `example_corpus()` shows the
shape and is explicitly labelled as a shape and not as a benchmark.

**It refuses to declare a winner it cannot support.** Below `MIN_TASKS`, or when
the bootstrap interval spans zero, `verdict` is `inconclusive` and says which of
the two it was. A harness that reports a winner from four samples is how an
unmeasured change becomes a settled fact.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field

from .contracts import ArtifactContract
from .graph import TaskGraph, TaskNode
from .runner import Runner, default_graph
from . import graph as G

BASELINE = "baseline"
GATED = "gated"
RUNTIME = "runtime"
CONDITIONS = (BASELINE, GATED, RUNTIME)

#: Fewer paired tasks than this and no comparison is reported. Not a magic
#: number: it is the point below which a single flaky task moves the headline.
MIN_TASKS = 8

#: Resamples for the bootstrap interval. Deterministic given the seed.
BOOTSTRAP_RESAMPLES = 2000


@dataclass
class Task:
    """One benchmark item. `checks` is what makes a result VERIFIED."""

    task_id: str
    task: str
    execute_command: str = ""
    checks: list = field(default_factory=list)
    provider: str | None = None

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "task": self.task,
                "execute_command": self.execute_command,
                "checks": list(self.checks), "provider": self.provider}

    @classmethod
    def from_dict(cls, raw: dict) -> "Task":
        return cls(**{k: v for k, v in raw.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class Outcome:
    """What one (task, condition) pair produced."""

    task_id: str
    condition: str
    verified: bool
    attempts: int
    wall_s: float
    state: str
    run_id: str = ""

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "condition": self.condition,
                "verified": self.verified, "attempts": self.attempts,
                "wall_s": round(self.wall_s, 3), "state": self.state,
                "run_id": self.run_id}


def example_corpus() -> list[Task]:
    """The SHAPE of a corpus. Not a benchmark, and not to be reported as one.

    Two items, both trivially satisfiable, so the harness can be exercised end
    to end on a machine with no providers installed. Reporting a result from
    these would be reporting that Python can print.
    """
    return [
        Task(task_id="echo", task="print a line and prove it printed",
             execute_command='{python} -c "print(\'ok\')"',
             checks=['{python} -c "pass"']),
        Task(task_id="arith", task="compute a sum and prove it is right",
             execute_command='{python} -c "print(2+2)"',
             checks=['{python} -c "assert 2+2==4"']),
    ]


def build_graph(task: Task, condition: str) -> TaskGraph:
    """The graph each condition runs. The DIFFERENCE between them is the point."""
    if condition == BASELINE:
        # No contract, no checks: whatever comes back is accepted. This is the
        # arm that shows what the gate is worth.
        return TaskGraph([TaskNode(
            node_id="execute", kind="execute",
            worker="command" if task.execute_command else "static",
            instruction=task.task, contract=ArtifactContract(),
            config=({"command": task.execute_command}
                    if task.execute_command else {"payload": {"done": True}}))])
    if condition == GATED:
        # Same single step, now with the contract and the acceptance checks.
        return TaskGraph([TaskNode(
            node_id="execute", kind="execute",
            worker="command" if task.execute_command else "static",
            instruction=task.task,
            contract=ArtifactContract(acceptance_checks=list(task.checks)),
            config=({"command": task.execute_command}
                    if task.execute_command else {"payload": {"done": True}}))])
    return default_graph(task.task, provider=task.provider,
                         execute_command=task.execute_command,
                         acceptance_checks=list(task.checks),
                         static=not (task.provider or task.execute_command))


def run_one(repo: str, data_dir: str, task: Task, condition: str) -> Outcome:
    runner = Runner(repo, data_dir=data_dir, sleep=lambda _s: None)
    started = time.monotonic()
    run_id = runner.start(f"[{condition}] {task.task}",
                          build_graph(task, condition))
    result = runner.run(run_id)
    return Outcome(task_id=task.task_id, condition=condition,
                   verified=result.state == G.SUCCEEDED,
                   attempts=sum(s.attempts for s in result.steps),
                   wall_s=time.monotonic() - started, state=result.state,
                   run_id=run_id)


def run_corpus(repo: str, data_dir: str, corpus: list[Task], *,
               conditions: tuple = CONDITIONS) -> list[Outcome]:
    """Every task under every condition. Order is task-major, so a run that is
    interrupted has complete pairs for the tasks it finished rather than one
    complete arm and nothing to compare it against."""
    outcomes: list[Outcome] = []
    for task in corpus:
        for condition in conditions:
            outcomes.append(run_one(repo, data_dir, task, condition))
    return outcomes


# -- statistics --------------------------------------------------------------

def _paired(outcomes: list[Outcome], a: str, b: str) -> list[tuple]:
    by_task: dict[str, dict] = {}
    for outcome in outcomes:
        by_task.setdefault(outcome.task_id, {})[outcome.condition] = outcome
    return [(v[a], v[b]) for v in by_task.values() if a in v and b in v]


def bootstrap_ci(deltas: list[float], *, seed: int = 12345,
                 resamples: int = BOOTSTRAP_RESAMPLES,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap on the mean difference. Seeded, so it is repeatable.

    A confidence interval nobody can reproduce is decoration. The seed is a
    parameter and the default is fixed, so two people reading the same numbers
    get the same interval.
    """
    if not deltas:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(resamples):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    low = means[int((alpha / 2) * resamples)]
    high = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return (round(low, 4), round(high, 4))


def compare(outcomes: list[Outcome], a: str, b: str, *,
            seed: int = 12345) -> dict:
    """Paired comparison of `b` against `a`, with an honest verdict."""
    pairs = _paired(outcomes, a, b)
    n = len(pairs)
    if n == 0:
        return {"comparison": f"{b} vs {a}", "n": 0, "verdict": "inconclusive",
                "why": "no task ran under both conditions"}

    verified = [float(y.verified) - float(x.verified) for x, y in pairs]
    attempts = [float(y.attempts) - float(x.attempts) for x, y in pairs]
    wall = [y.wall_s - x.wall_s for x, y in pairs]
    mean_verified = sum(verified) / n
    ci = bootstrap_ci(verified, seed=seed)

    if n < MIN_TASKS:
        verdict, why = "inconclusive", (
            f"{n} paired task(s); this harness reports no comparison below "
            f"{MIN_TASKS}, because one flaky task moves the headline")
    elif ci[0] <= 0.0 <= ci[1]:
        verdict, why = "inconclusive", (
            "the 95% interval on the verified-rate difference spans zero")
    elif mean_verified > 0:
        verdict, why = f"{b} verified more", "the interval excludes zero"
    else:
        verdict, why = f"{a} verified more", "the interval excludes zero"

    return {
        "comparison": f"{b} vs {a}", "n": n, "verdict": verdict, "why": why,
        "verified_rate": {a: round(sum(float(x.verified) for x, _ in pairs) / n, 4),
                          b: round(sum(float(y.verified) for _, y in pairs) / n, 4),
                          "mean_delta": round(mean_verified, 4), "ci95": ci},
        "attempts": {"mean_delta": round(sum(attempts) / n, 3)},
        "wall_s": {"mean_delta": round(sum(wall) / n, 3)},
        "not_established": (
            "this measures THIS corpus under THIS fleet. It says nothing about "
            "end-task capability, and nothing here licenses the claim that the "
            "harness improves a model"),
    }


def report(outcomes: list[Outcome], *, seed: int = 12345) -> dict:
    by_condition: dict[str, dict] = {}
    for condition in CONDITIONS:
        rows = [o for o in outcomes if o.condition == condition]
        if not rows:
            continue
        by_condition[condition] = {
            "n": len(rows),
            "verified_rate": round(sum(o.verified for o in rows) / len(rows), 4),
            "mean_attempts": round(sum(o.attempts for o in rows) / len(rows), 3),
            "mean_wall_s": round(sum(o.wall_s for o in rows) / len(rows), 3),
        }
    return {
        "conditions": by_condition,
        "comparisons": [compare(outcomes, BASELINE, GATED, seed=seed),
                        compare(outcomes, GATED, RUNTIME, seed=seed),
                        compare(outcomes, BASELINE, RUNTIME, seed=seed)],
        "outcomes": [o.to_dict() for o in outcomes],
    }


def load_corpus(path: str) -> list[Task]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    items = raw.get("tasks", raw) if isinstance(raw, dict) else raw
    return [Task.from_dict(item) for item in items]


def save_report(data_dir: str, payload: dict) -> str:
    path = os.path.join(data_dir, "state", "runtime",
                        f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, default=str)
    return path
