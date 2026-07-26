"""Wire `search.search` to real providers, with a real objective.

`dobby/search.py` implemented the policy — DRAFT until there are enough drafts,
DEBUG a buggy node to a bounded depth, otherwise IMPROVE the best viable one — and
the whole thing had never executed a model call. That was recorded as a gap. This
closes it, and the interesting part is not the plumbing.

WHERE THE SCORE COMES FROM

A tree search climbs whatever gradient it is given. If the gradient is the model's
own opinion of its own output, the search maximises self-assessment, and it will
report steady improvement while producing nothing better — the failure is
invisible because the metric and the artifact come from the same place.

So a score here comes from **running a command**, never from a judgment.
`.dobby/ontology.json` already draws this line for evidence; a search objective is
the same line in a different place. `dobby/judge.py` exists for the cases where
only a model can answer, and it is careful to mark its answers advisory precisely
because they cannot bear this kind of weight.

`search()` already refuses to treat an unscored candidate as viable: `score=None`
without `buggy=True` is treated as buggy, because something uncomparable cannot be
selected as best. This module leans on that rather than inventing a fallback
number. A driver with no scorer produces no viable nodes, and says so.

WHAT A FAILED PROVIDER CALL IS

Not a bad candidate — no candidate. A timeout, an auth error, or a shim that
cannot deliver the prompt all yield `buggy=True` with the provider's error, so the
node is never selectable and the reason survives into the result. Scoring a failed
call as 0.0 would let infrastructure trouble read as a hard search problem.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Sequence

from .core.platform import child_env, resolve_command
from .core.security import cap_output, guard_command, load_protected, redact_secrets
from .search import Node

#: Placeholder in a score command, replaced with the path the candidate was
#: written to. Without it the scorer cannot see what it is scoring.
CANDIDATE_PLACEHOLDER = "{candidate}"

#: Candidate text handed to a provider as context. Long enough for a real
#: solution, short enough that a DEBUG prompt carrying parent + error still fits.
MAX_CONTEXT_CHARS = 8_000

_FLOAT = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

DRAFT_PROMPT = """{task}

Produce a complete solution. Output only the solution itself, with no commentary
before or after it.
"""

DEBUG_PROMPT = """{task}

A previous attempt failed. Here it is, followed by the failure.

--- PREVIOUS ATTEMPT ---
{parent}
--- FAILURE ---
{error}

Fix the cause of that failure. Output only the corrected solution, with no
commentary before or after it.
"""

IMPROVE_PROMPT = """{task}

The best attempt so far scored {score}. Here it is.

--- CURRENT BEST ---
{parent}

