"""First contact with a repository: what is it, how is it checked, is it sound.

The initialiser is not a worker. It does not finish anything. Its whole job is
to write the contract every later worker obeys — which is why it runs once, why
its output is structured, and why it refuses to guess.

Three refusals, all deliberate:

- **It does not invent a smoke check.** If the stack is unrecognised and the
  caller named none, `smoke_checks` is empty and the baseline records that it
  could not be established. An invented command that happens to exit zero is
  worse than no baseline: it certifies soundness nobody measured.
- **It does not call a provider.** Detection is a PATH lookup and a file scan.
  Nothing here spends money or leaves the machine, so `init` is safe to run on
  a repository you are still deciding about.
- **It does not write a portfolio it made up.** Items come from the caller.
  Deriving a feature list from a repository needs a model, and a model's guess
  at "what remains" — stored as the definition of done — is exactly the kind of
  fiction the rest of this kernel exists to keep out.
"""

from __future__ import annotations

import os
import subprocess
import time

from ..core.platform import child_env, resolve_command
from .models import Baseline, ProjectManifest, WorkItem, digest_of
from .store import ProjectStore, new_project_id, new_work_item_id

#: Marker file -> what it says the stack is, and the check that usually proves
#: the tree is importable. Ordered: the first match wins for the smoke default,
#: and every match is recorded in `stack`.
STACK_MARKERS = (
    ("pyproject.toml", "python", "{python} -c \"import sys; sys.exit(0)\""),
    ("setup.py", "python", "{python} -c \"import sys; sys.exit(0)\""),
    ("requirements.txt", "python", "{python} -c \"import sys; sys.exit(0)\""),
    ("package.json", "node", "node --version"),
    ("go.mod", "go", "go build ./..."),
    ("Cargo.toml", "rust", "cargo check"),
    ("pom.xml", "java-maven", "mvn -q -o -v"),
    ("build.gradle", "java-gradle", "gradle --version"),
    ("Gemfile", "ruby", "ruby --version"),
)

#: Test-runner defaults, only offered when the directory they need exists.
TEST_DEFAULTS = (
    ("tests", "python", "{python} -m unittest discover -s tests -q"),
    ("test", "python", "{python} -m unittest discover -s test -q"),
)

#: Ceiling on the file walk used for a repo digest outside git. A digest that
#: takes a minute is one nobody recomputes, and a stale digest defeats the
#: check it exists for.
MAX_WALKED_FILES = 4000

#: Directories never walked. They are large, generated, and change without the
#: project changing — which would make the digest report drift that is not real.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist",
             "build", ".mypy_cache", ".pytest_cache", ".tox", ".idea",
             ".dobby"}


