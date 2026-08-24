"""Run the promotion evidence for `runnable-gates` and `ledgered-task`.

    python evals/gates/run_scenarios.py [--out evals/gates/RESULTS.json]

Every check is decided by an exit code or a value read out of the CLI's own JSON.
Nothing here asks for an opinion, which is what `author-evals` step 1 requires of
a scenario and what makes the result usable as `record_eval_pass` evidence.

The fixtures are copied into a temp directory before running, and approvals are
redirected there too. A scenario that wrote into the repository — or into the
real approval store — would be measuring a machine it had just modified.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
MISSING = object()


def _dig(doc, pointer: str):
    """`summary.unmet`, `verdicts.0.exit_code`. Returns MISSING, never raises.

    A pointer that does not resolve is a FAILED check and not a crash: the
    commonest reason for one is that the CLI stopped emitting the field the
    scenario is about, which is exactly the regression worth reporting.
    """
    node = doc
    for part in pointer.split("."):
        if isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return MISSING
            node = node[int(part)]
        elif isinstance(node, dict):
            if part not in node:
                return MISSING
            node = node[part]
        else:
            return MISSING
    return node


def _cli(argv: list, ledger: str, cwd: str, approvals: str) -> tuple[int, dict]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["DOBBY_APPROVAL_DIR"] = approvals
    proc = subprocess.run(
        [sys.executable, "-m", "dobby.cli"] + argv + ["--file", ledger,
                                                      "--cwd", cwd],
        cwd=REPO, capture_output=True, encoding="utf-8", errors="replace",
        env=env, timeout=600)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except (ValueError, TypeError):
        return proc.returncode, {"_unparseable_stdout": (proc.stdout or "")[:400],
                                 "_stderr": (proc.stderr or "")[-400:]}


def _swap_check(ledger: str) -> None:
    """Replace the approved command with a different one, in place."""
    with open(ledger, encoding="utf-8") as fh:
        text = fh.read()
    swapped = text.replace("print('approved-output')",
                           "print('approved-output'); print('AND SOMETHING ELSE')")
    if swapped == text:
        raise RuntimeError("approval fixture did not contain the expected CHECK")
    with open(ledger, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(swapped)


def run_gate_scenario(scenario: dict, work: str) -> dict:
    ledger = os.path.join(work, os.path.basename(scenario["fixture"]))
    shutil.copy2(os.path.join(HERE, scenario["fixture"]), ledger)
    approvals = os.path.join(work, "approved")
    checks, staged = [], set()

    for check in scenario["verification"]:
        stage = check.get("after")
        if stage and stage not in staged:
            staged.add(stage)
            if stage == "approve":
                _cli(["gates", "approve"], ledger, work, approvals)
            elif stage == "swap":
                _swap_check(ledger)

        if check["kind"] == "command":
            code, doc = _cli(check["argv"], ledger, work, approvals)
            passed = code == check["expect_exit"]
            checks.append({"id": check["id"], "passed": passed,
                           "expected_exit": check["expect_exit"],
                           "actual_exit": code})
            scenario.setdefault("_last", {})["doc"] = doc
        elif check["kind"] == "json":
            doc = scenario.get("_last", {}).get("doc", {})
            got = _dig(doc, check["pointer"])
            passed = got is not MISSING and got == check["equals"]
            checks.append({"id": check["id"], "passed": passed,
                           "pointer": check["pointer"],
                           "expected": check["equals"],
                           "actual": None if got is MISSING else got,
                           "resolved": got is not MISSING})
        else:
            checks.append({"id": check["id"], "passed": False,
                           "error": f"unknown kind {check['kind']!r}"})
    scenario.pop("_last", None)
    return {"checks": checks}


def run_file_scenario(scenario: dict) -> dict:
    checks = []
    for check in scenario["verification"]:
        target = os.path.join(REPO, check["path"])
        if not os.path.exists(target):
            checks.append({"id": check["id"], "passed": False,
                           "error": f"{check['path']} does not exist"})
            continue
        with open(target, encoding="utf-8") as fh:
            body = fh.read()
        missing = [n for n in check["needles"] if n not in body]
        checks.append({"id": check["id"], "passed": not missing,
                       "path": check["path"], "missing": missing})
    return {"checks": checks}


def main(out: str) -> int:
    with open(os.path.join(HERE, "scenarios.json"), encoding="utf-8") as fh:
        book = json.load(fh)

    results = []
    work_root = tempfile.mkdtemp(prefix="dobby-gates-evals-")
    try:
        for scenario in book["scenarios"]:
            if scenario.get("status") in ("blocked", "superseded"):
                # Not run and not counted. A scenario whose subject was
                # rejected and reverted cannot pass, and letting it sit in the
                # failure column would make a respected rejection look like an
                # unfixed bug — while deleting it would erase the reason.
                results.append({"id": scenario["id"], "skill": scenario["skill"],
                                "claim": scenario["claim"], "blocked": True,
                                "reason": scenario.get("blocked_reason", ""),
                                "passed": False, "checks": []})
                print(f"BLOCK {scenario['id']:<24} {scenario['skill']}",
                      flush=True)
                continue
            started = time.monotonic()
            if scenario.get("fixture"):
                work = os.path.join(work_root, scenario["id"])
                os.makedirs(work, exist_ok=True)
                outcome = run_gate_scenario(scenario, work)
            else:
                outcome = run_file_scenario(scenario)
            passed = all(c["passed"] for c in outcome["checks"])
            results.append({
                "id": scenario["id"], "skill": scenario["skill"],
                "claim": scenario["claim"], "passed": passed,
                "wall_s": round(time.monotonic() - started, 2),
                "checks": outcome["checks"]})
            mark = "PASS" if passed else "FAIL"
            print(f"{mark}  {scenario['id']:<24} {scenario['skill']}",
                  flush=True)
            for check in outcome["checks"]:
                if not check["passed"]:
                    print(f"        {check['id']}: {check}", flush=True)
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    live = [r for r in results if not r.get("blocked")]
    by_skill: dict = {}
    for row in results:
        entry = by_skill.setdefault(row["skill"],
                                    {"passed": [], "failed": [], "blocked": []})
        if row.get("blocked"):
            entry["blocked"].append(row["id"])
        else:
            entry["passed" if row["passed"] else "failed"].append(row["id"])

    document = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "scenarios": len(live),
        "blocked": len(results) - len(live),
        "passed": sum(1 for r in live if r["passed"]),
        "by_skill": by_skill,
        "results": results,
    }
    target = os.path.abspath(out)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(document, fh, ensure_ascii=False, indent=1)

    print(f"\n{document['passed']}/{document['scenarios']} scenarios passed")
    for skill, entry in sorted(by_skill.items()):
        print(f"  {skill:<16} distinct passing: {len(entry['passed'])} "
              f"{entry['passed']}")
        if entry["failed"]:
            print(f"  {'':<16} FAILING: {entry['failed']}")
        if entry["blocked"]:
            print(f"  {'':<16} blocked: {entry['blocked']}")
    print(f"wrote {target}")
    # Exit code is the verdict, same contract the gates CLI uses.
    return 0 if document["passed"] == document["scenarios"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(HERE, "RESULTS.json"))
    raise SystemExit(main(parser.parse_args().out))
