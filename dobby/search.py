"""Solution-tree search, and composable inference-time layers.

Two mechanisms that compose
---------------------------
**Tree search over solutions** is the outer loop. Published ML-engineering
results show trial-and-error framed as a *tree* search over candidate solutions
substantially outperforms linear "keep improving the one thing you have"
agents — on MLE-Bench, a tree-search agent reached a 16.9% medal rate against
4.4% for the strongest linear agent at the same model tier. The reason is
structural: a linear agent that takes a wrong turn carries it forever, because
its only move is to improve its current state. A tree keeps the abandoned
branches available.

**Layer composition** is the inner loop. Instead of one model call per node,
compose sample → rank → fuse → critique → verify. Published configurations built
only from open-weight models, composed this way, beat a single frontier call.

Composing them is the point: the tree decides *which solution to work on next*,
and the layers decide *how much inference to spend producing it*. Either alone
leaves the other's failure mode intact — a tree of single-shot nodes explores
widely but shallowly; a deep layer stack on one node polishes a dead end.

The policy is hard-coded on purpose
-----------------------------------
`DRAFT` until there are enough initial solutions, then `DEBUG` while a buggy node
remains within a bounded debug depth, then `IMPROVE` the best non-buggy node.
That ordering is not a heuristic to be tuned away:

- Drafting first buys diversity while it is cheap. Improving a single early draft
  is the linear-agent failure the tree exists to avoid.
- The **debug depth bound** is the load-bearing part. Without it, an agent spends
  its entire budget repairing one broken script, because a buggy node always
  looks like it is one fix away. The bound converts "keep trying" into "abandon
  this branch and draft another".
- Improving the *best* node, not the newest, is what makes the search greedy in
  the right direction.

Overfitting the selection metric
--------------------------------
A search that selects on a metric will overfit that metric — this is the same
multiple-comparison problem `mlops.multiple_comparison_note` quantifies, and a
tree search makes it worse by trying more configurations. The mitigation used
here is the one that actually works: **split off a holdout BEFORE the search
starts**, keep it out of every node's reach, and score the winner on it exactly
once. `SearchResult.holdout_note` refuses to report a final number without it.

No model calls live in this module. `expand` takes a callable, so the search is
testable deterministically and can be driven by any provider from
`dobby/providers/`.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Sequence

DRAFT = "draft"
DEBUG = "debug"
IMPROVE = "improve"

#: Terminal reasons, kept distinct because they mean different things about the
#: result. Exhausting a budget is not the same as converging, and reporting both
#: as "finished" would hide that the search was cut off mid-climb.
STOP_BUDGET = "budget_exhausted"
STOP_CONVERGED = "no_improvement_within_patience"
STOP_TARGET = "target_metric_reached"
STOP_ALL_BUGGY = "every_branch_buggy_within_debug_depth"


@dataclasses.dataclass
class Node:
    """One candidate solution in the tree.

    `score` is `None` for a buggy node rather than a sentinel like `-inf`. A
    sentinel would let a buggy node participate in "best node" comparisons and
    silently win when every alternative is worse, which is how a broken script
    becomes the reported solution.
    """

    id: str
    parent: str | None
    action: str
    content: str
    score: float | None = None
    buggy: bool = False
    error: str = ""
    #: Distance from the nearest non-buggy ancestor. Bounded by `debug_depth`.
    debug_depth: int = 0
    #: Extra provenance the caller wants carried (provider, cost, duration).
    meta: dict = dataclasses.field(default_factory=dict)

    def viable(self) -> bool:
        return not self.buggy and self.score is not None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SearchResult:
    """Outcome of a search, with the honesty caveats attached."""

    best: Node | None
    nodes: list[Node]
    stopped_because: str
    drafts: int
    debugs: int
    improves: int
    buggy_count: int
    holdout_score: float | None
    holdout_note: str
    history: list[dict]

    @property
    def evaluated(self) -> int:
        return len(self.nodes)

    def summary(self) -> dict:
        return {
            "best_id": self.best.id if self.best else None,
            "best_score": self.best.score if self.best else None,
            "nodes_evaluated": self.evaluated,
            "actions": {"draft": self.drafts, "debug": self.debugs,
                        "improve": self.improves},
            "buggy": self.buggy_count,
            "stopped_because": self.stopped_because,
            "holdout_score": self.holdout_score,
            "holdout_note": self.holdout_note,
            # The selection metric was optimized over `evaluated` candidates, so
            # it is biased upward by the search itself. Saying so next to the
            # number is the difference between a result and a lucky maximum.
            "selection_bias_warning": (
                f"the best score was selected from {self.evaluated} candidates "
                "on the same metric; it is optimistically biased and is not an "
                "estimate of held-out performance"),
        }


def _next_action(nodes: Sequence[Node], *, min_drafts: int, debug_depth: int,
                 higher_is_better: bool) -> tuple[str, Node | None]:
    """The hard-coded policy: DRAFT → DEBUG → IMPROVE.

    Returns the action and the node it applies to (None for DRAFT, which has no
    parent). Returns `("", None)` when nothing is actionable, which the caller
    reports as `STOP_ALL_BUGGY` rather than looping.

    `higher_is_better` must be threaded in here, not only applied to the final
    selection. IMPROVE picks the node the search will build on for the rest of
    its budget, so getting the direction wrong here means every remaining call
    refines the *worst* candidate while the final answer is still chosen
    correctly — a search that looks like it worked and spent its budget climbing
    the wrong way.
    """
    if len([n for n in nodes if n.action == DRAFT]) < min_drafts:
        return DRAFT, None

    # DEBUG the shallowest repairable buggy node. Shallowest first because a
    # bug near a working ancestor is likelier to be a small, local mistake than
    # one four repairs deep.
    #
    # "Already attempted" is part of repairability, not just depth. Bounding the
    # chain depth alone does NOT bound the work: a shallow buggy node stays
    # eligible forever, so the policy keeps returning to it and the whole budget
    # drains into repeated retries of the same broken draft — precisely the
    # failure the depth bound exists to prevent, arriving by a different route.
    # One repair attempt per node, chains capped at `debug_depth`, makes the
    # broken region of the tree finite.
    attempted = {n.parent for n in nodes if n.action == DEBUG and n.parent}
    repairable = [n for n in nodes
                  if n.buggy and n.debug_depth < debug_depth
                  and n.id not in attempted]
    if repairable:
        repairable.sort(key=lambda n: (n.debug_depth, n.id))
        return DEBUG, repairable[0]

    viable = [n for n in nodes if n.viable()]
    if viable:
        pick = max if higher_is_better else min
        return IMPROVE, pick(viable, key=lambda n: n.score)

    # No drafts left to make, nothing repairable, nothing viable: the branch set
    # is exhausted. Drafting more would be the right move only if the caller
    # allowed a larger draft budget, which it did not.
    return "", None


def search(*, expand: Callable[[str, Node | None, dict], dict],
           max_nodes: int = 20,
           min_drafts: int = 3,
           debug_depth: int = 3,
           patience: int = 6,
           target_score: float | None = None,
           higher_is_better: bool = True,
           holdout_eval: Callable[[Node], float] | None = None,
           summarize: Callable[[Sequence[Node]], str] | None = None) -> SearchResult:
    """Run the tree search.

    `expand(action, parent, context) -> dict` produces one child. The dict needs
    `content`, and may supply `score` (float or None), `buggy` (bool), `error`,
    and `meta`. Returning `score=None` without `buggy=True` is treated as buggy:
    an unscored solution cannot be compared, so it cannot be improved on, and
    calling it viable would let it be selected as best.

    `context` carries `{"summary": str, "best_score": float|None, "attempt": int}`
    so the expander can be told what has already been tried without this module
    prescribing a prompt format.

    `patience` stops the search after N consecutive expansions with no
    improvement to the best score. This is separate from `max_nodes` because the
    two failures differ: a budget stop means "we ran out of money mid-climb", a
    patience stop means "we plateaued". Conflating them makes it impossible to
    tell whether spending more would have helped.

    `higher_is_better=False` flips comparisons for loss-like metrics. Handled
    once, here, rather than asking every caller to negate its scores — a caller
    that forgets to negate gets a search that confidently minimizes quality.
    """
    if max_nodes < 1:
        raise ValueError("max_nodes must be at least 1")
    if min_drafts < 1:
        raise ValueError("min_drafts must be at least 1 — a search with no "
                         "draft phase is a linear agent")

    nodes: list[Node] = []
    history: list[dict] = []
    counts = {DRAFT: 0, DEBUG: 0, IMPROVE: 0}
    stale = 0
    best_so_far: float | None = None

    def better(a: float, b: float | None) -> bool:
        if b is None:
            return True
        return a > b if higher_is_better else a < b

    stopped = STOP_BUDGET
    while len(nodes) < max_nodes:
        action, parent = _next_action(nodes, min_drafts=min_drafts,
                                      debug_depth=debug_depth,
                                      higher_is_better=higher_is_better)
        if not action:
            stopped = STOP_ALL_BUGGY
            break

        context = {
            "summary": summarize(nodes) if summarize else _default_summary(nodes),
            "best_score": best_so_far,
            "attempt": len(nodes) + 1,
            "action": action,
            "parent_id": parent.id if parent else None,
            "parent_error": parent.error if parent else "",
        }
        raw = expand(action, parent, context) or {}

        score = raw.get("score")
        buggy = bool(raw.get("buggy")) or score is None
        node = Node(
            id=raw.get("id") or f"n{len(nodes) + 1}",
            parent=parent.id if parent else None,
            action=action,
            content=raw.get("content", ""),
            score=None if buggy else float(score),
            buggy=buggy,
            error=raw.get("error", "" if not buggy else
                          "no score returned (an unscored solution cannot be "
                          "compared, so it is treated as buggy)"),
            debug_depth=(parent.debug_depth + 1
                         if action == DEBUG and parent else 0),
            meta=dict(raw.get("meta") or {}),
        )
        nodes.append(node)
        counts[action] += 1

        improved = node.viable() and better(node.score, best_so_far)
        if improved:
            best_so_far = node.score
            stale = 0
        else:
            stale += 1

        history.append({
            "step": len(nodes), "action": action,
            "node": node.id, "parent": node.parent,
            "score": node.score, "buggy": node.buggy,
            "best_so_far": best_so_far, "improved": improved,
            "stale_steps": stale,
        })

        if target_score is not None and node.viable() and (
                node.score >= target_score if higher_is_better
                else node.score <= target_score):
            stopped = STOP_TARGET
            break
        # Patience is only meaningful once the draft phase is over AND at least
        # one viable solution exists. Two reasons:
        #   - stopping during drafting aborts the diversity the search depends on;
        #   - "no improvement" describes a plateau, and a run with zero viable
        #     nodes never started climbing. Reporting that as CONVERGED would
        #     hide the real situation, which is that every branch is broken —
        #     a different problem with a different fix.
        if (counts[DRAFT] >= min_drafts and stale >= patience
                and any(n.viable() for n in nodes)):
            stopped = STOP_CONVERGED
            break

    viable = [n for n in nodes if n.viable()]
    best = (max(viable, key=lambda n: n.score) if higher_is_better
            else min(viable, key=lambda n: n.score)) if viable else None

    holdout_score = None
    if best is not None and holdout_eval is not None:
        holdout_score = float(holdout_eval(best))
        note = (f"holdout scored once, after selection: {holdout_score}. This is "
                "the reportable number; the selection score is not")
    elif best is None:
        note = "no viable solution was produced, so there is nothing to score"
    else:
        note = ("NO HOLDOUT EVALUATION was supplied. The best score was chosen "
                f"from {len(nodes)} candidates on the same metric and is "
                "therefore optimistically biased. Do not report it as "
                "performance — split a holdout before the search and score the "
                "winner on it exactly once")

    return SearchResult(
        best=best, nodes=nodes, stopped_because=stopped,
        drafts=counts[DRAFT], debugs=counts[DEBUG], improves=counts[IMPROVE],
        buggy_count=sum(1 for n in nodes if n.buggy),
        holdout_score=holdout_score, holdout_note=note, history=history)


def _default_summary(nodes: Sequence[Node]) -> str:
    """Compact tree state for the expander's context.

    Carries scores, actions, and error headlines only — never node content. A
    summary that inlines every previous script saturates the context window
    within a handful of steps, which is the failure the summarization operator
    exists to prevent.
    """
    if not nodes:
        return "no attempts yet"
    lines = []
    for n in sorted(nodes, key=lambda x: x.id):
        if n.buggy:
            head = (n.error or "unknown error").splitlines()[0][:120]
            lines.append(f"{n.id} [{n.action}] BUGGY: {head}")
        else:
            lines.append(f"{n.id} [{n.action}] score={n.score}")
    viable = [n for n in nodes if n.viable()]
    if viable:
        best = max(viable, key=lambda n: n.score)
        lines.append(f"best so far: {best.id} at {best.score}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Inference-time layer composition
# --------------------------------------------------------------------------

#: Layer kinds and what each contributes. Ordering constraints below exist
#: because several orderings are silently useless rather than merely suboptimal.
LAYER_KINDS: dict[str, str] = {
    "generate": "sample N independent candidates",
    "rank": "score candidates and keep the top K",
    "fuse": "merge the survivors into one answer",
    "critique": "identify defects in the current answer",
    "revise": "apply the critique",
    "verify": "check the answer against an objective test",
}

#: Layers that require more than one input to do anything. Placing `rank` or
#: `fuse` after a stage that collapsed to a single answer is a no-op that still
#: costs a model call, and it looks like a working pipeline in a config file.
_NEEDS_MANY = frozenset({"rank", "fuse"})

#: Layers that collapse N candidates to 1.
_COLLAPSES = frozenset({"fuse"})


@dataclasses.dataclass
class Layer:
    kind: str
    n: int = 1
    keep: int = 1
    provider: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in LAYER_KINDS:
            raise ValueError(f"unknown layer kind {self.kind!r}; "
                             f"expected one of {sorted(LAYER_KINDS)}")
        if self.kind == "generate" and self.n < 1:
            raise ValueError("generate needs n >= 1")
        if self.kind == "rank" and self.keep < 1:
            raise ValueError("rank needs keep >= 1")


def validate_pipeline(layers: Sequence[Layer]) -> dict:
    """Check a layer stack for orderings that cost calls and do nothing.

    This is a static check, run before any inference is spent. The failures it
    catches are all of the same shape — a layer whose precondition no longer
    holds — and every one of them produces a pipeline that *runs* and returns a
    plausible answer, so nothing else would surface them.
    """
    problems: list[dict] = []
    if not layers:
        return {"valid": False, "candidates_at_end": 0,
                "problems": [{"where": "pipeline", "detail": "no layers"}]}

    if layers[0].kind != "generate":
        problems.append({
            "where": "layer 0",
            "detail": f"pipeline starts with '{layers[0].kind}': there is "
                      "nothing to operate on until something has generated"})

    candidates = 0
    for i, layer in enumerate(layers):
        if layer.kind == "generate":
            candidates = layer.n if candidates == 0 else candidates
            continue
        if layer.kind in _NEEDS_MANY and candidates <= 1:
            problems.append({
                "where": f"layer {i} ({layer.kind})",
                "detail": f"'{layer.kind}' needs more than one candidate but "
                          f"only {candidates} exist(s) at this point — a paid "
                          "no-op"})
        if layer.kind == "rank":
            if layer.keep > candidates:
                problems.append({
                    "where": f"layer {i} (rank)",
                    "detail": f"keep={layer.keep} exceeds the {candidates} "
                              "candidates available; the rank cannot discard "
                              "anything"})
            candidates = min(candidates, layer.keep)
        if layer.kind in _COLLAPSES:
            candidates = 1
        if layer.kind == "revise" and not any(
                l.kind == "critique" for l in layers[:i]):
            problems.append({
                "where": f"layer {i} (revise)",
                "detail": "revise with no preceding critique: there is no "
                          "identified defect to apply"})

    if candidates > 1 and layers[-1].kind not in _COLLAPSES:
        problems.append({
            "where": "pipeline end",
            "detail": f"{candidates} candidates remain and the last layer does "
                      "not collapse them — the caller receives a set where it "
                      "expects an answer. Add a fuse, or a rank with keep=1"})

    return {
        "valid": not problems,
        "candidates_at_end": candidates,
        "layer_count": len(layers),
        "generate_calls": sum(l.n for l in layers if l.kind == "generate"),
        "problems": problems,
        "verdict": ("pipeline is well-formed" if not problems else
                    f"{len(problems)} ordering problem(s): each one produces a "
                    "pipeline that runs and quietly wastes calls"),
    }


def suggest_pipeline(*, budget_calls: int, task_kind: str = "general") -> dict:
    """A well-formed stack that fits `budget_calls`.

    Composition is chosen by task kind because the useful layers differ:
    verifiable tasks should spend on `verify` (a cheap objective check beats an
    expensive opinion), while open-ended ones should spend on breadth then
    fusion, since there is nothing to verify against.
    """
    if budget_calls < 1:
        raise ValueError("budget_calls must be at least 1")
    if budget_calls == 1:
        return {"layers": [Layer("generate", n=1)],
                "rationale": "a single call affords no composition; this is the "
                             "honest baseline every stack must beat",
                "validation": validate_pipeline([Layer("generate", n=1)])}

    if task_kind == "verifiable":
        # Objective checks are the cheapest reliable signal, so buy breadth and
        # spend the remainder on verify/revise rather than on a ranker's opinion.
        gen = max(2, budget_calls - 2)
        layers = [Layer("generate", n=gen), Layer("verify"),
                  Layer("rank", keep=1)]
        rationale = ("verifiable task: breadth first, then an objective check. "
                     "A verifier that can be wrong is still cheaper and more "
                     "reliable than a ranker's preference")
    elif task_kind == "open_ended":
        gen = max(3, budget_calls - 2)
        keep = max(2, min(3, gen - 1))
        layers = [Layer("generate", n=gen), Layer("rank", keep=keep),
                  Layer("fuse")]
        rationale = ("open-ended task: nothing to verify against, so spend on "
                     "diverse samples and merge the survivors")
    else:
        gen = max(2, budget_calls - 3)
        layers = [Layer("generate", n=gen), Layer("rank", keep=1),
                  Layer("critique"), Layer("revise")]
        rationale = ("general task: modest breadth, pick one, then one "
                     "critique/revise round — a second round usually restates "
                     "the first")

    return {"layers": layers, "task_kind": task_kind,
            "rationale": rationale,
            "validation": validate_pipeline(layers)}


# --------------------------------------------------------------------------
# Case-based reasoning over past runs
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Case:
    """One past task and what worked on it.

    A case stores the *approach* and its *outcome*, never a copied answer.
    Retrieving and reapplying an answer is plagiarism when the source is someone
    else's and overfitting when it is your own; retrieving an approach is reuse.
    """

    id: str
    task: str
    approach: str
    outcome_score: float | None
    succeeded: bool
    tags: tuple[str, ...] = ()
    #: Whether the outcome was verified by a command, or merely asserted. An
    #: unverified case is a hypothesis about what worked.
    verified: bool = False

    def text(self) -> str:
        return f"{self.task} {self.approach} {' '.join(self.tags)}"


def retrieve_cases(bank: Sequence[Case], task: str, *, k: int = 3,
                   require_success: bool = True) -> dict:
    """Retrieve the most similar past cases, successes ranked above failures.

    Failures are retrievable on request (`require_success=False`) because a
    recorded dead end is the cheapest possible saving — rediscovering it costs a
    full attempt. They are returned in a separate list so a caller cannot
    accidentally reuse a failed approach as a template.
    """
    from .swarm.diversity import jaccard_distance, token_set

    q = token_set(task)
    scored = []
    for case in bank:
        sim = 1.0 - jaccard_distance(q, token_set(case.text()))
        # Verified beats unverified at equal similarity, matching the rest of
        # the kit's authority rule.
        scored.append((sim + (0.1 if case.verified else 0.0), sim, case))
    scored.sort(key=lambda t: (-t[0], t[2].id))

    successes = [(s, c) for _, s, c in scored if c.succeeded]
    failures = [(s, c) for _, s, c in scored if not c.succeeded]

    return {
        "task": task,
        "reuse": [{"id": c.id, "similarity": round(s, 4), "approach": c.approach,
                   "score": c.outcome_score, "verified": c.verified}
                  for s, c in successes[:k]],
        "avoid": [] if require_success else
                 [{"id": c.id, "similarity": round(s, 4),
                   "approach": c.approach, "why_failed": c.approach}
                  for s, c in failures[:k]],
        "bank_size": len(bank),
        "note": ("no similar prior case: proceed from first principles and "
                 "RETAIN the outcome so the next run has one"
                 if not successes else
                 f"{len(successes[:k])} prior approach(es) to adapt — adapt the "
                 "approach, never copy the answer"),
    }


def retain_case(bank: list[Case], case: Case, *,
                min_novelty: float = 0.2) -> dict:
    """Add a case to the bank unless it duplicates one already there.

    Duplicate cases make retrieval return the same advice k times, which crowds
    out the second-most-relevant approach. Novelty is checked against the task
    text, not the outcome, so two different tasks with the same approach both
    stay.
    """
    from .swarm.diversity import jaccard_distance, token_set

    q = token_set(case.text())
    if not q:
        return {"retained": False, "reason": "case has no content"}
    for existing in bank:
        sim = 1.0 - jaccard_distance(q, token_set(existing.text()))
        if sim > (1.0 - min_novelty):
            if case.verified and not existing.verified:
                bank.remove(existing)
                bank.append(case)
                return {"retained": True,
                        "reason": f"replaced unverified duplicate {existing.id}"}
            return {"retained": False,
                    "reason": f"{sim:.0%} similar to existing case "
                              f"{existing.id}"}
    bank.append(case)
    return {"retained": True, "reason": "novel case", "bank_size": len(bank)}


def yield_report(*, attempts: int, genuine: int) -> dict:
    """Report the hit rate of an autonomous improvement loop, honestly.

    Included because published autonomous-research runs report yields on the
    order of a few percent — roughly 20 genuine improvements from about 700
    experiments in one widely-cited run. A loop that reports only its successes
    implies a hit rate it does not have, and the *expected* rate is what tells a
    user whether to fund another thousand attempts.
    """
    if attempts <= 0:
        return {"attempts": 0, "note": "no attempts recorded"}
    rate = genuine / attempts
    return {
        "attempts": attempts,
        "genuine_improvements": genuine,
        "yield_rate": round(rate, 4),
        "attempts_per_improvement": (round(attempts / genuine, 1)
                                     if genuine else None),
        "note": (f"{genuine}/{attempts} attempts produced a genuine improvement "
                 f"({rate:.1%}). Reporting the successes alone would imply a "
                 "hit rate this loop does not have"
                 if genuine else
                 f"0/{attempts} attempts produced a genuine improvement — the "
                 "loop is running and finding nothing, which is a result worth "
                 "reporting rather than hiding"),
        # A low yield is normal and is not evidence the loop is broken. Saying so
        # stops a user from tuning away the very gates that make the yield honest.
        "calibration": ("single-digit percent yields are the published norm for "
                        "autonomous improvement loops; a suspiciously high rate "
                        "usually means the validation gate is too weak"),
    }
