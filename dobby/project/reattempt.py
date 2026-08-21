"""Retry a blocked item — but only after something about it has changed.

THE CONSTRAINT THIS WORKS INSIDE

`project/loop.py` stops on ITEM_BLOCKED and says why: "repeating it unchanged is
the one action guaranteed not to help". That is right, and it is also why a
research lifecycle cannot get past its first bad artifact without a person. The
stages that fail here fail in a very particular way — a literature artifact with
four sources instead of five, an ideation artifact whose ideas carry no
falsifiable test — and `project/evidence.py` already computed the exact repair
for each. Nothing about that needs a human. What needed a human was the absence
of anything that would apply it.

So this does not weaken the rule. It satisfies it. Before every re-entry it
derives a repair from the item's OWN failing acceptance check, writes that repair
into the item, and only then calls `advance` again. If no repair can be derived,
it does not retry — it stops with NO_REPAIR_DERIVED, which is a different and
more useful answer than trying once more and failing identically.

WHY THIS IS A WRAPPER AND NOT AN EDIT TO THE LOOP

`loop.advance` is a kernel: it applies the project invariants and every stop it
emits is a boundary somebody chose. Adding a retry counter inside it would put a
"try again" path next to the invariants that decide when trying again is
forbidden, and the next reader would have to work out which wins. Here the
ordering is visible in the call stack — the kernel refuses, this decides whether
the refusal was repairable, and the kernel gets the final word either way.

WHAT IT WILL NOT DO

- It never lowers an acceptance check. Editing the gate to fit the output is
  Evaluation Gaming (docs/FAILURE_CATALOG.md) and it is task failure, not a
  recovery strategy.
- It never retries an item whose check is not an artifact check. A failing test
  suite is not repaired by re-running it.
- It never exceeds `max_attempts` per item, and it reports the count it used.
"""

from __future__ import annotations

import os
import shlex

from . import architecture as A
from . import evidence as E
from .loop import ITEM_BLOCKED, advance
from .models import OPEN
from .store import ProjectStore

# -- stop reasons this module adds to `loop.STOP_REASONS` --------------------
#: The item failed and nothing about the failure suggested a change to make.
NO_REPAIR_DERIVED = "no_repair_derived"
#: A repair was applied and the item failed again, up to the caller's ceiling.
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
#: The architect was asked to reconsider and did not apply a plan. Its own reason
#: is carried in `detail`; this only says the retry was not licensed.
REPLAN_NOT_APPLIED = "replan_not_applied"

STOP_REASONS = (NO_REPAIR_DERIVED, ATTEMPTS_EXHAUSTED, REPLAN_NOT_APPLIED)

#: Where an applied REPLAN is noted on the item, distinct from a repair so the
#: two are never confused in a later read: one says "your artifact was wrong",
#: the other says "your approach was".
REPLAN_MARKER = (
    "\n\n--- REPLANNED (the architect reconsidered after a failure) ---\n")

#: Where an applied repair is written into the item's outcome. Everything from
#: this marker to the end is REPLACED on each attempt rather than appended to, so
#: an item retried three times carries the current directive and not a stack of
#: three, which would grow the worker's prompt with instructions it already met.
REPAIR_MARKER = "\n\n--- REPAIR DIRECTIVE (from the failing acceptance check) ---\n"

#: A hard ceiling, matching the spirit of `loop.DRAIN_CEILING`: an operator may
#: choose more attempts than this only by invoking again, deliberately.
ATTEMPT_CEILING = 5


#: What may sit immediately before `project` for this to be a dobby invocation.
#: `python -m dobby.cli` leaves `dobby.cli`; the installed shims leave their own
#: basename. Anything else — `echo`, a wrapper script, a shell builtin — is a
#: command that merely mentions these words, and re-running it would derive a
#: repair from an artifact this module never actually graded.
INVOCATIONS = ("dobby.cli", "dobby", "dobby.sh", "dobby.exe", "dobby.cmd",
               "dobby.bat", "dobby.ps1")


