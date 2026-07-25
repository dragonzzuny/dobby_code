"""Token efficiency: condense tool output, and account for it honestly.

Where the tokens actually go
---------------------------
An agent's context fills with tool output far faster than with its own reasoning:
a test run, a `git status`, a directory listing, an API response. Three published
approaches attack this from different sides, and this module adopts the mechanism
of each while refusing the overstated accounting that usually accompanies them:

1. **Per-command output condensers.** Generic compression cannot know that in
   `pytest` output the failures matter and the 200 passing dots do not. A handler
   per command family can. Reported reductions of 60–90% are on *bash output*, and
   the tools that report them typically estimate tokens as bytes÷4 with no
   tokenizer involved.
2. **Priority-tiered session snapshots.** When context is about to be compacted,
   what survives should be chosen by priority tier (files modified and active
   decisions before tool-call counts), inside a hard byte budget.
3. **Query-first context selection.** Instead of reading files to find what
   changed, query a structural graph for the blast radius of a change and read
   only that. This is where the largest reductions come from, because it avoids
   reading rather than shrinking what was read.

Honest accounting is part of the feature
----------------------------------------
Every estimate here is labelled with what it does and does not include:

- Condensing tool output reduces **input** tokens on subsequent turns.
- Style constraints on the model's replies reduce **output** tokens, and cost
  input tokens to install (the instruction itself is resident in every turn).
- Neither reduces the model's internal reasoning cost.

`estimate_savings` therefore reports character deltas and a clearly-labelled
token ESTIMATE, never a billing claim. A harness that overstates its savings
teaches its user to distrust its other numbers.

Failures keep their full output
------------------------------
Condensing a failing command's output is the one case where compression actively
harms: the detail that explains the failure is exactly what a summarizer drops.
Every condenser therefore passes non-zero exits through with a preserved full
copy on disk, so the agent can re-read the original.
"""

from __future__ import annotations

import dataclasses
import os
import re
import time
from collections.abc import Sequence

#: Characters per token, used only for ESTIMATES and labelled as such at every
#: call site. There is no tokenizer here: adding one would mean a model
#: dependency, and the ratio is stable enough for budgeting decisions while being
#: wrong enough that it must never be presented as a bill.
CHARS_PER_TOKEN = 4.0

#: Where preserved full outputs go, relative to the data dir. Kept on disk rather
#: than in memory so a later turn — possibly after a compaction — can still reach
#: the original.
RAW_SUBDIR = os.path.join("state", "raw_output")


def estimate_tokens(text: str) -> int:
    """Rough token count from character length. An estimate, never a bill."""
    return int(len(text) / CHARS_PER_TOKEN)


# --------------------------------------------------------------------------
# Per-command condensers
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Condensed:
    """Result of condensing one command's output."""

    command: str
    handler: str
    text: str
    original_chars: int
    condensed_chars: int
    #: True when the original was preserved verbatim (failure, or no handler).
    passthrough: bool
    raw_path: str | None = None
    note: str = ""

    def reduction(self) -> float:
        if self.original_chars == 0:
            return 0.0
        return round(1.0 - (self.condensed_chars / self.original_chars), 4)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self) | {
            "reduction": self.reduction(),
            "estimated_input_tokens_saved":
                estimate_tokens("x" * (self.original_chars
                                       - self.condensed_chars)),
            "accounting_note": ("an ESTIMATE at "
                                f"{CHARS_PER_TOKEN} chars/token; affects INPUT "
                                "tokens on later turns only, and excludes "
                                "reasoning cost"),
        }


def _dedupe_lines(lines: Sequence[str]) -> list[str]:
    """Collapse consecutive duplicate lines into `line  (xN)`.

    Consecutive rather than global: a repeated line far later in the output is
    usually a genuine second occurrence, and merging the two would misrepresent
    where in the run each happened.
    """
    out: list[str] = []
    prev: str | None = None
    count = 0
    for line in list(lines) + [None]:  # sentinel flushes the last group
        if line == prev:
            count += 1
            continue
        if prev is not None:
            out.append(prev if count == 1 else f"{prev}  (x{count})")
        prev, count = line, 1
    return out


def _condense_pytest(text: str) -> tuple[str, str]:
    """Keep failures, errors, and the summary; drop the progress dots."""
    lines = text.splitlines()
    keep: list[str] = []
    in_failure = False
    for line in lines:
        if re.match(r"^(=+\s*(FAILURES|ERRORS)|_{5,})", line):
            in_failure = True
        if re.match(r"^=+\s*(short test summary|\d+ (passed|failed))", line,
                    re.IGNORECASE):
            in_failure = False
            keep.append(line)
            continue
        if in_failure or re.match(r"^(FAILED|ERROR|E\s|>\s|assert)", line):
            keep.append(line)
        elif re.search(r"\b\d+ (passed|failed|error|skipped)", line):
            keep.append(line)
    if not keep:
        # No failures found: the summary line is the entire useful content.
        tail = [l for l in lines if l.strip()][-2:]
        return "\n".join(tail), "pytest: passing run reduced to its summary"
    return "\n".join(_dedupe_lines(keep)), "pytest: failures and summary only"