{history}
Make one substantive improvement. Output only the improved solution, with no
commentary before or after it.
"""


def candidate_dir(data_dir: str) -> str:
    """Runtime location for candidate files. Gitignored, like all state."""
    path = os.path.join(data_dir, "state", "search")
    os.makedirs(path, exist_ok=True)
    return path


def write_candidate(data_dir: str, index: int, content: str) -> str:
    """Persist one candidate so a score command has something to run against.

    Under `.dobby/state/`, which `.gitignore` and both installers already exclude
    — model output is untrusted text and must not travel into a host project as
    though it were part of the kit.
    """
    path = os.path.join(candidate_dir(data_dir), f"candidate_{index:03d}.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def command_scorer(command: str, *, data_dir: str, cwd: str | None = None,
                   timeout_s: int = 300, config: dict | None = None
                   ) -> Callable[[str, int], tuple[float | None, str]]:
    """Build a scorer that runs `command` and reads a number out of its stdout.

    `{candidate}` in the command is replaced with the path the candidate was
    written to. A command without the placeholder is rejected outright rather than
    run: it would score the same unchanged thing every iteration and produce a
    flat curve that looks like a converged search.

    The number is the LAST float in stdout, so a command can print progress and
    finish with its metric. A non-zero exit is not a score of zero — it is a
    failure to produce a score, which keeps a broken harness from reading as a
    genuinely bad candidate.
    """
    if CANDIDATE_PLACEHOLDER not in command:
        raise ValueError(
            f"score command must contain {CANDIDATE_PLACEHOLDER!r}; without it "
            f"every candidate gets the same score and the search reports a "
            f"plateau it never reached")
    protected = load_protected(config)

    def score(content: str, index: int) -> tuple[float | None, str]:
        path = write_candidate(data_dir, index, content)
        # POSIX-style separators: the command is user-supplied text that may be
        # quoted for either shell, and a backslash is an escape in one of them.
        resolved = resolve_command(
            command.replace(CANDIDATE_PLACEHOLDER, path.replace("\\", "/")))
        allowed, reason = guard_command(resolved, protected)
        if not allowed:
            return None, f"score command blocked by the guard: {reason}"
        try:
            proc = subprocess.run(
                resolved, shell=True, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=child_env(),
                timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None, f"score command timed out after {timeout_s}s"
        out = redact_secrets(proc.stdout or "")
        if proc.returncode != 0:
            return None, (f"score command exited {proc.returncode}: "
                          f"{cap_output(redact_secrets(proc.stderr or out), 400).strip()}")
        found = _FLOAT.findall(out)
        if not found:
            return None, ("score command printed no number; it exited 0 without "
                          "producing a metric, which is not a score of zero")
        return float(found[-1]), ""

    return score


def _context_block(node: Node | None) -> str:
    if node is None:
        return ""
    return cap_output(node.content or "", MAX_CONTEXT_CHARS)


def provider_expander(task: str, *,
                      score: Callable[[str, int], tuple[float | None, str]] | None,
                      provider_id: str | None = None,
                      role: str = "draft",
                      exclude: set[str] | None = None,
                      timeout_s: int | None = None,
                      cwd: str | None = None,
                      on_call: Callable[[dict], None] | None = None
                      ) -> Callable[[str, Node | None, dict], dict]:
    """An `expand` callable for `search.search`, backed by a live provider.

    `score=None` is allowed and honest: every candidate then comes back
    uncomparable, `search()` treats it as buggy, and the result contains the
    reason instead of a ranking built on nothing.
    """
    calls: list[dict] = []

    def expand(action: str, parent: Node | None, context: dict) -> dict:
        # Imported HERE, not when the expander is built. Binding these names at
        # construction time made a test that patches `dobby.providers.*` around
        # the CALL silently ineffective, so the "mocked" test issued real paid
        # provider calls and hung on a 900s timeout. Resolving through the module
        # at each call keeps the seam where every caller expects it.
        from .providers import resolve_role, run_by_id

        chosen = provider_id or resolve_role(role, exclude=exclude or set())
        if not chosen:
            return {"content": "", "buggy": True,
                    "error": f"no usable provider for role {role!r}"}

        if action == "DEBUG" and parent is not None:
            prompt = DEBUG_PROMPT.format(
                task=task, parent=_context_block(parent),
                error=parent.error or "(the attempt produced no comparable score)")
        elif action == "IMPROVE" and parent is not None:
            prompt = IMPROVE_PROMPT.format(
                task=task, parent=_context_block(parent),
                score=parent.score,
                history=(f"Already tried:\n{context['summary']}\n"
                         if context.get("summary") else ""))
        else:
            prompt = DRAFT_PROMPT.format(task=task)

        started = time.monotonic()
        result = run_by_id(chosen, prompt, timeout_s=timeout_s, cwd=cwd)
        record = {"action": action, "provider": chosen,
                  "duration_s": round(time.monotonic() - started, 2),
                  "ok": result.ok, "attempt": context.get("attempt")}
        calls.append(record)

        if not result.ok:
            # No candidate, rather than a bad one. Scoring this 0.0 would make an
            # auth failure indistinguishable from a hard problem.
            record["error"] = result.error
            if on_call:
                on_call(record)
            return {"content": "", "buggy": True,
                    "error": f"{chosen} failed: {result.error}",
                    "meta": record}

        content = result.text or ""
        if not content.strip():
            record["error"] = "empty reply"
            if on_call:
                on_call(record)
            return {"content": "", "buggy": True,
                    "error": f"{chosen} exited 0 with no output", "meta": record}

        if score is None:
            record["error"] = "no scorer"
            if on_call:
                on_call(record)
            return {"content": content, "score": None, "buggy": True,
                    "error": ("no score command was supplied, so this candidate "
                              "cannot be compared with any other"),
                    "meta": record}

        value, why = score(content, len(calls))
        record["score"] = value
        record["score_error"] = why
        if on_call:
            on_call(record)
        if value is None:
            return {"content": content, "score": None, "buggy": True,
                    "error": why, "meta": record}
        return {"content": content, "score": value, "buggy": False,
                "meta": record}

    expand.calls = calls          # type: ignore[attr-defined]
    return expand


def driver_report(result, calls: Sequence[dict]) -> dict:
    """Search outcome plus what it cost and how often the providers answered.

    Separated from the search result because the two answer different questions.
    A search that stopped on patience after four failed provider calls has not
    plateaued; it never got started, and one number cannot say both.
    """
    ok = [c for c in calls if c.get("ok")]
    scored = [c for c in ok if c.get("score") is not None]
    return {
        "provider_calls": len(calls),
        "answered": len(ok),
        "scored": len(scored),
        "unscored_answers": len(ok) - len(scored),
        "agent_seconds": round(sum(c.get("duration_s", 0.0) for c in calls), 2),
        "by_action": {action: sum(1 for c in calls if c["action"] == action)
                      for action in {c["action"] for c in calls}},
        "note": ("`answered` counts provider replies; `scored` counts replies a "
                 "command could measure. A gap between them means the search had "
                 "candidates it could not compare, and its stop reason says less "
                 "than it appears to."),
    }
