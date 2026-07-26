"""Parallel provider execution — the multi-agent primitive.

Threads, not processes
---------------------
Each unit of work here is `subprocess.run` waiting on a child agent CLI for
seconds to minutes. That is pure I/O wait, so the GIL is released for
essentially the whole call and threads give full concurrency at a fraction of
the setup cost of processes. A process pool would also have to pickle the
`ProviderSpec` argv closures, which are functions and therefore unpicklable.

Bounded concurrency is mandatory
--------------------------------
Every concurrent agent is a real model session with its own rate limit and its
own memory footprint. Unbounded fan-out gets throttled by the provider (turning
parallelism into serialized retries) and can exhaust local RAM when several
tools index a repo at once. `DEFAULT_MAX_CONCURRENCY` is deliberately small and
derived from CPU count only as a proxy for "how big is this machine".

Failure is per-agent, never per-round
-------------------------------------
One provider timing out, lacking auth, or exiting non-zero must not lose the
other five answers. Every call is wrapped so the round always returns one
`ProviderResult` per requested agent, ordered to match the input. The caller
filters on `.ok` and can report exactly which agents contributed.

Worktree isolation
------------------
Two providers that write files cannot share a working tree: their edits
interleave and the result is a tree neither agent intended. When more than one
mutating provider runs concurrently, each gets its own `git worktree`, created
under a temp root and removed afterwards. Isolation costs real time and disk, so
it is applied only when the specs say it is needed — a panel of read-only
scouts shares one tree.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence

from ..core.platform import child_env
from .base import ProviderResult, ProviderSpec
from .catalog import registry
from .run import run_provider

#: Upper bound on simultaneous agent calls. Small on purpose: provider rate
#: limits, not local cores, are the binding constraint, and a throttled call is
#: slower than a queued one.
DEFAULT_MAX_CONCURRENCY = min(6, (os.cpu_count() or 2))


@dataclasses.dataclass
class AgentTask:
    """One agent's assignment in a fan-out round."""

    provider_id: str
    prompt: str
    label: str = ""
    model: str | None = None
    extra: tuple[str, ...] = ()
    timeout_s: int | None = None
    #: Set by the orchestrator when this task runs in an isolated worktree.
    cwd: str | None = None

    def display(self) -> str:
        return self.label or self.provider_id


@dataclasses.dataclass
class FanoutRound:
    """Everything a ledger needs about one parallel round."""

    results: list[ProviderResult]
    tasks: list[AgentTask]
    wall_s: float
    #: Sum of per-agent durations. The ratio to `wall_s` is the realized
    #: speedup, which is worth recording because a rate-limited provider can
    #: make a "parallel" round effectively serial without any error surfacing.
    serial_s: float
    isolated: bool
    concurrency: int

    @property
    def ok_results(self) -> list[ProviderResult]:
        return [r for r in self.results if r.ok]

    @property
    def texts(self) -> list[str]:
        return [r.text for r in self.ok_results]

    @property
    def labels(self) -> list[str]:
        by_provider = {t.provider_id: t.display() for t in self.tasks}
        return [by_provider.get(r.provider, r.provider) for r in self.ok_results]

    def speedup(self) -> float:
        return round(self.serial_s / self.wall_s, 2) if self.wall_s > 0 else 0.0

    def summary(self) -> dict:
        return {
            "requested": len(self.tasks),
            "succeeded": len(self.ok_results),
            "failed": [{"provider": r.provider, "error": r.error}
                       for r in self.results if not r.ok],
            "wall_s": round(self.wall_s, 2),
            "serial_s": round(self.serial_s, 2),
            "speedup": self.speedup(),
            "concurrency": self.concurrency,
            "worktree_isolated": self.isolated,
        }


# --------------------------------------------------------------------------
# git worktree isolation
# --------------------------------------------------------------------------

def _git_toplevel(path: str) -> str | None:
    """The root of the repository containing `path`, or None.

    Returns the TOPLEVEL rather than a boolean, because `git rev-parse` walks
    *upward*: a directory that is not a repository still answers "yes" whenever
    any ancestor is one. On a machine where the user's home directory is
    git-tracked — which is common, and was true on the authoring machine — every
    path under it reports as a repository, and the repository reported is the
    home directory.

    That turns worktree isolation into a hazard: a fan-out running in a plain
    project folder would create detached worktrees off the user's HOME repo.
    Returning the path makes which repository is in play visible instead of
    implied, and callers record it.
    """
    proc = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=child_env())
    if proc.returncode != 0:
        return None
    top = (proc.stdout or "").strip()
    return os.path.normpath(top) if top else None