def _condense_unittest(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    keep = [l for l in lines
            if re.match(r"^(FAIL|ERROR|OK|Ran \d+ test|FAILED|AssertionError)", l)
            or l.startswith("  File ")]
    if not keep:
        return "\n".join([l for l in lines if l.strip()][-2:]), \
               "unittest: summary only"
    return "\n".join(_dedupe_lines(keep)), "unittest: verdicts and frames only"


def _condense_git_status(text: str) -> tuple[str, str]:
    """Group porcelain entries by status code and directory.

    A repo with 400 untracked files produces 400 lines that say the same thing.
    The agent needs the counts and the top directories, not the enumeration.
    """
    lines = [l for l in text.splitlines() if l.strip()]
    groups: dict[str, list[str]] = {}
    for line in lines:
        code = line[:2].strip() or "?"
        path = line[2:].strip()
        groups.setdefault(code, []).append(path)
    out = []
    for code, paths in sorted(groups.items()):
        if len(paths) <= 6:
            out.append(f"{code}: " + ", ".join(paths))
            continue
        dirs: dict[str, int] = {}
        for p in paths:
            top = p.split("/")[0] if "/" in p else "."
            dirs[top] = dirs.get(top, 0) + 1
        summary = ", ".join(f"{d}/ ({n})" for d, n in
                            sorted(dirs.items(), key=lambda kv: -kv[1])[:8])
        out.append(f"{code}: {len(paths)} paths — {summary}")
    return "\n".join(out), "git status: grouped by status code and directory"


def _condense_git_push(text: str) -> tuple[str, str]:
    """Progress counters carry no information once the push succeeded."""
    lines = [l for l in text.splitlines()
             if l.strip() and not re.search(r"^(Enumerating|Counting|Compressing|"
                                            r"Writing|Total|remote:\s*$)", l)
             and "%" not in l]
    return "\n".join(lines[-3:]) or "ok", "git push: progress stripped"


def _condense_listing(text: str) -> tuple[str, str]:
    """Directory listings: counts by extension plus the first entries."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) <= 15:
        return text.strip(), "listing: short enough to keep verbatim"
    exts: dict[str, int] = {}
    for line in lines:
        name = line.split()[-1] if line.split() else line
        ext = os.path.splitext(name)[1] or "(no ext)"
        exts[ext] = exts.get(ext, 0) + 1
    head = "\n".join(lines[:8])
    tally = ", ".join(f"{e} x{n}" for e, n in
                      sorted(exts.items(), key=lambda kv: -kv[1])[:10])
    return (f"{head}\n… {len(lines)} entries total — {tally}",
            "listing: head plus per-extension counts")


def _condense_diff(text: str) -> tuple[str, str]:
    """Keep hunk headers and changed lines; drop unchanged context."""
    lines = text.splitlines()
    keep = [l for l in lines
            if l.startswith(("diff --git", "@@", "+++", "---", "+", "-"))
            and not l.startswith(("+++ ", "--- "))]
    files = sum(1 for l in lines if l.startswith("diff --git"))
    added = sum(1 for l in keep if l.startswith("+"))
    removed = sum(1 for l in keep if l.startswith("-"))
    header = f"{files} file(s), +{added}/-{removed}"
    return f"{header}\n" + "\n".join(keep), "diff: context lines dropped"


#: Command prefix → handler. Matched on the command STRING because that is what a
#: pre-tool hook sees; the longest matching prefix wins so `git status` beats
#: `git`.
HANDLERS: dict[str, callable] = {
    "pytest": _condense_pytest,
    "python -m pytest": _condense_pytest,
    "python -m unittest": _condense_unittest,
    "unittest": _condense_unittest,
    "git status": _condense_git_status,
    "git push": _condense_git_push,
    "git diff": _condense_diff,
    "git show": _condense_diff,
    "ls": _condense_listing,
    "dir": _condense_listing,
    "find": _condense_listing,
}


def pick_handler(command: str) -> tuple[str, callable] | tuple[None, None]:
    """Longest matching command prefix, or (None, None)."""
    normalized = " ".join(command.strip().split())
    best_key, best_fn = None, None
    for key, fn in HANDLERS.items():
        if normalized.startswith(key) and (best_key is None
                                           or len(key) > len(best_key)):
            best_key, best_fn = key, fn
    return best_key, best_fn


def condense(command: str, output: str, *, exit_code: int = 0,
             data_dir: str | None = None,
             min_chars: int = 400) -> Condensed:
    """Condense `output` for `command`, preserving it verbatim when appropriate.

    Passthrough happens in three cases, each for a distinct reason:

    - **Non-zero exit.** The detail that explains a failure is the first thing a
      summarizer discards. A failing command's output is the highest-value text
      in the session and is never condensed.
    - **No handler.** Generic compression of unknown output is guesswork; leaving
      it alone is the honest default.
    - **Already short.** Below `min_chars` the handler's own header can cost more
      than it saves.
    """
    original = output or ""
    if exit_code != 0:
        raw = _preserve(command, original, data_dir)
        return Condensed(command=command, handler="passthrough:failure",
                         text=original, original_chars=len(original),
                         condensed_chars=len(original), passthrough=True,
                         raw_path=raw,
                         note="non-zero exit: full output kept, because the "
                              "detail explaining a failure is what condensing "
                              "removes")
    if len(original) < min_chars:
        return Condensed(command=command, handler="passthrough:short",
                         text=original, original_chars=len(original),
                         condensed_chars=len(original), passthrough=True,
                         note=f"under {min_chars} chars: condensing would not "
                              "pay for its own header")
    key, fn = pick_handler(command)
    if fn is None:
        return Condensed(command=command, handler="passthrough:no_handler",
                         text=original, original_chars=len(original),
                         condensed_chars=len(original), passthrough=True,
                         note="no per-command handler; generic compression of "
                              "unknown output is guesswork")
    text, note = fn(original)
    raw = _preserve(command, original, data_dir)
    return Condensed(command=command, handler=key, text=text,
                     original_chars=len(original), condensed_chars=len(text),
                     passthrough=False, raw_path=raw, note=note)


def _preserve(command: str, output: str, data_dir: str | None) -> str | None:
    """Write the full output to disk so it can be re-read later."""
    if not data_dir or not output:
        return None
    target = os.path.join(data_dir, RAW_SUBDIR)
    os.makedirs(target, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", command.lower())[:40].strip("-") or "cmd"
    path = os.path.join(target, f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.txt")
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(output)
    return path


# --------------------------------------------------------------------------
# Priority-tiered snapshot
# --------------------------------------------------------------------------

#: Event kinds by priority tier. When the byte budget tightens, tier 4 drops
#: first and tier 1 never drops. The ordering is the claim: knowing which files
#: were modified and which decisions were made is what lets a session continue,
#: while a tool-call tally is trivia.
SNAPSHOT_TIERS: dict[int, tuple[str, ...]] = {
    1: ("files_modified", "active_tasks", "rules", "last_request", "decisions"),
    2: ("errors_unresolved", "constraints", "blockers", "rejected_approaches"),
    3: ("git_operations", "environment", "session_intent"),
    4: ("tool_counts", "external_refs", "data_references"),
}

#: Default byte ceiling for a snapshot. Small on purpose: a snapshot that costs a
#: meaningful share of the context it is meant to preserve defeats itself.
SNAPSHOT_BUDGET_BYTES = 2048


def build_snapshot(events: dict, *, budget_bytes: int = SNAPSHOT_BUDGET_BYTES
                   ) -> dict:
    """Build a bounded, priority-ordered continuation snapshot.

    `events` maps a kind from `SNAPSHOT_TIERS` to a list of strings. Returns both
    the rendered text and an explicit record of what was DROPPED — a snapshot
    that silently omits half the blockers reads as "there were no blockers",
    which is the worst possible failure for a handoff.
    """
    sections: list[str] = []
    included: dict[str, int] = {}
    dropped: dict[str, int] = {}

    #: Cost of the "\n\n" that `join` inserts between sections. Counted
    #: explicitly: a budget that ignores its own separators is not a budget, and
    #: the overshoot grows with the number of sections.
    separator = 2

    def rendered_size(candidate: list[str]) -> int:
        if not candidate:
            return 0
        return (sum(len(s.encode("utf-8")) for s in candidate)
                + separator * (len(candidate) - 1))

    for tier in sorted(SNAPSHOT_TIERS):
        for kind in SNAPSHOT_TIERS[tier]:
            items = [str(x).strip() for x in (events.get(kind) or [])
                     if str(x).strip()]
            if not items:
                continue
            header = f"## {kind} (P{tier})"
            kept: list[str] = []
            for item in items:
                trial = kept + [f"- {item}"]
                block = header + "\n" + "\n".join(trial)
                # Measure the WHOLE rendered snapshot each time. Incremental
                # accounting drifts from the real output; measuring the artifact
                # cannot.
                if rendered_size(sections + [block]) > budget_bytes:
                    break
                kept = trial
            if kept:
                sections.append(header + "\n" + "\n".join(kept))
                included[kind] = len(kept)
            if len(kept) < len(items):
                dropped[kind] = len(items) - len(kept)

    text = "\n\n".join(sections)
    return {
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "budget_bytes": budget_bytes,
        "included": included,
        "dropped": dropped,
        "complete": not dropped,
        "note": ("snapshot is COMPLETE" if not dropped else
                 f"budget exceeded: dropped {sum(dropped.values())} item(s) "
                 f"across {list(dropped)} — the snapshot is a summary, not the "
                 "whole session state, and lower-priority tiers were sacrificed "
                 "first"),
        "tier_policy": {f"P{t}": list(k) for t, k in SNAPSHOT_TIERS.items()},
    }


# --------------------------------------------------------------------------
# Blast radius (query-first context selection)
# --------------------------------------------------------------------------

def blast_radius(edges: Sequence[tuple[str, str]], changed: Sequence[str], *,
                 max_hops: int = 2, max_nodes: int = 40) -> dict:
    """Nodes reachable from `changed` within `max_hops`, following edges BACKWARD.

    Backward, because the question a review asks is "who depends on what I
    changed", not "what does my change depend on". Following edges forward finds
    a change's dependencies, which are usually unaffected by it.

    Both bounds are reported rather than silently applied. A truncated radius
    presented as a complete one is how a review concludes that a change is
    contained when it is not — so `truncated` and `frontier` exist to make the
    unexplored remainder visible.

    Takes plain edge tuples rather than a graph object so it works over the
    knowledge graph, an import graph, or a hand-written dependency list.
    """
    reverse: dict[str, set[str]] = {}
    for src, dst in edges:
        reverse.setdefault(dst, set()).add(src)

    seen = {c for c in changed}
    layers: list[dict] = []
    frontier = set(seen)
    truncated = False
    for hop in range(1, max_hops + 1):
        nxt: set[str] = set()
        for node in frontier:
            nxt |= reverse.get(node, set())
        nxt -= seen
        if not nxt:
            frontier = set()
            break
        room = max_nodes - len(seen)
        if room <= 0:
            truncated = True
            break
        if len(nxt) > room:
            truncated = True
            nxt = set(sorted(nxt)[:room])
        layers.append({"hop": hop, "nodes": sorted(nxt), "count": len(nxt)})
        seen |= nxt
        frontier = nxt

    return {
        "changed": sorted(set(changed)),
        "layers": layers,
        "impacted": sorted(seen - set(changed)),
        "total_in_radius": len(seen),
        "max_hops": max_hops,
        "max_nodes": max_nodes,
        "truncated": truncated,
        "frontier": sorted(frontier),
        "note": (f"radius truncated at {max_nodes} nodes / {max_hops} hops — "
                 f"{len(frontier)} node(s) on the frontier were NOT expanded, so "
                 "the impact set is a lower bound"
                 if truncated else
                 f"radius fully explored: {len(seen) - len(set(changed))} "
                 "impacted node(s)"),
        "usage": ("read the impacted nodes instead of the whole tree — avoiding "
                  "a read saves more than condensing one"),
    }


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------

def estimate_savings(condensations: Sequence[Condensed], *,
                     style_output_reduction: float = 0.0,
                     style_instruction_chars: int = 0,
                     turns: int = 1) -> dict:
    """Aggregate savings, separating input from output and naming the caveats.

    `style_output_reduction` is the fraction by which a brevity instruction
    shrinks the model's own replies; `style_instruction_chars` is what that
    instruction costs as resident input EVERY turn. Netting the two is the whole
    point: a style constraint that saves 65% of output tokens while adding ~1–1.5k
    input tokens per turn can be a net loss on a long session with short replies,
    and reporting only the 65% would hide that.
    """
    saved_chars = sum(c.original_chars - c.condensed_chars
                      for c in condensations)
    input_saved = estimate_tokens("x" * max(0, saved_chars))
    style_cost = estimate_tokens("x" * style_instruction_chars) * max(1, turns)
    return {
        "commands_condensed": sum(1 for c in condensations if not c.passthrough),
        "commands_passed_through": sum(1 for c in condensations if c.passthrough),
        "input_chars_saved": saved_chars,
        "estimated_input_tokens_saved": input_saved,
        "style_output_reduction_claimed": style_output_reduction,
        "estimated_style_input_cost_tokens": style_cost,
        "net_estimated_input_tokens": input_saved - style_cost,
        "caveats": [
            f"token counts are ESTIMATES at {CHARS_PER_TOKEN} chars/token; no "
            "tokenizer is used, so treat them as budgeting aids, not bills",
            "condensing tool output reduces INPUT tokens on later turns only",
            "a brevity instruction reduces OUTPUT tokens and costs resident "
            "INPUT tokens every turn; the net can be negative on long sessions "
            "with short replies",
            "neither mechanism reduces the model's internal reasoning cost",
            "failing commands are never condensed, so a session with many "
            "failures will show low savings — correctly",
        ],
    }