def parse_artifact_check(command: str) -> dict | None:
    """Recognise `dobby project check --kind K --file F [--min N]`, or return None.

    Parsed rather than pattern-matched on a substring, because a check that
    merely CONTAINS those words is not one this module can re-run and interpret.
    The caller who invokes something else that happens to print the same tokens
    gets no repair and an honest NO_REPAIR_DERIVED, which is the correct answer.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if "check" not in parts or "project" not in parts:
        return None
    index = parts.index("project")
    if index + 1 != parts.index("check") or index == 0:
        return None
    caller = os.path.basename(parts[index - 1].replace("\\", "/")).lower()
    if caller not in INVOCATIONS:
        return None
    out: dict = {}
    for flag, key in (("--kind", "kind"), ("--file", "file"), ("--min", "min")):
        if flag in parts:
            index = parts.index(flag)
            if index + 1 < len(parts):
                out[key] = parts[index + 1]
    if out.get("kind") not in E.KINDS or not out.get("file"):
        return None
    if "min" in out:
        try:
            out["min"] = int(out["min"])
        except ValueError:
            out.pop("min")
    return out


def derive_repair(item, *, root: str) -> dict | None:
    """Re-run the item's artifact checks and turn their failures into instructions.

    Returns None when there is nothing to act on: no artifact check, or every
    artifact check now passes. The second case matters — an item can be BLOCKED
    for a reason that has nothing to do with its artifact (the run crashed, the
    worker was absent), and inventing an artifact repair for it would send the
    next attempt to fix something that is not broken.
    """
    findings: list[dict] = []
    for command in item.acceptance_checks:
        parsed = parse_artifact_check(command)
        if not parsed:
            continue
        verdict = E.check_file(parsed["kind"], parsed["file"],
                               min_rows=parsed.get("min"), root=root)
        if verdict["ok"]:
            continue
        findings.append({
            "kind": parsed["kind"],
            "artifact": parsed["file"],
            "failures": verdict["failures"],
            # Present only for the ideation family, where `explore_cycle` has
            # per-idea repairs rather than per-artifact ones.
            "repairs": verdict.get("repairs", {}).get("items", []),
        })
    if not findings:
        return None

    lines = [
        "The previous attempt did not satisfy this item's own acceptance check. "
        "Each line below is a condition that failed, measured against the "
        "artifact you produced. Fix the artifact. Do NOT change the acceptance "
        "check, and do not lower a threshold to make it pass."]
    for finding in findings:
        lines.append("")
        lines.append(f"ARTIFACT: {finding['artifact']} (kind: {finding['kind']})")
        for failure in finding["failures"]:
            lines.append(f"  - {failure}")
        for repair in finding["repairs"]:
            lines.append(f"  idea {repair['title']!r}:")
            for fix in repair["repairs"]:
                lines.append(f"    fix: {fix}")
    return {"instruction": "\n".join(lines), "findings": findings}


def _apply(store: ProjectStore, project_id: str, item, repair: dict,
           attempt: int) -> None:
    """Write the repair into the item and reopen it. The change that licenses a retry."""
    base = item.outcome.split(REPAIR_MARKER)[0]
    item.outcome = (f"{base}{REPAIR_MARKER}attempt {attempt}\n"
                    f"{repair['instruction']}")
    item.state = OPEN
    item.blocked_reason = ""
    version = store.load_project(project_id)["portfolio"].version
    store.update_item(item, expected_version=version,
                      reason=f"repair derived from failing acceptance check "
                             f"(attempt {attempt})")


def _replan(store, data_dir, project_id, item, *, session_id, provider,
            propose, allow_network):
    """Ask the architect to reconsider, and reopen the item if it did.

    Returns `(retryable, decision)`. Reopening is what licenses the next attempt
    under this module's own rule — an applied plan changed the item's contract,
    which is a recorded change and not a repeat.
    """
    from .replan import request_replan

    decision = request_replan(
        data_dir, item, project_id=project_id, session_id=session_id,
        provider=provider, propose=propose, allow_network=allow_network)
    if decision.outcome != A.APPLIED:
        return False, decision

    # Re-read: `request_architecture` wrote the plan onto the item and bumped
    # the portfolio, and the copy in hand predates that.
    fresh = store.load_project(project_id)["portfolio"].get(item.work_item_id)
    base = fresh.outcome.split(REPAIR_MARKER)[0].split(REPLAN_MARKER)[0]
    fresh.outcome = (f"{base}{REPLAN_MARKER}plan {decision.plan_id}: "
                     f"{decision.reason or 'applied'}")
    fresh.state = OPEN
    fresh.blocked_reason = ""
    version = store.load_project(project_id)["portfolio"].version
    store.update_item(fresh, expected_version=version,
                      reason=f"replanned by {decision.plan_id}")
    return True, decision


def persevere(data_dir: str, *, project_id: str | None = None,
              max_attempts: int = 2, replan: bool = False,
              replan_provider: str | None = None, replan_propose=None,
              **advance_kwargs) -> dict:
    """`advance` one item at a time, repairing a blocked item between attempts.

    Every keyword `loop.advance` takes is passed through, except `max_items`,
    which is fixed at 1: this module's whole subject is one item's repeated
    attempts, and draining a portfolio at the same time would make the attempt
    counter meaningless.

    The result carries every attempt, and `stopped` is either one of
    `loop.STOP_REASONS` (the kernel had the final word) or one of this module's
    two (the retry decision did).
    """
    advance_kwargs.pop("max_items", None)
    ceiling = min(max(1, int(max_attempts)), ATTEMPT_CEILING)

    store = ProjectStore(data_dir)
    project = store.load_project(project_id)
    pid = project["project_id"]
    root = project["manifest"].root

    attempts: list[dict] = []
    repairs_applied = 0
    replans_applied = 0
    stopped, detail = ATTEMPTS_EXHAUSTED, f"no attempt was made (ceiling {ceiling})"

    for attempt in range(1, ceiling + 1):
        result = advance(data_dir, project_id=pid, max_items=1, **advance_kwargs)
        attempts.append({"attempt": attempt, "stopped": result["stopped"],
                         "detail": result["detail"],
                         "items_completed": result["items_completed"],
                         "iterations": result["iterations"]})
        stopped, detail = result["stopped"], result["detail"]

        if result["stopped"] != ITEM_BLOCKED:
            break

        blocked_id = (result["iterations"][-1]["work_item_id"]
                      if result["iterations"] else None)
        if not blocked_id:                       # pragma: no cover - defensive
            break

        item = store.load_project(pid)["portfolio"].get(blocked_id)
        repair = derive_repair(item, root=root)
        if repair is None:
            # The deterministic path had nothing to say. That is exactly the
            # case a replan is for — and it is tried SECOND on purpose: a repair
            # derived from a failing check costs nothing and is right whenever
            # the artifact was the problem, so spending a model call before
            # trying it would invert the ordering.
            if replan and (replan_provider or replan_propose or
                           advance_kwargs.get("provider")):
                retryable, decision = _replan(
                    store, data_dir, pid, item,
                    session_id=result["iterations"][-1].get("session_id", ""),
                    provider=replan_provider or advance_kwargs.get("provider"),
                    propose=replan_propose,
                    allow_network=advance_kwargs.get("allow_network", False))
                attempts[-1]["replan"] = {"outcome": decision.outcome,
                                          "plan_id": decision.plan_id,
                                          "reason": decision.reason}
                if retryable and attempt < ceiling:
                    replans_applied += 1
                    continue
                if retryable:
                    replans_applied += 1
                    stopped = ATTEMPTS_EXHAUSTED
                    detail = (f"{blocked_id} was replanned by "
                              f"{decision.plan_id} on the last permitted "
                              f"attempt; the plan is applied and a further "
                              f"invocation continues from it")
                    break
                stopped = REPLAN_NOT_APPLIED
                detail = (f"{blocked_id}: the architect was asked to reconsider "
                          f"and answered {decision.outcome} — {decision.reason}")
                break
            stopped = NO_REPAIR_DERIVED
            detail = (f"{blocked_id} is blocked and its acceptance checks give "
                      f"nothing to change: either they are not artifact checks, "
                      f"or they pass and the run failed for another reason. "
                      f"Retrying unchanged cannot help — a person decides this "
                      f"one")
            attempts[-1]["repair"] = None
            break

        if attempt == ceiling:
            stopped = ATTEMPTS_EXHAUSTED
            detail = (f"{blocked_id} still blocked after {ceiling} attempt(s); "
                      f"the last repair is written into the item so a person or "
                      f"a later invocation continues from it rather than from "
                      f"the original wording")
            attempts[-1]["repair"] = repair["findings"]
            _apply(store, pid, item, repair, attempt)
            repairs_applied += 1
            break

        _apply(store, pid, item, repair, attempt)
        repairs_applied += 1
        attempts[-1]["repair"] = repair["findings"]

    refreshed = store.load_project(pid)
    return {
        "project_id": pid,
        "stopped": stopped,
        "detail": detail,
        "attempts": attempts,
        "attempts_used": len(attempts),
        "max_attempts": ceiling,
        "repairs_applied": repairs_applied,
        "replans_applied": replans_applied,
        "coverage": refreshed["portfolio"].coverage(),
    }
