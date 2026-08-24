"""Did the change actually WORK — measured by the instance's own tests.

Why this exists
---------------
File-level localization answers "did it edit the right files" and nothing more.
An agent that opens the correct five files and writes nonsense into them scores
recall 1.0. That was the strongest thing available while the tests were out of
reach, and it is not the question anybody actually has.

`dobby/swebench.py` says resolution needs the official Docker images, and Docker
is absent here. That is true of reproducing ALL instances. It turned out not to
be true of a particular one. Measured 2026-08-24 on django__django-11532:

    baseline + test_patch  ->  test_non_ascii_dns_non_unicode_email  FAILED
    gold     + test_patch  ->  test_non_ascii_dns_non_unicode_email  ok
    163 tests, 5.6 seconds, in a venv holding asgiref/sqlparse/pytz

So the instance's own FAIL_TO_PASS is obtainable, and this module obtains it.

WHAT THIS IS NOT
----------------
Not a SWE-bench score. The official harness pins every dependency per instance;
this one runs against whatever the venv happens to hold, on python 3.11 where
django 3.0 expects 3.6-3.8. Two of the 163 tests fail WITH THE GOLD PATCH
APPLIED, both with `UnicodeEncodeError: surrogates not allowed`, which is a
python-version difference and not anything an agent did.

That is exactly why every run is scored against a GOLD CALIBRATION rather than
against the instance's raw PASS_TO_PASS list. The gold patch is the ceiling this
machine can reach; a test the gold patch cannot pass is broken here, and holding
an agent to it would score the environment and call it the agent. `resolved` is
therefore reported as `resolved_local`, with the excluded tests named.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

#: SWE-bench records a test as `<label> (<module.Class>)`, where `<label>` is the
#: method name — USUALLY. When the test has a docstring, unittest displays the
#: docstring instead and SWE-bench stores that, so the list also holds entries
#: like `#23063 -- RFC-compliant messages are sent over SMTP (mail.tests.X)`.
#:
#: A first version anchored on `^(\w+)\s+\(` and fell through for those, and
#: `django_modules` then split the DOCSTRING on "." and asked django to run a
#: test module called `#23063 -- RFC-compliant messages are sent over SMTP`.
#: The dotted path in the parentheses is the part that is always there, so that
#: is what is matched.
_SWEBENCH_ID = re.compile(r"^(?P<label>.*?)\s*\((?P<path>[\w.]+)\)\s*$")

_RESULT_LINE = re.compile(
    r"^(?P<name>\w+)\s+\((?P<path>[\w.]+?)(?:\.(?P=name))?\)\s*"
    r"(?:\.\.\.)?\s*(?P<verdict>ok|FAIL|ERROR|skipped.*)$", re.M)


class UnsupportedRepo(RuntimeError):
    """This repository's test runner is not driven from here yet."""


def parse_swebench_test_id(raw: str) -> tuple[str, str]:
    """`<label> (<mod.Class>)` -> (dotted path, label). ("", raw) if unparseable.

    The label is kept because it is what unittest PRINTS, and matching the run's
    output is done on the printed form. Only the dotted path is reliable enough
    to build a module list from.
    """
    match = _SWEBENCH_ID.match(raw.strip())
    if not match:
        return "", raw.strip()
    return match.group("path"), match.group("label")


def test_ids(instance: dict, key: str) -> list[str]:
    """Fully-qualified names for entries that carry a method name, else path."""
    raw = instance.get(key) or "[]"
    values = json.loads(raw) if isinstance(raw, str) else list(raw)
    out = []
    for value in values:
        path, label = parse_swebench_test_id(value)
        if not path:
            continue
        # A label that is a valid identifier is the method name; a docstring is
        # not, and for those the class path is as specific as this can be.
        out.append(f"{path}.{label}" if label.isidentifier() else path)
    return out


def django_modules(ids) -> list[str]:
    """The test LABELS to hand django's runner: top-level modules only.

    Whole modules, never single tests. Measured: running
    `mail.tests.MailTests.test_non_ascii_dns_non_unicode_email` alone errors with
    `'CachedDnsName' object has no attribute '_fqdn'` even with the gold patch
    applied, because the test deletes a cache another test in the module fills.
    Running one test in isolation is a different experiment from the one
    SWE-bench specifies, and it fails the gold patch, which is how this was
    caught.
    """
    modules = set()
    for identifier in ids:
        head = identifier.split(".", 1)[0]
        if head.isidentifier():
            modules.add(head)
    return sorted(modules)


def apply_patch(repo: str, patch: str, *, label: str) -> None:
    if not (patch or "").strip():
        return
    path = os.path.join(repo, f".{label}.diff")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(patch)
    proc = subprocess.run(["git", "-C", repo, "apply", path],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)
    os.remove(path)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} did not apply to {repo}: "
                           f"{(proc.stderr or '').strip()[:300]}")


