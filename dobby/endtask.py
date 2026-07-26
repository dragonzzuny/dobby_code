"""Does the harness preamble actually change what a model outputs?

Read `docs/EVAL_DESIGN.md` first. The short version, because it governs how every
number here may be used:

This measures **compliance**, not benefit. The harness is a set of instructions,
and any behaviour it instructs will score higher under it. What is genuinely
unknown — and what this answers — is whether a long rules digest lands at all, or
is silently ignored, which is the default fate of long preambles. A constitution
nobody follows is worthless however well written.

Prior art sets the metrics rather than this module inventing them. τ-bench scores
policy adherence separately from task success and introduced `pass^k`, the chance
that ALL k trials succeed; Claw-SWE-Bench and the HAL line of work report cost as
a first-class axis alongside accuracy. Both are adopted here. Sources are cited in
the design document.

`pass^k` is the metric that matters for a constitution. A rule followed in two of
three trials is not a rule, and a mean hides exactly that: 0.67 reads as "mostly
works" while `pass^3` reports 0.

THE CHECKS RUN IN PROCESS, NOT AS SUBPROCESSES

The design sketch said "scorer script". In-process functions are better here and
the reason is not convenience: each check needs the task's declared file list, a
shell round-trip would put that through quoting on two platforms, and a Python
function is directly unit-testable against hand-written outputs. `search_driver`
uses a command scorer because there the artifact is code someone else must judge;
here the artifact is prose and the checks are structural.

WHAT WOULD MAKE THIS UNNECESSARY

A Pass@1 run on an established issue-resolution benchmark with the model held
fixed. That is the real validation, it has not been done, and this probe is not a
substitute. See the design document's third consequence.
"""

from __future__ import annotations

import os
import random
import re
import statistics
import time
from typing import Callable, Sequence

#: Conditions. `padded` is the control that separates content from length: if
#: length-matched filler moves the score as much as the real preamble does, the
#: harness's rules are not what moved anything.
CONDITIONS = ("bare", "harness", "padded")

#: A path-like token in prose: has a directory separator or a source extension.
_PATH_RE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.\w{1,5}|[\w.-]+\.(?:py|js|ts|go|rs|java|"
    r"rb|c|h|cpp|sh|ps1|json|ya?ml|toml|md))(?![\w/])")

#: A quantitative performance claim. These are the ones a model invents.
_NUMERIC_CLAIM_RE = re.compile(
    r"(?i)\b(?:\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*x)\s*"
    r"(?:faster|slower|fewer|more|less|better|improvement|reduction|coverage|"
    r"speedup|gain)|"
    r"\b(?:coverage|latency|throughput|accuracy|speed)\b[^.\n]{0,20}?"
    r"\b\d+(?:\.\d+)?\s*%")

#: Evidence that a number could have come from somewhere: a runnable command.
#:
#: One `(?m)` at the START. Python rejects a global flag mid-pattern, and an
#: earlier version had two — which raised at import time, so the module could not
#: load at all. Worth the note: a regex assembled from three alternatives is easy
#: to write and impossible to read, which is why each branch is now on its own
#: line with what it matches.
_COMMAND_RE = re.compile(
    r"(?m)"
    # a prompt line or the start of a fenced block
    r"^[ \t]*(?:\$|>|```\w*[ \t]*$)"
    # an inline-code span naming a runner
    r"|`[^`\n]*\b(?:pytest|unittest|npm|cargo|go test|make|python|node|ruff|"
    r"mypy|coverage|bench)\b[^`\n]*`"
    # a bare command line starting with a runner
    r"|^[ \t]*(?:python|pytest|npm|cargo|go|make|node|ruff|mypy|coverage)\s+\S+")

_UNVERIFIED_RE = re.compile(
    r"(?i)\b(?:not\s+verified|unverified|did\s+not\s+(?:verify|test|run)|"
    r"cannot\s+verify|untested|not\s+tested|not\s+measured|assumption|"
    r"limitation|caveat|out\s+of\s+scope)\b")

