"""Did the side effect a node declared actually happen?

THE DEFECT THIS ANSWERS

Measured 2026-08-22: a node with `side_effect_class=LOCAL_WRITE` and an
instruction to create a file returned `ok: True`, `failure: None`, and created
nothing. Two faults met there. The provider was never granted the permission —
a missing wire, fixed in `ProviderWorker`. And nothing checked, so a layer
reported a result it had no basis for.

The second is the one worth a module. A worker's job is to say what happened,
and "the call returned" is not the same claim as "the work was done". Where a
node DECLARES an effect, that declaration is checkable, and checking it is
cheaper than every downstream consequence of not checking it.

WHY IT IS JUDGED BY THE DECLARATION AND NOT BY GIT

Not every legitimate write shows up as a repository change: a node may write
outside the tree, into a gitignored directory, or produce a file that already
existed with identical content. So a node states what it expects — the paths it
will touch — and that is what is checked. A node that declares nothing specific
falls back to "did anything under the root change at all", which is weaker and
still catches the measured defect.

WHY FAIL-CLOSED IS THE DEFAULT

A LOCAL_WRITE node that legitimately writes nothing has a way to say so: declare
READ_ONLY, which is what the class exists for. Choosing the writing class and
then writing nothing is either a provider that could not, or a task that was
already done — and the second is worth a failed node and an operator's attention,
because an item whose work was already complete is a portfolio that is out of
date with its tree.

WHAT THIS IS NOT

It is not the acceptance gate. It runs BEFORE any check, and it answers a
smaller question: did the thing this node said it would do leave a trace. An
acceptance check asks whether the result is right. Both are needed and neither
substitutes for the other.
"""

from __future__ import annotations

import os

#: Directories never walked when fingerprinting a tree. Large, generated, and
#: changing without the project changing — a fingerprint that moved because
#: `__pycache__` did would report an effect nobody caused.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".omc"}

#: Ceiling on the fallback walk. A node's effect check must be cheap enough to
#: run on every attempt; a check nobody can afford is a check that gets disabled.
MAX_WALKED = 3000


def _stat(path: str):
    try:
        info = os.stat(path)
        return (True, info.st_size, info.st_mtime_ns)
    except OSError:
        return (False, None, None)


def snapshot(root: str, expected_paths=()) -> dict:
    """The state of what a node said it would touch, or of the tree if it did not.

    Returns a dict either way so `observed` has one shape to compare, and so a
    caller cannot accidentally compare a path map against a tree map.
    """
    if expected_paths:
        return {"kind": "paths",
                "entries": {p: _stat(os.path.join(root, p))
                            for p in expected_paths}}

    entries: dict = {}
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            entries[rel] = _stat(full)
            if len(entries) >= MAX_WALKED:
                truncated = True
                break
        if truncated:
            break
    # The truncation flag is part of the fingerprint's identity, so a walk that
    # stopped early cannot be compared against a complete one as though both
    # described the whole tree.
    return {"kind": "tree", "entries": entries, "truncated": truncated}


def observed(before: dict, after: dict) -> tuple[bool, str]:
    """`(happened, detail)` — whether anything the node was watching changed.

    The detail NAMES the paths rather than counting them. "2 files changed" is
    the finding shape this repository rejects; an operator deciding whether the
    worker did the right thing needs to see which two.
    """
    if before.get("kind") != after.get("kind"):     # pragma: no cover - guard
        return False, "the two snapshots describe different things"

    old, new = before.get("entries", {}), after.get("entries", {})
    created = sorted(p for p in new
                     if new[p][0] and not old.get(p, (False,))[0])
    removed = sorted(p for p in old if old[p][0] and not new.get(p, (False,))[0])
    modified = sorted(p for p in new
                      if p in old and old[p][0] and new[p][0]
                      and old[p][1:] != new[p][1:])

    parts = []
    if created:
        parts.append(f"created {created[:5]}")
    if modified:
        parts.append(f"modified {modified[:5]}")
    if removed:
        parts.append(f"removed {removed[:5]}")

    if parts:
        return True, "; ".join(parts)

    if before.get("kind") == "paths":
        return False, (f"none of the declared paths {sorted(old)} exist or "
                       f"changed")
    note = " (walk truncated)" if after.get("truncated") else ""
    return False, f"nothing under the root changed{note}"
