"""Evaluator layer: deterministic-first, criterion-graded, generator-isolated.

Criteria live in a JSON file whose sha256 is pinned into every evaluation
record; if the generator rewrites criteria mid-run, verify_criteria_integrity
fails (anti Evaluation-Gaming / Echo-Chamber Review). Deterministic checks run
first. Model-based judgment is available via `judge=True` (see dobby/judge.py)
but is ADVISORY: `.dobby/ontology.json` states that a model_assertion is never
verification, so a judged criterion is excluded from the PASS/FAIL verdict no
matter what the model says. Judging is opt-in because it costs money and reaches
an external service.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time

from .platform import resolve_command, child_env
from .security import guard_command, cap_output, redact_secrets, load_protected


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Evaluator:
    def __init__(self, criteria_path: str, workdir: str,
                 config: dict | None = None, *,
                 judge: bool = False, artifact: str | None = None,
                 judge_provider: str | None = None,
                 judge_exclude: set[str] | None = None):
        """`judge` is opt-in and defaults off.

        Model judgment costs money and reaches an external service, so it is
        never implicit — the same rule `providers/run.py::probe` follows for the
        only other paid path. `judge_exclude` names providers that must not grade
        this work, which is how the author is kept from grading itself.
        """
        self.criteria_path = criteria_path
        self.workdir = workdir
        self.judge = judge
        self.artifact = artifact
        self.judge_provider = judge_provider
        self.judge_exclude = judge_exclude or set()
        self.protected = load_protected(config)
        with open(criteria_path, encoding="utf-8") as f:
            self.criteria = json.load(f)["criteria"]
        self.criteria_hash = _sha256_file(criteria_path)

    def verify_criteria_integrity(self) -> bool:
        return _sha256_file(self.criteria_path) == self.criteria_hash

    def run_check(self, crit: dict) -> dict:
        """One criterion -> one record with observed evidence."""
        record = {
            "criterion": crit["id"],
            "description": crit["description"],
            "severity": crit.get("severity", "medium"),
            "kind": crit["kind"],
            "passed": None, "confidence": None,
            "evidence": None, "recommended_action": crit.get("on_fail", ""),
        }
        if crit["kind"] == "command":
            # Resolve `{python}` to the running interpreter BEFORE the guard so
            # the audited string is exactly the string that executes.
            command = resolve_command(crit["command"])
            allowed, reason = guard_command(command, self.protected)
            if not allowed:
                record.update(passed=False, confidence=1.0,
                              evidence=f"BLOCKED by command guard: {reason}")
                return record
            try:
                proc = subprocess.run(
                    command, shell=True, cwd=self.workdir,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=child_env(),
                    timeout=crit.get("timeout_s", 120))
                out = cap_output(redact_secrets(proc.stdout + proc.stderr), 4000)
                want = crit.get("expect_exit", 0)
                record.update(
                    passed=(proc.returncode == want),
                    confidence=1.0,
                    evidence=f"exit={proc.returncode} (want {want})\n{out}")
            except subprocess.TimeoutExpired:
                record.update(passed=False, confidence=1.0, evidence="TIMEOUT")
        elif crit["kind"] == "path_exists":
            p = os.path.join(self.workdir, crit["path"])
            record.update(passed=os.path.exists(p), confidence=1.0,
                          evidence=f"exists({crit['path']})={os.path.exists(p)}")
        elif crit["kind"] == "path_absent":
            p = os.path.join(self.workdir, crit["path"])
            record.update(passed=not os.path.exists(p), confidence=1.0,
                          evidence=f"absent({crit['path']})={not os.path.exists(p)}")
        elif crit["kind"] == "model_judgment":
            # Advisory whether or not a judge runs, so no caller can mistake the
            # answer for a measurement. See dobby/judge.py for why the ontology
            # forbids a model verdict from producing a verified result.
            record["advisory"] = True
            if not self.judge:
                record.update(
                    passed=None, confidence=0.0,
                    evidence="NOT RUN: model judgment is opt-in (it costs money "
                             "and reaches an external service). Pass judge=True, "
                             "or `dobby slice --judge`, with an artifact to grade.")
            elif not self.artifact:
                record.update(
                    passed=None, confidence=0.0,
                    evidence="NOT RUN: judging was requested but no artifact was "
                             "supplied; there is nothing to grade.")
            else:
                from ..judge import judge_criterion
                verdict = judge_criterion(
                    crit, self.artifact, provider_id=self.judge_provider,
                    exclude=self.judge_exclude, cwd=self.workdir)
                record.update(
                    passed=verdict["passed"], confidence=verdict["confidence"],
                    evidence=verdict["evidence"])
                record["judge_provider"] = verdict["judge_provider"]
                record["verdict_token"] = verdict["verdict_token"]
        else:
            record.update(passed=False, confidence=1.0,
                          evidence=f"unknown criterion kind '{crit['kind']}'")
        return record

    def evaluate(self, criteria_ids: list[str] | None = None) -> dict:
        selected = [c for c in self.criteria
                    if criteria_ids is None or c["id"] in criteria_ids]
        records = [self.run_check(c) for c in selected]

        # The verdict is DETERMINISTIC-ONLY. Advisory records are excluded even
        # when they carry a True/False, because `.dobby/ontology.json` states
        # that a model_assertion is never verification. Without this filter,
        # wiring the judge adapter would have silently let a model opinion carry
        # the same weight as a test exit code, and the claim that this evaluator
        # is deterministic-first would have become false without a line of it
        # being edited.
        det = [r for r in records
               if r["passed"] is not None and not r.get("advisory")]
        advisory = [r for r in records if r.get("advisory")]
        verdict = "PASS" if det and all(r["passed"] for r in det) else \
                  "FAIL" if any(r["passed"] is False for r in det) else "NO_DETERMINISTIC_CHECKS"
        return {
            "verdict": verdict,
            "verdict_basis": f"{len(det)} deterministic check(s); "
                             f"{len(advisory)} advisory judgment(s) excluded",
            "criteria_hash": self.criteria_hash,
            "criteria_integrity": self.verify_criteria_integrity(),
            "records": records,
            "advisory": [
                {"criterion": r["criterion"], "passed": r["passed"],
                 "verdict_token": r.get("verdict_token"),
                 "judge_provider": r.get("judge_provider"),
                 "confidence": r["confidence"], "evidence": r["evidence"]}
                for r in advisory],
            "not_evaluated": [r["criterion"] for r in records
                              if r["passed"] is None],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