def _has_commits(repo: str) -> bool:
    """Whether HEAD points at a commit.

    A repository with no commits has an unborn HEAD, and `git worktree add`
    cannot check anything out of it. Detecting this up front replaces git's
    multi-line `--orphan` hint with one sentence the caller can act on.
    """
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "HEAD"],
                          capture_output=True, text=True, env=child_env())
    return proc.returncode == 0


class WorktreeSet:
    """Temporary `git worktree`s, one per mutating agent, cleaned up on exit.

    Uses `--detach` so no branch is created or moved: the agents produce diffs
    the orchestrator inspects, and leaving branch refs behind in the user's repo
    for every fan-out would be a side effect nobody asked for.

    If the target is not a git repo, or git is unavailable, isolation is
    IMPOSSIBLE rather than approximated — `available` stays False and the caller
    must decide between running serially and running unisolated. Silently
    copying the tree instead would look like isolation while breaking every
    relative path and git operation inside it.
    """

    def __init__(self, repo: str, count: int):
        self.repo = os.path.abspath(repo)
        self.count = count
        self.paths: list[str] = []
        self.root: str | None = None
        self.available = False
        self.reason = ""
        #: The repository git actually resolved. May be an ANCESTOR of `repo`.
        self.toplevel: str | None = None

    def __enter__(self) -> "WorktreeSet":
        if self.count <= 0:
            self.reason = "no isolation requested"
            return self
        if shutil.which("git") is None:
            self.reason = "git not on PATH"
            return self

        toplevel = _git_toplevel(self.repo)
        if toplevel is None:
            self.reason = f"{self.repo} is not inside a git repository"
            return self
        # Name the repository that will actually be used. `rev-parse` walks up,
        # so this can be an ancestor the caller did not have in mind — a home
        # directory, or a monorepo root. Recording it makes that visible in the
        # round's audit rather than discovered afterwards.
        self.toplevel = toplevel
        if not _has_commits(toplevel):
            self.reason = (f"{toplevel} has no commits yet; git cannot check "
                           "anything out into a worktree. Make one commit, or "
                           "run this round without isolation")
            return self

        self.root = tempfile.mkdtemp(prefix="dobby-wt-")
        for i in range(self.count):
            path = os.path.join(self.root, f"agent{i}")
            proc = subprocess.run(
                ["git", "-C", toplevel, "worktree", "add", "--detach", path],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=child_env())
            if proc.returncode != 0:
                # Partial success is worse than none: agents 0..i-1 would be
                # isolated while i..n share the main tree, which is exactly the
                # corruption case. Unwind and report.
                first = (proc.stderr or "").strip().splitlines()
                headline = next((l for l in first
                                 if l and not l.startswith("hint:")), "")
                self.reason = (f"git worktree add failed in {toplevel}: "
                               f"{headline[:200] or 'see git output'}")
                self._cleanup()
                return self
            self.paths.append(path)
        self.available = True
        self.reason = (f"{self.count} detached worktrees under {self.root}, "
                       f"from repository {toplevel}")
        return self

    def _cleanup(self) -> None:
        for path in self.paths:
            subprocess.run(
                ["git", "-C", self.toplevel or self.repo,
                 "worktree", "remove", "--force", path],
                capture_output=True, text=True, env=child_env())
        self.paths = []
        if self.root and os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        self.root = None
        self.available = False

    def diffs(self) -> list[dict]:
        """Per-worktree `git diff` so the orchestrator can compare agent edits.

        Captured BEFORE cleanup — once a worktree is removed its changes are
        gone, so a caller that wants the edits must read them inside the
        `with` block.
        """
        out = []
        for i, path in enumerate(self.paths):
            proc = subprocess.run(["git", "-C", path, "diff"],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  env=child_env())
            out.append({"agent": i, "path": path,
                        "diff": proc.stdout or "",
                        "diff_bytes": len(proc.stdout or "")})
        return out

    def __exit__(self, *exc) -> None:
        self._cleanup()


# --------------------------------------------------------------------------
# the fan-out
# --------------------------------------------------------------------------