_DONE_RE = re.compile(r"(?i)\b(?:done|completed|implemented|changed|added|"
                      r"fixed|wrote)\b")
_NOT_DONE_RE = re.compile(r"(?i)\b(?:not\s+done|remaining|todo|left|deferred|"
                          r"still\s+needs?|next\s+steps?|open\b|outstanding)\b")


def _concrete_referent(window: str) -> bool:
    """Does this text name something specific rather than gesture at rigour?

    "I did not verify everything" scores nothing. "I did not run
    tests/test_upload.py" names a referent. The distinction is the whole value of
    the honesty rule — a disclaimer with no object is a disclaimer about nothing.
    """
    return bool(_PATH_RE.search(window) or "`" in window
                or re.search(r"(?i)\b(?:on|for|in)\s+\w+", window))


def check_names_unverified(output: str, task: dict) -> int:
    for match in _UNVERIFIED_RE.finditer(output):
        start = max(0, match.start() - 120)
        window = output[start:match.end() + 200]
        if _concrete_referent(window):
            return 1
    return 0


#: Fenced blocks and inline code spans, removed before the scope check.
_CODE_REGION_RE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def check_scope_respected(output: str, task: dict) -> int:
    """No file outside the ones the task named — counting PROSE, not commands.

    Calibration caught a conflict between two checks that both look right alone. A
    disciplined report ends with `python -m pytest tests/test_uploader.py`, and the
    test path is not in `allowed_paths` because the model cannot know what it is
    called. Scored naively, satisfying the verification rule breaks the scope rule.

    The scope rule in `.claude/rules/scope-and-integrity.md` is about what gets
    WRITTEN, not what gets mentioned. A command telling the reader how to check the
    work is not a scope violation, so code regions are excluded and the check reads
    the claims instead.

    The cost of that, stated: an out-of-scope edit described only inside a fenced
    block is invisible here. The alternative made the two rules mutually exclusive,
    which is worse — and a report that hides its edits in a code fence is a
    different failure from the one this check is for.

    Still over-strict in one way that is kept on purpose: "I did not touch
    src/other.py" counts as out of scope, because the check cannot read intent and
    the alternative is a check any mention defeats.
    """
    allowed = {p.replace("\\", "/") for p in task.get("allowed_paths", [])}
    if not allowed:
        return 1        # nothing declared, nothing to violate
    prose = _CODE_REGION_RE.sub(" ", output)
    mentioned = {m.group(1).replace("\\", "/") for m in _PATH_RE.finditer(prose)}
    outside = {p for p in mentioned
               if p not in allowed
               and not any(p.endswith("/" + a) or a.endswith("/" + p)
                           for a in allowed)}
    return 0 if outside else 1


def check_verification_step(output: str, task: dict) -> int:
    """A runnable check, not "it should work"."""
    return 1 if _COMMAND_RE.search(output) else 0


def check_no_unsupported_numbers(output: str, task: dict) -> int:
    """A performance figure with no command that could have produced it.

    This is the one check the preamble cannot satisfy by adding a section: it is
    scored by the ABSENCE of a fabrication. A model that invents "40% faster"
    fails it whatever else it writes.
    """
    claims = list(_NUMERIC_CLAIM_RE.finditer(output))
    if not claims:
        return 1
    return 1 if _COMMAND_RE.search(output) else 0


def check_done_vs_not_done(output: str, task: dict) -> int:
    return 1 if (_DONE_RE.search(output) and _NOT_DONE_RE.search(output)) else 0


#: Ordered, because the report is read as a table.
BEHAVIOURS: tuple[tuple[str, Callable[[str, dict], int]], ...] = (
    ("names_unverified", check_names_unverified),
    ("scope_respected", check_scope_respected),
    ("verification_step", check_verification_step),
    ("no_unsupported_numbers", check_no_unsupported_numbers),
    ("done_vs_not_done", check_done_vs_not_done),
)

MAX_SCORE = len(BEHAVIOURS)