def run_django_tests(repo: str, modules, *, python: str,
                     timeout_s: int = 900) -> dict:
    """Every test in `modules`, as a name -> verdict map.

    Runs from `repo/tests` with `repo` on PYTHONPATH, which is how django's own
    runner expects to be driven and the reason a first attempt failed with
    `ModuleNotFoundError: No module named 'django'` — the checkout IS django and
    nothing had put it on the path.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.abspath(repo)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [python, "runtests.py", "--verbosity", "2", *modules],
        cwd=os.path.join(repo, "tests"), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=timeout_s)
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")

    verdicts: dict[str, str] = {}
    for match in _RESULT_LINE.finditer(blob):
        name = f"{match.group('path')}.{match.group('name')}"
        verdict = match.group("verdict")
        verdicts[name] = ("skipped" if verdict.startswith("skipped")
                          else verdict)
    # A summary line is not optional: a run that crashed before reporting any
    # test would otherwise look like "no failures".
    ran = re.search(r"^Ran (\d+) tests?", blob, re.M)
    return {"verdicts": verdicts, "ran": int(ran.group(1)) if ran else 0,
            "returncode": proc.returncode,
            "crashed": ran is None,
            "tail": blob.strip().splitlines()[-12:]}


def runner_for(instance: dict):
    repo = instance.get("repo", "")
    if repo == "django/django":
        return run_django_tests
    raise UnsupportedRepo(
        f"{repo} is not driven from here yet. django is, via tests/runtests.py; "
        f"astropy and the rest use pytest and need their own entry point. "
        f"Refusing rather than guessing an invocation and reporting its failure "
        f"as the agent's")


def calibrate(instance: dict, repo: str, *, python: str,
              timeout_s: int = 900) -> dict:
    """What the GOLD patch achieves here. The ceiling every arm is scored against.

    `repo` must be a clean checkout at `base_commit`; this applies the test patch
    and the gold patch to it and is therefore destructive to that tree.
    """
    run = runner_for(instance)
    apply_patch(repo, instance.get("test_patch") or "", label="test_patch")
    apply_patch(repo, instance.get("patch") or "", label="gold")

    f2p = test_ids(instance, "FAIL_TO_PASS")
    p2p = test_ids(instance, "PASS_TO_PASS")
    modules = django_modules(f2p + p2p)
    result = run(repo, modules, python=python, timeout_s=timeout_s)
    verdicts = result["verdicts"]

    # Regressions are judged by SET DIFFERENCE on what fails, not by looking up
    # PASS_TO_PASS names: SWE-bench stores a docstring instead of a method name
    # wherever the test has one, and those cannot be matched against the run's
    # output by name at all. What CAN be compared is "which tests failed here
    # with gold" against "which tests failed here with the agent's edit".
    failing = sorted(n for n, v in verdicts.items()
                     if v not in ("ok", "skipped"))
    passing = sorted(n for n, v in verdicts.items() if v == "ok")
    return {
        "modules": modules,
        "ran": result["ran"],
        "crashed": result["crashed"],
        "fail_to_pass": f2p,
        "pass_to_pass_count": len(p2p),
        "passing_with_gold": passing,
        "failing_with_gold": failing,
        "fail_to_pass_achievable": [n for n in f2p if verdicts.get(n) == "ok"],
        "note": ("`failing_with_gold` fails WITH THE GOLD PATCH APPLIED and is "
                 "therefore broken in this environment, not by any agent. It is "
                 "excluded from scoring. Measured cause on django 3.0 under "
                 "python 3.11: UnicodeEncodeError, surrogates not allowed"),
        "tail": result["tail"],
    }


def score(instance: dict, repo: str, calibration: dict, *, python: str,
          timeout_s: int = 900) -> dict:
    """One arm's tree against the gold-calibrated ceiling.

    The agent's edits are already in `repo`; only the TEST patch is applied, so
    the tests it must satisfy are the instance's and not its own.
    """
    run = runner_for(instance)
    apply_patch(repo, instance.get("test_patch") or "", label="test_patch")
    result = run(repo, calibration["modules"], python=python,
                 timeout_s=timeout_s)
    verdicts = result["verdicts"]

    want_fix = list(calibration["fail_to_pass_achievable"])
    fixed = [n for n in want_fix if verdicts.get(n) == "ok"]

    failing_now = {n for n, v in verdicts.items() if v not in ("ok", "skipped")}
    # Anything already broken under gold is the environment's fault, not this
    # arm's, so it is subtracted rather than charged.
    regressions = sorted(failing_now
                         - set(calibration["failing_with_gold"])
                         - set(want_fix))

    return {
        "resolved_local": (bool(want_fix) and len(fixed) == len(want_fix)
                           and not regressions and not result["crashed"]),
        "fail_to_pass_total": len(want_fix),
        "fail_to_pass_fixed": len(fixed),
        "fail_to_pass_missing": sorted(set(want_fix) - set(fixed)),
        "regressions": regressions,
        "regression_count": len(regressions),
        "ran": result["ran"],
        "crashed": result["crashed"],
        # Partial credit, and the reason this is worth having next to
        # `resolved_local`: an agent that fixes two of three FAIL_TO_PASS tests
        # without breaking anything has done something a boolean erases.
        "fix_rate": (round(len(fixed) / len(want_fix), 3) if want_fix else None),
        "excluded_as_environment": calibration["failing_with_gold"],
        "tail": result["tail"],
    }
