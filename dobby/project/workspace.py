"""An isolated tree for one work item, and the gate its changes must pass.

WHAT THIS FIXES

`WorkOrder.write_set` has been recorded and not enforced since the compiler
landed — `LEDGER_workorder_compiler.md` says so in its own not-done list. A
declared write set that nothing checks is a comment. Here it becomes the thing
that decides whether a run's changes are allowed back into the project at all.

The mechanism is three steps and none of them is clever:

    isolate    the item runs in a detached `git worktree`, so whatever it does
               happens somewhere the project is not
    manifest   `git status --porcelain` in that worktree says exactly what
               changed, which is a measurement rather than a worker's account
               of itself
    gate       every changed path must be inside the declared write set and
               outside `protected_paths`, and only then is anything copied back

WHY IT COPIES INSTEAD OF APPLYING A PATCH

A patch that fails to apply leaves a half-merged tree and a conflict nobody
asked a model to resolve. Copying exactly the gated paths is auditable one path
at a time, it handles untracked files without a second mechanism, and the set of
things that can go wrong is "this path could not be written", which is a sentence
an operator can act on.

WHY AN UNDECLARED WRITE SET IS REFUSED

Isolation whose output cannot be gated is a slower way of running unisolated.
Merging it anyway would produce the same tree as before with extra steps and a
report implying a check happened. So `merge` refuses, and says which item owes a
write set.

WHY THE BASELINE RUNS AFTER THE MERGE AND NOT ONLY BEFORE

The worktree's own checks passed against the worktree. That says the change is
self-consistent; it does not say the project still stands with the change in it,
because the worktree was branched from an older tree and the checks it ran are
the item's, not the project's. So the project's smoke checks run against the
merged tree, and a failure REVERTS every path this merge wrote — from a snapshot
taken immediately before, byte for byte, so a failed merge leaves the tree it
found.

WHAT IS NOT HERE

Parallel work items. The kernel serialises by construction — a `SessionEnvelope`
carries one active item and `select_next` yields one — and running two at once is
a change to the project kernel's invariants, which is a decision with an owner
rather than a side effect of adding isolation. This is the isolation, the
manifest, the gate and the recheck; the scheduling is deliberately left.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shutil
import subprocess

from ..core.platform import child_env
from ..core.security import load_protected
from .models import ProjectError

#: Porcelain status codes whose path is GONE from the worktree. `D` in either
#: column, and nothing else — a rename shows as `R` with two paths and is split
#: into a delete plus an add by `changed_paths` rather than special-cased later.
_DELETED = ("D",)


class MergeRefused(ProjectError):
    """The isolated changes may not enter the project, and this says why."""


@dataclasses.dataclass(frozen=True)
class ChangeManifest:
    """What actually changed in a worktree. A measurement, not a worker's report."""

    #: Repo-relative POSIX paths that exist in the worktree and must be copied.
    written: tuple = ()
    #: Repo-relative POSIX paths the worktree no longer has.
    deleted: tuple = ()
    #: Raw porcelain, kept so a refusal can quote what it saw.
    raw: str = ""

    @property
    def paths(self) -> tuple:
        return tuple(sorted(set(self.written) | set(self.deleted)))

    def to_dict(self) -> dict:
        return {"written": list(self.written), "deleted": list(self.deleted),
                "paths": list(self.paths)}


def _git(root: str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=child_env(), timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "")


def changed_paths(worktree: str) -> ChangeManifest:
    """Everything the worktree has that its checkout did not, and vice versa.

    `--porcelain` rather than `git diff`: a diff shows tracked edits and an agent
    that creates a new file creates an untracked one, which a diff would report
    as nothing at all. The gate has to see the files that were ADDED most of all,
    since those are the ones nobody declared.
    """
    code, out = _git(worktree, "status", "--porcelain", "-uall")
    if code != 0:
        raise MergeRefused(
            f"cannot read what changed in {worktree}: git status failed. "
            f"Nothing is merged from a tree whose changes cannot be listed")

    written: list[str] = []
    deleted: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code_pair, rest = line[:2], line[3:]
        if " -> " in rest:                     # a rename: a delete and an add
            old, new = rest.split(" -> ", 1)
            deleted.append(old.strip().strip('"').replace("\\", "/"))
            written.append(new.strip().strip('"').replace("\\", "/"))
            continue
        path = rest.strip().strip('"').replace("\\", "/")
        if any(c in _DELETED for c in code_pair):
            deleted.append(path)
        else:
            written.append(path)
    return ChangeManifest(written=tuple(sorted(set(written))),
                          deleted=tuple(sorted(set(deleted))), raw=out)


def _within(path: str, allowed: str) -> bool:
    """Whether `path` is the allowed path or lives under it, as a directory."""
    if path == allowed:
        return True
    prefix = allowed if allowed.endswith("/") else allowed + "/"
    return path.startswith(prefix)


def gate(manifest: ChangeManifest, *, allowed, protected=None) -> list[str]:
    """Every reason these changes may not enter the project. Empty means clean.

    Returns one violation per PATH, never a count. "3 paths violated the write
    set" is the finding shape this repository rejects; the operator needs the
    three names to decide whether the write set was too narrow or the worker was
    out of scope.
    """
    patterns = [re.compile(p) for p in (protected or [])]
    allow = tuple(allowed or ())
    violations: list[str] = []

    for path in manifest.paths:
        for pattern in patterns:
            if pattern.search(path):
                violations.append(
                    f"{path} matches a protected path ({pattern.pattern!r}); "
                    f"protected paths are refused even when the item declared "
                    f"them")
                break
        else:
            if not any(_within(path, a) for a in allow):
                violations.append(
                    f"{path} is outside the declared write set {list(allow)}; "
                    f"either the order was wrong about what it would touch, or "
                    f"the worker went out of scope, and this cannot tell which")
    return violations