def _needs_isolation(tasks: Sequence[AgentTask]) -> int:
    """How many worktrees this round needs: 0 unless ≥2 agents mutate files."""
    reg = registry()
    mutating = [t for t in tasks
                if reg.get(t.provider_id).mutates_worktree]
    return len(mutating) if len(mutating) >= 2 else 0


def run_round(tasks: Sequence[AgentTask], *,
              cwd: str | None = None,
              max_concurrency: int | None = None,
              isolate: bool | None = None,
              output_cap: int | None = None,
              on_complete=None) -> FanoutRound:
    """Run every task concurrently; return one result per task, input-ordered.

    `isolate=None` decides from the specs (isolate iff ≥2 mutating providers).
    Pass False to force a shared tree — correct for read-only panels and for
    callers that have already arranged isolation themselves.

    `on_complete(result, done, total)` fires as each agent finishes, in
    completion order rather than input order. It exists so a caller can show
    progress during a round that takes minutes; without it the only signal is
    silence followed by everything at once. A callback that raises is swallowed —
    a broken progress display must not lose a completed round's results.
    """
    tasks = list(tasks)
    if not tasks:
        return FanoutRound([], [], 0.0, 0.0, False, 0)

    base = os.path.abspath(cwd or os.getcwd())
    want = _needs_isolation(tasks) if isolate is None else (
        len(tasks) if isolate else 0)
    limit = max_concurrency or DEFAULT_MAX_CONCURRENCY
    reg = registry()

    started = time.monotonic()
    with WorktreeSet(base, want) as trees:
        if trees.available:
            # Assign worktrees only to the agents that need them; read-only
            # agents keep the real tree so they see the actual project state.
            free = list(trees.paths)
            for task in tasks:
                if reg.get(task.provider_id).mutates_worktree and free:
                    task.cwd = free.pop(0)
                else:
                    task.cwd = base
        else:
            for task in tasks:
                task.cwd = base

        def call(task: AgentTask) -> ProviderResult:
            spec: ProviderSpec = reg.get(task.provider_id)
            kwargs = {
                "model": task.model,
                "extra": task.extra,
                "cwd": task.cwd,
                "timeout_s": task.timeout_s,
            }
            if output_cap is not None:
                kwargs["output_cap"] = output_cap
            result = run_provider(spec, task.prompt, **kwargs)
            result.meta["label"] = task.display()
            result.meta["isolated_cwd"] = (task.cwd != base)
            return result

        results: list[ProviderResult] = [None] * len(tasks)  # type: ignore[list-item]
        finished = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as pool:
            futures = {pool.submit(call, t): i for i, t in enumerate(tasks)}
            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    # A bug in argv construction must not lose the round.
                    results[idx] = ProviderResult(
                        provider=tasks[idx].provider_id, ok=False,
                        error=f"orchestrator error: {type(exc).__name__}: {exc}")
                finished += 1
                if on_complete is not None:
                    try:
                        on_complete(results[idx], finished, len(tasks))
                    except Exception:  # noqa: BLE001
                        # A broken progress display must never cost a round that
                        # already succeeded. Swallowed deliberately.
                        pass

        isolated = trees.available
        # Read diffs before the worktrees are torn down by __exit__.
        collected_diffs = trees.diffs() if trees.available else []

    wall = time.monotonic() - started
    serial = sum(r.duration_s for r in results if r is not None)
    round_ = FanoutRound(results=list(results), tasks=tasks, wall_s=wall,
                         serial_s=serial, isolated=isolated,
                         concurrency=min(limit, len(tasks)))
    if collected_diffs:
        for entry in collected_diffs:
            idx = entry["agent"]
            if idx < len(round_.results) and round_.results[idx] is not None:
                round_.results[idx].meta["worktree_diff_bytes"] = \
                    entry["diff_bytes"]
    return round_


def broadcast(prompt: str, provider_ids: Sequence[str], **kwargs) -> FanoutRound:
    """Same prompt to every provider.

    Useful for measuring raw model disagreement, and the WRONG default for
    ideation: identical prompts are the correlated case that
    `swarm/protocols.py` exists to avoid. Kept because it is the honest baseline
    a decorrelated panel must beat.
    """
    tasks = [AgentTask(provider_id=p, prompt=prompt, label=p)
             for p in provider_ids]
    return run_round(tasks, **kwargs)
