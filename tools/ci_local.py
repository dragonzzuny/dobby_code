"""Run what CI runs, before pushing, in a throwaway clone.

WHY THIS EXISTS

The first fourteen CI runs of this repository were red while the local suite was
green, and nobody noticed because the workflow's result was never read. Three
separate causes came out of that, and all three shared one shape: the working
tree carried state that no checkout has.

  1. Line endings. The skill origin pin hashed raw bytes, and git rewrites
     newlines on checkout, so the tamper check fired on an untouched file.
  2. Gitignored artifacts. A test required the verdict "all checks pass", which
     needs a kg.bootstrap.json that only a machine that has run `init` has.
  3. Installed tooling. This machine has four agent CLIs; a runner has none.

Reading the local suite told us nothing about any of them. A clone does.

WHAT THIS DOES DIFFERENTLY FROM `python -m unittest`

  * Clones HEAD, so gitignored files, uncommitted edits and local line endings
    are all out of the picture. What CI sees is what this runs.
  * Runs `unittest discover` in ONE process, exactly as .github/workflows/ci.yml
    does. Module-by-module runs hide cross-test interference: shared module
    state, a changed cwd, a cached registry. Discover does not.
  * Runs every interpreter it can find, not just the default one. The matrix
    tests 3.10 and 3.12; this machine's default is 3.11, so the versions CI
    actually exercises were the two never being run here.
  * Runs the CLI steps too. A green test suite with a broken `dobby doctor` is
    still a red pipeline.

WHAT IT CANNOT DO

It cannot reproduce a different operating system. Reading the run status remains
mandatory; this only removes the excuse for pushing a break that was reproducible
here all along.

Usage:
    python tools/ci_local.py                # every interpreter found
    python tools/ci_local.py --python 3.10  # just one
    python tools/ci_local.py --keep          # leave the clone for inspection
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "ci.yml")

# Kept in step order and in the workflow's own words so a drift between the two
# is visible in a diff. parse_workflow_steps() below checks they still agree.
CLI_STEPS = [
    ("environment the tests will actually see",
     ["tools/ci_env_report.py"]),
    ("unicode round-trip", ["tools/ci_unicode_probe.py"]),
    ("bootstrap scan", ["-m", "dobby.cli", "init", "--scan", "."]),
    ("doctor", ["-m", "dobby.cli", "doctor"]),
    ("self-check slice", ["-m", "dobby.cli", "slice", "--scenario",
                          "SELF-CHECK"]),
    ("retrieval optimizer smoke", ["-m", "dobby.cli", "optimize", "--seeds",
                                   "0", "--iters", "5", "--pop", "6"]),
    ("DESIGN.md validates", ["-m", "dobby.cli", "design"]),
    ("fleet reports an empty fleet without failing",
     ["-m", "dobby.cli", "fleet"]),
    # From the separate `mcp` job. CI runs it on ubuntu only; the protocol
    # itself is platform-independent, so running it here costs seconds and
    # closes the last gap between this mirror and the workflow.
    ("mcp meta-tools", ["-m", "unittest", "tests.test_mcp_server", "-q"]),
]


def ci_env() -> dict:
    """The environment the workflow actually gives its steps.

    .github/workflows/ci.yml declares no `env:` block, so CI runs with UTF-8
    mode OFF. Every local run in this project has been made from a shell that
    exports PYTHONUTF8=1, which silently turns stdout into UTF-8 on Windows and
    hides exactly the class of failure the workflow is there to catch: on
    Windows, Python 3.10 and 3.12 encode stdout as the ANSI code page unless
    UTF-8 mode is on, and a test that prints non-ASCII then dies with
    UnicodeEncodeError.

    So these variables are REMOVED here rather than set. A reproduction that
    fixes the environment it is meant to be testing reproduces nothing. Code
    under test may still set them for its own children - that is
    dobby.core.platform's job and is part of what is being verified.
    """
    env = dict(os.environ)
    for masking in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO"):
        env.pop(masking, None)
    return env


CHILD_ENV = ci_env()

# KNOWN GAP, stated rather than papered over: a runner has no agent CLIs, this
# machine has four, and their presence changes provider detection, panel size
# and several verdicts. There is no switch to force the zero case, so this
# script cannot reproduce a failure that only occurs with an empty fleet. If one
# is ever suspected, the reproduction is to run with a PATH that excludes the
# CLI directories. Asserting a suppression that does not exist would be worse
# than naming the limit.
PROVIDER_CAVEAT = ("providers installed here are NOT suppressed; an "
                   "empty-fleet-only failure will not reproduce")


def find_interpreters(only: str | None) -> list[tuple[str, str]]:
    """Locate every CPython this machine has, newest label first.

    `py -0p` is the reliable enumerator on Windows. Elsewhere, probe the version
    names the matrix uses plus the running interpreter.
    """
    found: dict[str, str] = {}
    if os.name == "nt" and shutil.which("py"):
        proc = subprocess.run(["py", "-0p"], capture_output=True, text=True)
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith("-"):
                label = parts[0].lstrip("-").replace("-64", "").replace(
                    "-32", "")
                path = " ".join(parts[1:]).strip()
                if os.path.exists(path):
                    found.setdefault(label, path)
    for name in ("python3.10", "python3.11", "python3.12", "python3.13"):
        path = shutil.which(name)
        if path:
            found.setdefault(name.replace("python", ""), path)
    found.setdefault(f"{sys.version_info[0]}.{sys.version_info[1]}",
                     sys.executable)
    pairs = sorted(found.items(), key=lambda kv: kv[0])
    if only:
        pairs = [(lab, p) for lab, p in pairs if lab.startswith(only)]
    return pairs


def parse_workflow_steps() -> list[str]:
    """Read the step names out of the workflow so drift is detectable.

    Deliberately a text scan rather than a YAML load: this must work in a clone
    before dependencies are installed, and the only thing needed is the names.
    """
    if not os.path.exists(WORKFLOW):
        return []
    names = []
    with open(WORKFLOW, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("- name:"):
                names.append(stripped[len("- name:"):].strip())
    return names


def clone_head(dest: str) -> str:
    clone = os.path.join(dest, "c")
    proc = subprocess.run(["git", "clone", "--quiet", REPO, clone],
                          capture_output=True, text=True, env=CHILD_ENV,
                          timeout=900)
    if proc.returncode != 0:
        raise SystemExit(f"clone failed: {(proc.stderr or '')[-400:]}")
    return clone


def run(label: str, argv: list[str], cwd: str, timeout: int = 3600) -> dict:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=CHILD_ENV,
                          timeout=timeout)
    ok = proc.returncode == 0
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if not ok:
        # Print enough to identify the failure without opening a browser: the
        # test-runner tail is where unittest puts FAIL/ERROR blocks.
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        for line in tail[-45:]:
            print(f"        {line}", flush=True)
    return {"label": label, "rc": proc.returncode,
            "tail": ((proc.stderr or "") + (proc.stdout or ""))[-4000:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", help="only interpreters whose label starts here")
    ap.add_argument("--keep", action="store_true",
                    help="leave the clone in place for inspection")
    ap.add_argument("--json", help="write the full record here")
    args = ap.parse_args()

    interpreters = find_interpreters(args.python)
    if not interpreters:
        print("no interpreter matched", file=sys.stderr)
        return 2

    declared = parse_workflow_steps()
    mirrored = {label for label, _ in CLI_STEPS}
    # Names that are covered but not by an identical string.
    ALIASES = {"install the only dependency", "engine tests",
               "environment the tests will actually see",
               "four meta-tools only (progressive disclosure)"}
    missing = [n for n in declared
               if n not in mirrored and n not in ALIASES
               and not any(n.startswith(m) for m in mirrored)]
    if missing:
        print(f"NOTE: workflow steps not mirrored here: {missing}\n"
              "      this script is now incomplete - add them", flush=True)

    tmp = tempfile.mkdtemp(prefix="dobby_ci_local_")
    print(f"clone -> {tmp}", flush=True)
    probe = clone_head(os.path.join(tmp, "probe"))
    head = subprocess.run(["git", "-C", probe, "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True,
                          env=CHILD_ENV).stdout.strip()
    boot = os.path.exists(os.path.join(probe, ".dobby", "knowledge",
                                       "kg.bootstrap.json"))
    modules = len(glob.glob(os.path.join(probe, "tests", "test_*.py")))
    print(f"HEAD={head}  test modules={modules}  "
          f"kg.bootstrap.json in clone={boot}", flush=True)
    print(f"interpreters: {[lab for lab, _ in interpreters]}", flush=True)
    print(f"caveat: {PROVIDER_CAVEAT}\n", flush=True)

    record = {"head": head, "runs": []}
    failed_labels = []

    for label, exe in interpreters:
        print(f"--- python {label} ({exe}) ---", flush=True)
        ver = subprocess.run([exe, "-V"], capture_output=True, text=True)
        print(f"  {(ver.stdout or ver.stderr).strip()}", flush=True)

        # Show the encoding the tests will actually get. This is the variable
        # that was masked for fourteen red runs, so it is reported, not assumed.
        enc = subprocess.run(
            [exe, "-c", "import sys;print(sys.stdout.encoding,"
             "sys.getfilesystemencoding(),sys.flags.utf8_mode)"],
            capture_output=True, text=True, env=CHILD_ENV)
        print(f"  stdout/fs encoding, utf8_mode: "
              f"{(enc.stdout or '').strip()}", flush=True)

        dep = subprocess.run([exe, "-c", "import yaml"], capture_output=True,
                             text=True)
        if dep.returncode != 0:
            print("  SKIP  PyYAML missing for this interpreter "
                  "(pip install PyYAML to include it)", flush=True)
            record["runs"].append({"python": label, "skipped": "no PyYAML"})
            continue

        # A FRESH clone per interpreter. Sharing one was a real defect, and this
        # script caught it on itself: `init --scan .` is not idempotent, so the
        # second interpreter hit FileExistsError on a kg.bootstrap.json the first
        # had written. CI gives every job its own checkout, and a mirror that
        # does not is testing a sequence the pipeline never runs.
        clone = clone_head(os.path.join(tmp, f"py{label}"))

        # The engine suite first, in one process, exactly as the workflow does.
        results = [run("engine tests (discover, one process)",
                       [exe, "-m", "unittest", "discover", "-s", "tests", "-q"],
                       clone)]
        for step_label, argv in CLI_STEPS:
            results.append(run(step_label, [exe] + argv, clone, timeout=1800))

        bad = [r["label"] for r in results if r["rc"] != 0]
        failed_labels += [f"py{label}: {b}" for b in bad]
        record["runs"].append({"python": label, "exe": exe,
                               "results": results, "failed": bad})
        print("", flush=True)

    print("=" * 60, flush=True)
    if failed_labels:
        print(f"FAIL - {len(failed_labels)} step(s) would break CI:", flush=True)
        for f in failed_labels:
            print(f"  {f}", flush=True)
    else:
        print("PASS - every mirrored CI step passed on every interpreter here.",
              flush=True)
        print("Still read the run status: this cannot reproduce another OS.",
              flush=True)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
        print(f"record -> {args.json}", flush=True)

    if args.keep:
        print(f"clones kept under {tmp}", flush=True)
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    return 1 if failed_labels else 0


if __name__ == "__main__":
    sys.exit(main())
