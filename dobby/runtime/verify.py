"""The verifier gate — what turns a produced artifact into a usable one.

Three layers, in the order they are trusted:

    deterministic   tests, linters, type checks, a file existing, a schema
                    satisfied. Machine-readable pass/fail with a log.
    grounded        does the quoted evidence exist in the cited source; does a
                    recomputation agree with the reported number.
    semantic        does this actually answer what was asked.

The order is the whole design. A deterministic check that CAN run outranks any
model's opinion, and when one exists the semantic layer is advisory. This is the
same rule `docs/EVAL_DESIGN.md` already states for end-task evaluation, applied
to the runtime: a model grading its own family of outputs measures agreement,
not correctness.

Only the first two layers are implemented here. The third is deliberately left
to the existing `judge`/`review` modules, invoked as an ordinary node, so a
semantic verdict costs a visible provider call rather than arriving free and
unbudgeted inside the gate.

A failing verdict is a repair instruction, not a rejection notice. Every
`VerifierResult` carries what failed, the evidence for it, and the class of
retry it implies — because a planner that only learns "verify failed" can do
nothing except run the whole node again.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field

from ..core.platform import child_env, resolve_command
from .contracts import ArtifactContract
from .failures import Failure, classify_verifier_failure

#: A check gets its own wall clock. A hung test suite must fail the gate, not
#: hang the run — and an unbounded verifier is how a "safety" layer becomes the
#: least reliable part of a system.
DEFAULT_CHECK_TIMEOUT_S = 900


@dataclass
class CheckRecord:
    """One acceptance check and what it actually did."""

    check: str
    passed: bool
    exit_code: int | None = None
    detail: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {"check": self.check, "passed": self.passed,
                "exit_code": self.exit_code, "detail": self.detail[:800],
                "duration_s": self.duration_s}


@dataclass
class VerifierResult:
    """The verdict, and everything a repair step needs to act on it."""

    passed: bool
    failed_requirements: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    repair_hint: str = ""
    #: The failure class this verdict implies, so the scheduler does not have to
    #: re-derive it from prose.
    failure: Failure | None = None
    records: list[CheckRecord] = field(default_factory=list)
    #: Checks that could not run here — a suite needing a binary this machine
    #: does not have. NOT counted as passes. A gate that reports "all checks
    #: passed" when three of them never ran is worse than no gate.
    not_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "failed_requirements": list(self.failed_requirements),
                "evidence_refs": list(self.evidence_refs),
                "repair_hint": self.repair_hint,
                "failure": self.failure.to_dict() if self.failure else None,
                "records": [r.to_dict() for r in self.records],
                "not_run": list(self.not_run)}


class Verifier:
    """Runs a contract's checks against a produced payload."""

    def __init__(self, repo: str, *, timeout_s: int = DEFAULT_CHECK_TIMEOUT_S,
                 log_dir: str | None = None):
        self.repo = os.path.abspath(repo)
        self.timeout_s = timeout_s
        self.log_dir = log_dir

    def verify(self, contract: ArtifactContract, payload, *,
               node_id: str = "", env_extra: dict | None = None
               ) -> VerifierResult:
        """Shape first, then the commands.

        Shape first because a payload of the wrong shape makes every downstream
        check meaningless — a test command that reads `changed_files` from a
        payload that has none fails for a reason that has nothing to do with the
        code. Reporting that as a test failure sends the repair to the wrong
        place.
        """
        problems = contract.check_shape(payload)
        if problems:
            failure = classify_verifier_failure(
                problems, schema_error="; ".join(problems[:6]))
            return VerifierResult(
                passed=False, failed_requirements=problems,
                repair_hint=(
                    "return the same content in the declared shape; the listed "
                    "paths are the ones that did not match"),
                failure=failure)

        records: list[CheckRecord] = []
        not_run: list[str] = []
        for check in contract.acceptance_checks:
            record = self._run_check(check, env_extra=env_extra,
                                     node_id=node_id)
            if record.exit_code is None and not record.passed:
                not_run.append(check)
            records.append(record)

        failed = [r.check for r in records if not r.passed]
        if not failed:
            return VerifierResult(
                passed=True, records=records,
                evidence_refs=[r.check for r in records], not_run=not_run)

        detail = next((r.detail for r in records if not r.passed), "")
        return VerifierResult(
            passed=False, failed_requirements=failed, records=records,
            not_run=not_run,
            evidence_refs=[r.check for r in records if not r.passed],
            repair_hint=(
                "read the failing output below and change the artifact, not the "
                "check: " + detail[:400]),
            failure=classify_verifier_failure(failed))

    def _run_check(self, check: str, *, env_extra: dict | None = None,
                   node_id: str = "") -> CheckRecord:
        command = resolve_command(check)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.repo, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=child_env(env_extra), timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return CheckRecord(check, False, exit_code=None,
                               detail=f"no verdict within {self.timeout_s}s",
                               duration_s=round(time.monotonic() - started, 2))
        except OSError as exc:
            # Cannot run is NOT a pass and NOT a fail of the artifact: it is a
            # missing capability of this machine, and it is reported as such.
            return CheckRecord(check, False, exit_code=None,
                               detail=f"could not run this check here: {exc}",
                               duration_s=round(time.monotonic() - started, 2))
        duration = round(time.monotonic() - started, 2)
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if self.log_dir and node_id:
            self._write_log(node_id, check, proc.returncode, output)
        return CheckRecord(check, proc.returncode == 0,
                           exit_code=proc.returncode,
                           detail=output[-1500:], duration_s=duration)

    def _write_log(self, node_id: str, check: str, code: int,
                   output: str) -> None:
        os.makedirs(self.log_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in node_id)
        path = os.path.join(self.log_dir, f"{safe}.checks.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"\n$ {check}\nexit {code}\n{output}\n")


def promotable(contract: ArtifactContract, verdict: VerifierResult) -> bool:
    """The machine promotion rule, and the only one.

    `ArtifactContract.promotion_rule` is prose for the report. THIS is the gate,
    and it is intentionally not configurable at runtime: a threshold a run may
    lower when it is failing is not a gate, it is a suggestion.

    A check that could not run blocks promotion. That is stricter than it needs
    to be for a lint, and it is correct for the case that matters: a machine
    missing the test runner would otherwise promote an unverified patch and
    report it as verified.
    """
    return bool(verdict.passed and not verdict.not_run)