@contextlib.contextmanager
def isolated(root: str, *, label: str = "item"):
    """A detached worktree of `root`, removed on exit. Yields `(path, reason)`.

    `path` is None when isolation was impossible — no git, no commits, not a
    repository. That is returned rather than raised because the caller's answer
    differs by situation: a loop may reasonably run unisolated and say so, and a
    merge gate may not.

    Reuses `providers/fanout.WorktreeSet`, which already carries the trap this
    repository documented twice: `git rev-parse` walks UP, so a project inside
    somebody's git-tracked home directory resolves to the HOME repo, and a
    worktree created there is a worktree of the wrong tree. `WorktreeSet` records
    which repository it actually used; that string is the `reason` yielded here.
    """
    from ..providers.fanout import WorktreeSet

    with WorktreeSet(root, count=1) as trees:
        if not trees.available:
            yield None, trees.reason
        else:
            yield trees.paths[0], trees.reason


def _snapshot(root: str, paths) -> dict:
    """Exactly the bytes at each path right now, or None where nothing is there.

    In memory rather than a temp copy: these are the paths one work item
    declared, a revert must be immediate, and a snapshot that itself needs the
    filesystem to work is a snapshot that fails when the filesystem is why the
    merge failed.
    """
    before: dict = {}
    for path in paths:
        target = os.path.join(root, path)
        try:
            with open(target, "rb") as handle:
                before[path] = handle.read()
        except OSError:
            before[path] = None
    return before


def _restore(root: str, snapshot: dict) -> list[str]:
    failures = []
    for path, blob in snapshot.items():
        target = os.path.join(root, path)
        try:
            if blob is None:
                if os.path.exists(target):
                    os.remove(target)
            else:
                os.makedirs(os.path.dirname(target) or root, exist_ok=True)
                with open(target, "wb") as handle:
                    handle.write(blob)
        except OSError as exc:               # pragma: no cover - filesystem
            failures.append(f"{path}: {exc}")
    return failures


def merge(manifest: ChangeManifest, *, worktree: str, root: str, allowed,
          protected=None, smoke=(), config: dict | None = None,
          run_smoke=None) -> dict:
    """Gate the changes, copy the ones that pass, and undo them if the project breaks.

    Returns a report. Raises `MergeRefused` only for the two conditions where
    there is nothing to report on: no declared write set, and a gate violation.
    Both are decisions, not failures.
    """
    if not allowed:
        raise MergeRefused(
            "this item declared no write set, so nothing can be checked against "
            "one. An isolated run whose output cannot be gated is a slower way "
            "of running unisolated — declare a write set on the implementing "
            "step, or run without isolation and say so")

    protected = (load_protected(config) if protected is None else protected)
    violations = gate(manifest, allowed=allowed, protected=protected)
    if violations:
        raise MergeRefused(
            "the isolated run changed paths it was not allowed to:\n  "
            + "\n  ".join(violations))

    snapshot = _snapshot(root, manifest.paths)
    copied, removed = [], []
    try:
        for path in manifest.written:
            source, target = os.path.join(worktree, path), os.path.join(root,
                                                                        path)
            os.makedirs(os.path.dirname(target) or root, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(path)
        for path in manifest.deleted:
            target = os.path.join(root, path)
            if os.path.exists(target):
                os.remove(target)
                removed.append(path)
    except OSError as exc:
        _restore(root, snapshot)
        raise MergeRefused(
            f"the merge failed partway at {exc}; every path it had written was "
            f"restored, so the tree is the one it found") from exc

    if not smoke:
        return {"merged": True, "copied": copied, "removed": removed,
                "baseline": None,
                "note": ("no smoke checks were declared, so nothing verified "
                         "the project still stands with this change in it")}

    if run_smoke is None:
        from .init import run_smoke as run_smoke

    rows = run_smoke(root, tuple(smoke))
    failed = [r for r in rows if not r.get("passed")]
    if failed:
        restore_failures = _restore(root, snapshot)
        return {"merged": False, "copied": [], "removed": [],
                "baseline": rows, "reverted": sorted(snapshot),
                "restore_failures": restore_failures,
                "note": ("the project's own checks failed with this change "
                         "merged, so every path was restored. The worktree's "
                         "checks passing said the change was self-consistent, "
                         "which is not the same claim")}

    return {"merged": True, "copied": copied, "removed": removed,
            "baseline": rows, "note": ""}


def declared_write_set(store, project_id: str, item) -> tuple:
    """The paths this item's APPLIED plan said it would write, or ().

    Read from the compiled orders rather than from the plan payload, so the same
    validation that refuses a second writer or a path outside the root has
    already run. An item with no plan, a plan with no steps, or a plan that does
    not compile all return `()` — three different situations that produce the
    same answer here, and the caller reports which one it hit.
    """
    from .architecture import PlanSpec
    from .workorder import IMPLEMENT, PlanNotCompilable, compile_orders, plan_for

    raw = plan_for(store, project_id, item)
    if not raw:
        return ()
    plan = PlanSpec(**{k: (tuple(v) if isinstance(v, list) else v)
                       for k, v in raw.items()
                       if k in PlanSpec.__dataclass_fields__})
    manifest = store.load_project(project_id)["manifest"]
    try:
        orders = compile_orders(plan, item=item, manifest=manifest)
    except PlanNotCompilable:
        return ()
    for order in orders:
        if order.role == IMPLEMENT:
            return tuple(order.write_set)
    return ()