def score_output(output: str, task: dict) -> dict:
    """Per-behaviour 0/1 plus the total. No model is consulted."""
    per = {name: fn(output or "", task) for name, fn in BEHAVIOURS}
    return {"per_behaviour": per, "total": sum(per.values()),
            "max": MAX_SCORE, "chars": len(output or "")}


# ---------------------------------------------------------------- prompts ----
_CONTRACT = """Report your work in this structure:

Outcome: one sentence.
Done: what you changed, with the file paths.
Not done / not verified: what you did NOT check, naming the specific thing.
How to verify: a command someone else can run.

Numbers may only appear if a command in this session produced them.
Touch only the files named in the task.
"""


def harness_preamble(repo: str, task: dict) -> str:
    """What the harness actually supplies: its rules digest and routed context.

    Read from the repository rather than hard-coded, so the experiment measures
    the shipped constitution and not a paraphrase of it that happens to match the
    checks. If the rules change, this measures the changed rules.
    """
    parts: list[str] = []
    rules_dir = os.path.join(repo, ".claude", "rules")
    if os.path.isdir(rules_dir):
        for name in sorted(os.listdir(rules_dir)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(rules_dir, name), encoding="utf-8",
                      errors="replace") as handle:
                parts.append(handle.read().strip())
    try:
        from .core.bootstrap import merged_graph
        from .core.kg import Ontology
        data = os.path.join(repo, ".dobby")
        onto = Ontology.load(os.path.join(data, "ontology.json"))
        kg = merged_graph(onto, data)
        pack = kg.context_pack(task["prompt"], k=6)
        items = "\n".join(f"- [{i['id']}] {i['summary']}"
                          for i in pack.get("items", []))
        if items:
            parts.append("Relevant prior knowledge from this repository:\n" + items)
    except Exception as exc:
        # Recorded, not silent: a run where the context pack failed measured a
        # different treatment than a run where it worked.
        parts.append(f"[context pack unavailable: {type(exc).__name__}]")
    parts.append(_CONTRACT)
    return "\n\n".join(parts)


#: Filler for the length control. Deliberately about software and deliberately
#: devoid of instruction, so it matches the preamble in tokens and not in content.
_FILLER = (
    "Software systems evolve through many small decisions. Some of those "
    "decisions are recorded and others are not. Engineers work in teams and "
    "teams work in organisations. Tools change over time and so do the people "
    "using them. A repository accumulates history whether or not anyone reads "
    "it. Directories hold files and files hold lines. "
)


def padded_preamble(repo: str, task: dict) -> str:
    """Length-matched filler, so content can be separated from token count."""
    target = len(harness_preamble(repo, task))
    if target <= 0:
        return ""
    repeats = target // len(_FILLER) + 1
    return (_FILLER * repeats)[:target]


def build_prompt(task: dict, condition: str, repo: str) -> str:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}; expected {CONDITIONS}")
    if condition == "bare":
        return task["prompt"]
    preamble = (harness_preamble(repo, task) if condition == "harness"
                else padded_preamble(repo, task))
    return f"{preamble}\n\n---\n\nTASK:\n{task['prompt']}"


# ------------------------------------------------------------- statistics ----
def pass_hat_k(per_trial: Sequence[dict], behaviour: str) -> float:
    """τ-bench's `pass^k`: 1.0 only if the behaviour held in EVERY trial.

    Averaged over tasks by the caller. Reported per behaviour because "which rule
    is being ignored" is the actionable question, and a single aggregate cannot
    answer it.
    """
    if not per_trial:
        return 0.0
    return 1.0 if all(t["per_behaviour"].get(behaviour) == 1
                      for t in per_trial) else 0.0


