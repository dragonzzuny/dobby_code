"""One provider call, fully gated — and a report nobody paid a model to write.

WHY THIS IS THE DEFAULT NOW

The pilot measured B_gated at 1.9% more cost than a bare direct call while
keeping the effect contract, the acceptance gate and artifact promotion. The
safety is nearly free. What was expensive was `default_graph`'s `plan` and
`report` provider nodes, which ran on every item and cost 2.94x per verified
task for no measured gain.

So the fast path is exactly B_gated promoted to a product path: one `execute`
node carrying the side-effect class, the expected paths and the item's own
acceptance checks. Everything the runtime does around it — leases, the verifier
gate, effect observation, PROPOSED -> VERIFIED -> PROMOTED — is unchanged,
because none of that was the cost.

THE REPORT IS ASSEMBLED, NOT GENERATED

`default_graph`'s `report` node asks a model to describe what just happened. The
run store already knows what happened: node states, promoted artifact ids, the
acceptance verdict, and which paths changed. A model restating that is a paid
paraphrase of a record — and one that can disagree with it, which is worse than
not having it.

`deterministic_report` composes the same fields from the record. A narrative
report remains available and becomes an explicit, separately requested step: the
one case where a model adds something the record does not contain is when
somebody wants prose ABOUT the record, and that is a choice with a price tag
rather than a default.
"""

from __future__ import annotations

from ..runtime import graph as G
from ..runtime.contracts import LOCAL_WRITE, NONE, ArtifactContract


def build_order(item, profile) -> str:
    """What the single worker is told: the task, its scope, and its limits.

    The scope is stated because the effect check will enforce it. A worker told
    only the objective and then failed for touching a file nobody mentioned was
    given a rule it could not see.
    """
    lines = [item.outcome or item.title, ""]
    if profile.expected_paths:
        lines.append(f"You may change ONLY: {', '.join(profile.expected_paths)}")
        lines.append("Anything else you notice is a finding to report, not a "
                     "file to edit.")
    else:
        lines.append("Keep the change as small as the task allows.")
    if item.acceptance_checks:
        lines.append("")
        lines.append("This is done when these commands pass, and they will be "
                     "run against your work:")
        lines.extend(f"  {c}" for c in item.acceptance_checks)
    lines.append("")
    lines.append("Do not modify the checks themselves.")
    return "\n".join(lines)


def direct_gated_graph(item, profile, *, provider: str | None = None,
                       execute_command: str | None = None,
                       static: bool = False, timeout_s: int | None = None
                       ) -> G.TaskGraph:
    """A single gated node. One provider call, every guard the slow path had.

    No `plan` node: the worker is being told the task and the scope, and a
    separate model call to restate them is what the measurement priced at 3x.
    No `report` node: see `deterministic_report`.
    """
    writes = profile.side_effect_class in ("LOCAL_WRITE",
                                           "EXTERNAL_REVERSIBLE",
                                           "EXTERNAL_IRREVERSIBLE")
    worker = ("command" if execute_command
              else ("provider" if provider else "static"))
    config: dict
    if worker == "command":
        config = {"command": execute_command}
    elif worker == "provider":
        config = {"provider": provider}
        if timeout_s:
            config["timeout_s"] = timeout_s
    else:
        config = {"payload": {"summary": item.outcome or item.title}}

    return G.TaskGraph([G.TaskNode(
        node_id="execute", kind="implement", worker=worker,
        instruction=build_order(item, profile),
        contract=ArtifactContract(
            side_effect_class=(profile.side_effect_class if writes else NONE),
            expected_paths=list(profile.expected_paths),
            acceptance_checks=list(item.acceptance_checks)),
        config=config)])


def deterministic_report(item, result, *, execution_class: str = "",
                         reason: str = "") -> dict:
    """The run, described from the record rather than by a model.

    Every field is read from something that already happened. `unestablished` is
    present and explicit for the same reason `runtime/metrics.py` keeps its
    `unmeasured` list: a report that omits what it could not show reads as a
    report in which everything was shown.
    """
    steps = list(getattr(result, "steps", []) or [])
    promoted = [s.artifact_id for s in steps if getattr(s, "artifact_id", None)]
    failed = [{"node": s.node_id,
               "class": (s.failure or {}).get("failure_class"),
               "detail": ((s.failure or {}).get("detail") or "")[:300]}
              for s in steps if (s.failure or {})]
    effects = [s.verdict.get("effect_detail") for s in steps
               if isinstance(getattr(s, "verdict", None), dict)
               and s.verdict.get("effect_detail")]

    unestablished = []
    if not item.acceptance_checks:
        unestablished.append("this item declared no acceptance check, so a "
                             "passing run proves only that nothing errored")
    if not promoted:
        unestablished.append("no artifact was promoted, so nothing here passed "
                             "a gate")

    return {
        "work_item_id": item.work_item_id,
        "outcome": item.outcome or item.title,
        "execution_class": execution_class,
        "why_this_class": reason,
        "run_state": getattr(result, "state", ""),
        "nodes": [{"node": s.node_id, "state": s.state, "attempts": s.attempts,
                   "worker": s.worker} for s in steps],
        "acceptance_checks": list(item.acceptance_checks),
        "promoted_artifacts": promoted,
        "changed": effects,
        "failures": failed,
        "unestablished": unestablished,
        "source": ("assembled from the run record; no provider call was made to "
                   "write this"),
    }