def _git(root: str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=child_env(), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def own_git_root(root: str) -> str | None:
    """The repository whose TOPLEVEL is exactly `root`, or None.

    `git rev-parse` walks UPWARD, so any directory inside a repository answers
    "yes" and names an ancestor. On a machine where the home directory is
    git-tracked — which is common, and is true on the machine this was written
    on — every temporary directory reports as a repository and the repository
    reported is the home directory. A project keyed on that would take its
    baseline sha from an unrelated tree and never notice its own edits.

    `dobby/providers/fanout.py` documents the same trap for worktrees. The rule
    here is stricter than it is there because the consequence is worse: a
    worktree created in the wrong repo is visible, and a baseline taken against
    the wrong repo silently certifies code nobody checked.

    A project in a SUBDIRECTORY of a real repository therefore falls back to the
    file walk. That is slower and it is correct: the digest then describes the
    files this project actually owns.
    """
    code, top = _git(root, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        return None
    top = os.path.normpath(top)
    return top if top == os.path.normpath(os.path.abspath(root)) else None


def git_sha(root: str) -> str:
    """HEAD of THIS project's repository. Never an empty string.

    An empty sha compares equal to another empty sha, which would make the
    staleness check in `session.open` silently pass for two unrelated trees.
    """
    if own_git_root(root) is None:
        return "(not a git repository)"
    code, out = _git(root, "rev-parse", "HEAD")
    return out if code == 0 and out else "(no commit yet)"


def repo_digest(root: str) -> str:
    """A digest that changes when the working tree changes.

    Inside git this is HEAD plus the porcelain status, so uncommitted edits move
    it — which is the point: a session resumed against a dirty tree is resuming
    against a project nobody has a baseline for.

    Outside git it walks, bounded, and records the bound in the digest input so
    a truncated walk cannot be mistaken for a complete one.
    """
    if own_git_root(root) is not None:
        code, head = _git(root, "rev-parse", "HEAD")
        if code == 0:
            _, status = _git(root, "status", "--porcelain")
            return digest_of({"head": head, "status": status})

    entries = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            try:
                entries.append((os.path.relpath(path, root).replace("\\", "/"),
                                os.path.getsize(path)))
            except OSError:
                continue
            if len(entries) >= MAX_WALKED_FILES:
                truncated = True
                break
        if truncated:
            break
    return digest_of({"files": sorted(entries), "truncated": truncated})


def detect_stack(root: str) -> tuple:
    return tuple(sorted({name for marker, name, _ in STACK_MARKERS
                         if os.path.exists(os.path.join(root, marker))}))


def discover_smoke_checks(root: str) -> tuple:
    """Cheap commands that fail loudly when the tree is broken.

    A test suite is NOT the default. A baseline is taken on every session open,
    and a check that costs ten minutes is a check somebody turns off. The
    caller passes `--smoke` when the real suite is the right bar.
    """
    checks: list[str] = []
    for marker, _, command in STACK_MARKERS:
        if os.path.exists(os.path.join(root, marker)):
            checks.append(command)
            break
    for directory, _, command in TEST_DEFAULTS:
        if os.path.isdir(os.path.join(root, directory)):
            checks.append(command)
            break
    return tuple(checks)


def capability_inventory(*, allow_network: bool = False) -> dict:
    """Which agent runners exist here, recorded and not trusted later.

    Recorded at init because "this project was set up on a machine with codex
    and gemini" explains a scorecard that a later machine cannot reproduce.
    Detection is a PATH lookup; nothing is invoked.
    """
    from ..providers.detect import survey
    return {pid: {"state": a.state, "usable": a.usable,
                  "cost_tier": getattr(a, "cost_tier", None)}
            for pid, a in survey(allow_network).items()}


def run_smoke(root: str, checks: tuple, *, timeout_s: int = 900) -> list[dict]:
    results = []
    for check in checks:
        command = resolve_command(check)
        started = time.monotonic()
        try:
            proc = subprocess.run(command, shell=True, cwd=root,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  env=child_env(), timeout=timeout_s)
            results.append({
                "check": check, "exit_code": proc.returncode,
                "passed": proc.returncode == 0,
                "duration_s": round(time.monotonic() - started, 2),
                "output_tail": ((proc.stdout or "") + (proc.stderr or ""))
                .strip()[-500:]})
        except subprocess.TimeoutExpired:
            results.append({"check": check, "exit_code": None, "passed": False,
                            "duration_s": timeout_s,
                            "output_tail": f"no verdict within {timeout_s}s"})
        except OSError as exc:
            results.append({"check": check, "exit_code": None, "passed": False,
                            "duration_s": 0.0,
                            "output_tail": f"could not run this here: {exc}"})
    return results


def take_baseline(root: str, manifest: ProjectManifest, *,
                  timeout_s: int = 900) -> Baseline:
    """Run the smoke checks and record whether the tree is sound.

    No checks means `passed=False` with the reason. Reporting an unchecked tree
    as sound is the one outcome that makes every later decision unsafe.
    """
    if not manifest.smoke_checks:
        return Baseline(
            project_id=manifest.project_id, git_sha=git_sha(root),
            manifest_digest=manifest.manifest_digest,
            repo_digest=repo_digest(root), smoke_results=(),
            passed=False,
            note=("no smoke check is defined for this project, so its "
                  "soundness is unestablished. Pass --smoke with the command "
                  "that proves the tree works"))
    results = run_smoke(root, manifest.smoke_checks, timeout_s=timeout_s)
    passed = bool(results) and all(r["passed"] for r in results)
    # Measured AFTER the checks ran, so it describes the tree they were run
    # against rather than the one that existed when the command was assembled.
    #
    # Omitting it here — which this function did, on this path only — made
    # `Baseline.repo_digest` dead: the no-checks branch below set it but always
    # returns `passed=False`, so the only baseline that could ever gate a
    # session carried an empty digest, and `Baseline.matches` skips the
    # comparison when either side is empty. The dirty-tree refusal that field
    # exists for had therefore never fired.
    return Baseline(
        project_id=manifest.project_id, git_sha=git_sha(root),
        manifest_digest=manifest.manifest_digest,
        repo_digest=repo_digest(root),
        smoke_results=tuple(results), passed=passed,
        note="" if passed else "at least one smoke check did not pass")


def build_manifest(root: str, *, smoke: tuple = (),
                   project_id: str | None = None,
                   allow_network: bool = False) -> ProjectManifest:
    root = os.path.abspath(root)
    return ProjectManifest(
        project_id=project_id or new_project_id(root),
        root=root,
        repo_digest=repo_digest(root),
        stack=detect_stack(root),
        smoke_checks=tuple(smoke) or discover_smoke_checks(root),
        capability_inventory=capability_inventory(allow_network=allow_network),
        policy_version="1")


def items_from_specs(project_id: str, specs: list[dict]) -> list[WorkItem]:
    """Turn caller-supplied item specs into `WorkItem`s, ids assigned in order."""
    items = []
    for index, spec in enumerate(specs, start=1):
        data = dict(spec)
        data.setdefault("work_item_id", new_work_item_id(index))
        data["project_id"] = project_id
        data.setdefault("title", data.get("outcome", "")[:60] or "untitled")
        items.append(WorkItem.from_dict(data))
    return items


def initialise(data_dir: str, root: str, *, smoke: tuple = (),
               item_specs: list[dict] | None = None,
               allow_network: bool = False,
               run_baseline: bool = True) -> dict:
    """Create the project, its portfolio, and its first baseline."""
    manifest = build_manifest(root, smoke=smoke, allow_network=allow_network)
    items = items_from_specs(manifest.project_id, item_specs or [])
    store = ProjectStore(data_dir)
    store.create_project(manifest, items)

    baseline = None
    if run_baseline:
        baseline = take_baseline(manifest.root, manifest)
        store.set_baseline(manifest.project_id, baseline)

    return {
        "project_id": manifest.project_id,
        "root": manifest.root,
        "git_sha": git_sha(manifest.root),
        "stack": list(manifest.stack),
        "smoke_checks": list(manifest.smoke_checks),
        "manifest_digest": manifest.manifest_digest,
        "capabilities": sorted(pid for pid, a in
                               manifest.capability_inventory.items()
                               if a.get("usable")),
        "work_items": [i.work_item_id for i in items],
        "baseline": baseline.to_dict() if baseline else None,
        "store": store.path,
        "next": (f"dobby project next --project {manifest.project_id}"
                 if not items else
                 f"review the portfolio, then "
                 f"dobby project next --project {manifest.project_id}"),
    }