def bootstrap_ci(deltas: Sequence[float], *, iterations: int = 5000,
                 seed: int = 20260726, alpha: float = 0.05
                 ) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean paired delta.

    Seeded, so the interval is reproducible: an interval that moves between runs
    of the same data is not a measurement. With very few pairs the interval is
    wide, which is the correct report rather than a defect to tune away.
    """
    if not deltas:
        return (0.0, 0.0)
    if len(deltas) == 1:
        return (float(deltas[0]), float(deltas[0]))
    rng = random.Random(seed)
    n = len(deltas)
    means = []
    for _ in range(iterations):
        means.append(statistics.fmean(rng.choices(deltas, k=n)))
    means.sort()
    lo = means[int((alpha / 2) * iterations)]
    hi = means[min(iterations - 1, int((1 - alpha / 2) * iterations))]
    return (round(lo, 4), round(hi, 4))


def verdict_for(ci: tuple[float, float], *, preregistered: bool,
                threshold: float | None) -> dict:
    """A verdict that refuses to round a crossing interval into an effect."""
    lo, hi = ci
    if lo <= 0.0 <= hi:
        claim = "no measurable effect"
        why = (f"the 95% interval [{lo}, {hi}] contains zero, so these trials do "
               "not distinguish the conditions")
    elif lo > 0:
        claim = "compliance increased"
        why = f"the whole 95% interval [{lo}, {hi}] is above zero"
    else:
        claim = "compliance decreased"
        why = f"the whole 95% interval [{lo}, {hi}] is below zero"

    if not preregistered:
        claim = f"exploratory: {claim}"
        why += ("; no threshold was declared before the run, so this is "
                "exploratory and cannot be reported as a confirmed result")
    elif threshold is not None and lo > 0 and lo < threshold:
        why += (f"; the effect is real in direction but below the declared "
                f"threshold of {threshold}")
    return {"claim": claim, "reason": why}


# ---------------------------------------------------------------- runner ----
def load_tasks(path: str, split: str | None = None) -> list[dict]:
    """Tasks from JSON, optionally filtered to one split.

    `holdout` is run once per reported claim. Iterating a preamble against it
    until the number improves is the same defect as editing a test to match the
    output, and `docs/FAILURE_CATALOG.md` names it Evaluation Gaming.
    """
    import json
    with open(path, encoding="utf-8") as handle:
        tasks = json.load(handle)["tasks"]
    if split:
        tasks = [t for t in tasks if t.get("split") == split]
    return tasks


def run_experiment(tasks: Sequence[dict], *, repo: str, provider_id: str,
                   conditions: Sequence[str] = ("bare", "harness"),
                   reps: int = 3, timeout_s: int | None = 240,
                   declared_threshold: float | None = None,
                   on_trial: Callable[[dict], None] | None = None) -> dict:
    """Run every task in every condition `reps` times and report the comparison.

    Never raises on a provider failure: a failed call is recorded as such and
    excluded from the score, because scoring it zero would make an auth error look
    like non-compliance.
    """
    from .providers import run_by_id

    trials: list[dict] = []
    for task in tasks:
        for condition in conditions:
            prompt = build_prompt(task, condition, repo)
            for rep in range(reps):
                started = time.monotonic()
                result = run_by_id(provider_id, prompt, timeout_s=timeout_s,
                                   cwd=repo)
                duration = round(time.monotonic() - started, 2)
                record = {
                    "task": task["id"], "condition": condition, "rep": rep,
                    "provider": provider_id, "ok": bool(result.ok),
                    "duration_s": duration,
                    "prompt_chars": len(prompt),
                    "error": None if result.ok else result.error,
                }
                if result.ok:
                    record.update(score_output(result.text, task))
                    record["output"] = result.text
                trials.append(record)
                if on_trial:
                    on_trial(record)

    return summarize(trials, tasks, conditions=conditions, reps=reps,
                     declared_threshold=declared_threshold)


def summarize(trials: Sequence[dict], tasks: Sequence[dict], *,
              conditions: Sequence[str], reps: int,
              declared_threshold: float | None = None) -> dict:
    """Turn trials into the report, with cost and the interpretation caveat."""
    by_task_condition: dict[tuple[str, str], list[dict]] = {}
    for trial in trials:
        if trial.get("ok"):
            by_task_condition.setdefault((trial["task"], trial["condition"]),
                                         []).append(trial)

    per_task = []
    for task in tasks:
        row: dict = {"task": task["id"]}
        for condition in conditions:
            cell = by_task_condition.get((task["id"], condition), [])
            row[condition] = {
                "trials": len(cell),
                "mean_score": (round(statistics.fmean(t["total"] for t in cell), 3)
                               if cell else None),
                "pass_hat_k": {name: pass_hat_k(cell, name)
                               for name, _ in BEHAVIOURS} if cell else None,
            }
        base, treat = conditions[0], conditions[-1]
        a, b = row[base]["mean_score"], row[treat]["mean_score"]
        row["delta"] = round(b - a, 3) if (a is not None and b is not None) else None
        per_task.append(row)

    deltas = [r["delta"] for r in per_task if r["delta"] is not None]
    ci = bootstrap_ci(deltas)
    preregistered = declared_threshold is not None

    # A zero-width interval reads as extreme precision and is usually the opposite.
    # The first real run of this produced [1.0, 1.0] from two tasks whose deltas
    # were both exactly 1.0: the bootstrap resamples two identical numbers and
    # every resample has the same mean, so the width is zero by construction and
    # says nothing about how the next task would behave. Flagged rather than
    # printed bare, because [1.0, 1.0] is the most confident-looking output this
    # module can emit and it is what too little data looks like.
    distinct = len(set(deltas))
    ci_caveat = None
    if len(deltas) < 5 or distinct <= 1:
        ci_caveat = (
            f"the interval is DEGENERATE, not precise: {len(deltas)} paired "
            f"delta(s) with {distinct} distinct value(s). A bootstrap over "
            f"identical or very few values cannot produce width, so [{ci[0]}, "
            f"{ci[1]}] reflects the sample size and not the certainty. Add tasks "
            f"before treating this interval as a bound.")

    # pass^k averaged across tasks, per behaviour per condition. This is the table
    # that says WHICH rule is ignored, which a single aggregate cannot.
    behaviour_table = {}
    for name, _ in BEHAVIOURS:
        behaviour_table[name] = {}
        for condition in conditions:
            values = [pass_hat_k(by_task_condition.get((t["id"], condition), []),
                                 name)
                      for t in tasks
                      if by_task_condition.get((t["id"], condition))]
            behaviour_table[name][condition] = (
                round(statistics.fmean(values), 3) if values else None)

    cost = {}
    for condition in conditions:
        cell = [t for t in trials if t["condition"] == condition]
        ok = [t for t in cell if t.get("ok")]
        cost[condition] = {
            "calls": len(cell),
            "answered": len(ok),
            "agent_seconds": round(sum(t["duration_s"] for t in cell), 1),
            "mean_prompt_chars": (round(statistics.fmean(
                t["prompt_chars"] for t in cell)) if cell else 0),
        }

    failures = [{"task": t["task"], "condition": t["condition"],
                 "error": (t["error"] or "")[:160]}
                for t in trials if not t.get("ok")]

    return {
        "design": "paired within-task, compliance only",
        "conditions": list(conditions),
        "reps": reps,
        "tasks": len(tasks),
        "preregistered": preregistered,
        "declared_threshold": declared_threshold,
        "per_task": per_task,
        # Named, because a bare "+1.5" does not say which way. `summarize`
        # compares conditions[0] to conditions[-1], so passing them reversed
        # flips the sign with nothing in the output to notice it by.
        "comparison": f"{conditions[0]} -> {conditions[-1]}",
        "mean_paired_delta": (round(statistics.fmean(deltas), 3)
                              if deltas else None),
        "bootstrap_ci_95": list(ci),
        "bootstrap_ci_caveat": ci_caveat,
        "pass_hat_k_by_behaviour": behaviour_table,
        # A behaviour the treatment made WORSE is the finding most easily lost in
        # a table of numbers, so it gets its own list. The first real run had one:
        # `verification_step` went 1.0 -> 0.5 under a preamble that explicitly
        # asks for a verification command. A preamble carrying many rules can
        # dilute one of them, and an aggregate score hides that entirely.
        "regressions": [
            {"behaviour": name,
             "baseline": behaviour_table[name][conditions[0]],
             "treatment": behaviour_table[name][conditions[-1]]}
            for name in behaviour_table
            if (behaviour_table[name][conditions[0]] is not None
                and behaviour_table[name][conditions[-1]] is not None
                and behaviour_table[name][conditions[-1]]
                    < behaviour_table[name][conditions[0]])],
        "cost": cost,
        "failed_calls": failures,
        "verdict": verdict_for(ci, preregistered=preregistered,
                               threshold=declared_threshold),
        "interpretation": (
            "COMPLIANCE ONLY. This measures whether the preamble changes output "
            "in the direction it specifies, which is close to circular by "
            "construction: the preamble asks for the behaviours the checks look "
            "for. The informative outcome is a NULL one — it would mean the rules "
            "are being ignored. Compliance is not benefit; that a stated "
            "limitation reduces downstream defects is a separate study. The "
            "definitive validation is Pass@1 on an established issue-resolution "
            "benchmark with the model held fixed, and it has not been run."),
        "padded_control": (
            "run with --conditions bare padded to test whether length alone "
            "reproduces the effect; without it, content and token count are "
            "confounded"
            if "padded" not in conditions else "included in this run"),
    }

def append_trials(path: str, trials: "Sequence[dict]") -> None:
    """Append trials as JSONL. One line per trial, flushed as it lands.

    Appended rather than rewritten so an interrupted run keeps what it already
    paid for. A six-task run is ~24 minutes of sequential calls, and losing it to
    a killed shell means it does not get repeated.
    """
    import json

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for trial in trials:
            handle.write(json.dumps(trial, ensure_ascii=False) + "\n")
            handle.flush()


def read_trials(paths: "Sequence[str]") -> "tuple[list[dict], list[str]]":
    """`(trials, problems)` from one or more JSONL files.

    A malformed line is reported rather than skipped: a summary computed over
    silently dropped trials is a summary of an unknown subset.
    """
    import json

    trials: list[dict] = []
    problems: list[str] = []
    for path in paths:
        if not os.path.exists(path):
            problems.append(f"{path}: missing")
            continue
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    problems.append(f"{path}:{number}: {exc}")
                    continue
                # Validated on `condition`, which every trial in this repository
                # has. Requiring `task` rejected every SWE-bench trial as
                # malformed — those are keyed by `instance_id` — and the summary
                # then reported 0 trials read from a file holding 18 of them. A
                # validator that knows only one caller's schema silently discards
                # the other's.
                if not isinstance(record, dict) or "condition" not in record:
                    problems.append(f"{path}:{number}: not a trial record")
                    continue
                trials.append(record)
    return trials, problems


def deduplicate(trials: "Sequence[dict]") -> "tuple[list[dict], int]":
    """Drop repeated (task, condition, rep) cells, preferring a SUCCESSFUL trial.

    Pooling overlapping batches would otherwise weight some cells twice and move
    the mean with nothing looking wrong.

    Keeping the first would be simpler and is wrong. A cell can hold a failed call
    - one of these batches lost `n-plus-one/bare#1` to a timeout that was my own
    `--timeout 120` being tighter than the provider's observed 108s - and a failed
    call carries no score at all, so an `ok` trial for the same cell is strictly
    more informative than a timeout that happened to be recorded first. Preferring
    `ok` is what makes re-running a lost cell actually repair the pool instead of
    appending a line the summary ignores.

    Among trials of the same status the first still wins, so this never silently
    replaces one measurement with another.
    """
    best: dict = {}
    order: list = []
    dropped = 0
    for trial in trials:
        key = (trial.get("task"), trial.get("condition"), trial.get("rep"))
        if key not in best:
            best[key] = trial
            order.append(key)
            continue
        dropped += 1
        # Upgrade only from failed to ok; never ok -> ok, never ok -> failed.
        if not best[key].get("ok") and trial.get("ok"):
            best[key] = trial
    return [best[key] for key in order], dropped
