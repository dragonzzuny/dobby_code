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
import re
import subprocess
import time
from dataclasses import dataclass, field

from ..core.platform import child_env, resolve_command
from .contracts import (ArtifactContract, V_NONE, level_name)
from .failures import Failure, classify_verifier_failure, NON_RETRYABLE, QUALITY_FAILURE

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
    #: The contract declared no shape, no check, no effect and nothing to
    #: ground, so nothing this node produced could have failed. Recorded rather
    #: than inferred from an empty `records`, because "everything passed" and
    #: "there was nothing to pass" are the two answers this field exists to
    #: keep apart.
    nothing_declared: bool = False
    #: The rung actually reached, from `contracts.VERIFICATION_LEVELS`.
    #:
    #: The contract's `declared_level` when everything declared ran and passed,
    #: and V_NONE otherwise -- a check that failed or could not run reached
    #: nothing, whatever it would have reached if it had. Carried so a PROMOTED
    #: artifact says at WHICH rung it was promoted; `passed=True` on its own was
    #: one word for four different claims.
    level: int = V_NONE

    @property
    def level_label(self) -> str:
        return level_name(self.level)

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "failed_requirements": list(self.failed_requirements),
                "evidence_refs": list(self.evidence_refs),
                "repair_hint": self.repair_hint,
                "failure": self.failure.to_dict() if self.failure else None,
                "records": [r.to_dict() for r in self.records],
                "not_run": list(self.not_run),
                "nothing_declared": self.nothing_declared,
                "level": self.level,
                "level_label": self.level_label}


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
        if contract.declares_nothing and not contract.ungraded:
            # Reported before the shape check, because there is no shape to
            # check. NON_RETRYABLE on purpose: this is a task DEFINITION that
            # cannot grade its own output, and running it again produces another
            # ungradeable result at the same price.
            return VerifierResult(
                passed=False, nothing_declared=True,
                failed_requirements=["the contract declares no schema, no "
                                     "acceptance check, no side effect and "
                                     "nothing to ground"],
                repair_hint=("give this node something its output could fail: "
                             "an output_schema, an acceptance_checks entry, or "
                             "a side_effect_class whose effect can be observed"),
                failure=Failure(
                    NON_RETRYABLE,
                    "nothing was declared for this node, so nothing it produced "
                    "could have failed; an ungraded artifact must not become an "
                    "input",
                    {"node_id": node_id}))
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

        # The grounded layer runs BEFORE the commands, on cost alone. Both are
        # deterministic and both block promotion; one is lexical matching over
        # text already in memory and the other can be a full test suite, so
        # failing on the cheap one first saves the expensive one.
        grounded = self.ground(contract, payload)
        if grounded and not grounded["passed"]:
            return VerifierResult(
                passed=False,
                failed_requirements=grounded["failed"],
                evidence_refs=grounded["evidence_refs"],
                repair_hint=grounded["repair_hint"],
                failure=classify_verifier_failure(grounded["failed"]),
                records=[CheckRecord(f"grounding:{name}", ok)
                         for name, ok in grounded["by_check"].items()])

        records: list[CheckRecord] = []
        not_run: list[str] = []
        for check in contract.acceptance_checks:
            record = self._run_check(check, env_extra=env_extra,
                                     node_id=node_id)
            if record.exit_code is None and not record.passed:
                not_run.append(check)
            records.append(record)

        prose = self._prose_verdict(contract, payload)
        if prose is not None:
            return prose

        failed = [r.check for r in records if not r.passed]
        if not failed:
            return VerifierResult(
                passed=True, records=records,
                evidence_refs=[r.check for r in records], not_run=not_run,
                # A deliberate control condition still reports the fact.
                # `promotable` lets it through on the strength of `ungraded`;
                # this is what makes the artifact travel labelled rather than
                # silently resembling one that was checked.
                nothing_declared=contract.declares_nothing,
                # Only on the passing path, and only when nothing was skipped.
                # A rung is what was CLIMBED, not what was pointed at.
                level=(contract.declared_level if not not_run else V_NONE))

        detail = next((r.detail for r in records if not r.passed), "")
        return VerifierResult(
            passed=False, failed_requirements=failed, records=records,
            not_run=not_run,
            evidence_refs=[r.check for r in records if not r.passed],
            repair_hint=(
                "read the failing output below and change the artifact, not the "
                "check: " + detail[:400]),
            failure=classify_verifier_failure(failed))

    def _prose_verdict(self, contract: ArtifactContract, payload):
        """The style gate, or None when the contract declares no prose.

        QUALITY_FAILURE and not CONTRACT_VIOLATION: the shape is fine and the
        writing is the problem, so the policy table sends it to REPAIR with the
        failure text in hand — and `style.rewrite_instruction` names the
        specific signals rather than saying "make it sound human", which
        produces a different generated voice rather than fewer signals.
        """
        if not contract.prose_at:
            return None
        from ..style import analyze, gate, rewrite_instruction

        text = _dig(payload, contract.prose_at)
        if not isinstance(text, str) or not text.strip():
            return None
        report = analyze(text)
        ok, why = gate(report)
        if ok:
            return None
        return VerifierResult(
            passed=False,
            failed_requirements=[f"prose at {contract.prose_at!r}: {why}"],
            repair_hint=rewrite_instruction(report),
            failure=Failure(QUALITY_FAILURE,
                            f"the generated-prose signature is present in "
                            f"{contract.prose_at!r}: {why}",
                            {"signals": report.get("acting_signals", [])}))

    # -- the grounded layer ------------------------------------------------
    def ground(self, contract: ArtifactContract, payload) -> dict | None:
        """Does the quoted evidence exist, and does the number survive a re-run?

        Returns None when the contract declares no grounding, so a node that
        makes no claims pays nothing.

        Two checks, because they catch the two ways a confident output is wrong:

        **Claims against a corpus.** Reuses `dobby/research.verify_claim`, whose
        matching is lexical overlap. That is a SCREEN and it is labelled as one
        there: it reliably finds "nothing in the corpus speaks to this" and
        cannot adjudicate a subtle mismatch. Used as a gate it therefore fails
        only the unsupported case, which is the case worth failing
        automatically.

        **Numbers against a re-run.** A reported figure is verified by producing
        it again, not by reading it again. The command's stdout is parsed as a
        number and compared within a tolerance the contract states. A figure the
        run cannot reproduce is the defect that survives to print.
        """
        spec = contract.grounding or {}
        if not spec:
            return None

        failed: list[str] = []
        evidence_refs: list[str] = []
        by_check: dict[str, bool] = {}
        hints: list[str] = []

        claims = _dig(payload, spec.get("claims_at", ""))
        if claims:
            corpus = self._corpus(spec)
            if not corpus:
                failed.append("grounding: claims were declared and no evidence "
                              "corpus could be read")
                by_check["corpus"] = False
                hints.append("point `evidence_files` at something that exists; "
                             "an unread corpus is not an empty one")
            else:
                from ..research import Claim, verify_claim
                for i, raw in enumerate(claims):
                    text = raw.get("claim") if isinstance(raw, dict) else str(raw)
                    if not text:
                        continue
                    verdict = verify_claim(Claim(text=text), corpus)
                    by_check[f"claim[{i}]"] = verdict.supported
                    if verdict.supported:
                        evidence_refs.extend(verdict.matched_evidence)
                    else:
                        failed.append(f"claim[{i}] unsupported: {text[:80]}")
                        hints.append(verdict.note)

        for rule in spec.get("recompute", []):
            name = rule.get("field", "?")
            ok, detail = self._recompute(payload, rule)
            by_check[f"recompute:{name}"] = ok
            if ok:
                evidence_refs.append(f"recompute:{name}")
            else:
                failed.append(f"recompute {name}: {detail}")
                hints.append("the fresh run wins; correct the artifact and say "
                             "so, rather than keeping the reported value")

        return {"passed": not failed, "failed": failed,
                "evidence_refs": evidence_refs, "by_check": by_check,
                "repair_hint": " | ".join(hints[:3])}

    def _corpus(self, spec: dict) -> list[dict]:
        """Evidence records, read from files or taken inline."""
        corpus = list(spec.get("evidence") or [])
        for rel in spec.get("evidence_files") or []:
            path = os.path.join(self.repo, rel)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                corpus.append({"id": rel, "text": handle.read()})
        return corpus

    def _recompute(self, payload, rule: dict) -> tuple[bool, str]:
        """Run the command, parse a number, compare to the reported one."""
        reported = _dig(payload, rule.get("field", ""))
        if reported is None:
            return False, f"the payload has no field {rule.get('field')!r}"
        command = resolve_command(rule.get("command", ""))
        if not command:
            return False, "no command to reproduce this number with"
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.repo, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                env=child_env(), timeout=rule.get("timeout_s", self.timeout_s))
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not re-derive it here: {exc}"
        if proc.returncode != 0:
            return False, (f"the reproduction command exited "
                           f"{proc.returncode}: "
                           f"{(proc.stderr or '').strip()[-200:]}")
        fresh = _first_number(proc.stdout or "")
        if fresh is None:
            return False, ("the reproduction command printed no number; its "
                           "stdout is the measurement and must contain one")
        try:
            reported_value = float(reported)
        except (TypeError, ValueError):
            return False, f"the reported value {reported!r} is not a number"
        tolerance = float(rule.get("tolerance", 0.0))
        if abs(fresh - reported_value) <= tolerance:
            return True, f"{reported_value} reproduced as {fresh}"
        return False, (f"reported {reported_value}, re-derived {fresh} "
                       f"(tolerance {tolerance})")

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


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _first_number(text: str) -> float | None:
    """The first number in a command's stdout, or None.

    First and not last: a measurement command should print its result, and a
    convention that reads the last number silently picks up a trailing line
    count or a timing suffix.
    """
    match = _NUMBER.search(text or "")
    return float(match.group(0)) if match else None


def _dig(payload, path: str):
    """`a.b[0].c` against nested dicts and lists. None when absent."""
    if not path:
        return None
    cursor = payload
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit():
            index = int(part)
            cursor = cursor[index] if index < len(cursor) else None
        else:
            return None
        if cursor is None:
            return None
    return cursor


def promotable(contract: ArtifactContract, verdict: VerifierResult) -> bool:
    """The machine promotion rule, and the only one.

    `ArtifactContract.promotion_rule` is prose for the report. THIS is the gate,
    and it is intentionally not configurable at runtime: a threshold a run may
    lower when it is failing is not a gate, it is a suggestion.

    A check that could not run blocks promotion. That is stricter than it needs
    to be for a lint, and it is correct for the case that matters: a machine
    missing the test runner would otherwise promote an unverified patch and
    report it as verified.

    So does a contract that declared nothing to check. `all([])` is True, so a
    contract with no shape, no acceptance check, no effect and nothing to ground
    used to promote whatever a model returned — measured, on a real contract:

        verify(ArtifactContract(), {"anything": "at all"})  ->  promotable: True

    That is the same shape as `not_run` one step earlier: a check that was never
    declared cannot be one that passed.

    Unless the contract SAYS it grades nothing. `ungraded=True` is a sentence
    someone wrote, and `runtime/bench.py` needs it: its baseline arm exists to
    show what the gate is worth and can only do that by running without one.
    Such an artifact promotes and then travels labelled, the same way `advisory`
    does. Declaring nothing by accident and declaring that you grade nothing are
    different facts, and this is where they stop being the same one.
    """
    if verdict.nothing_declared and not contract.ungraded:
        return False
    if not (verdict.passed and not verdict.not_run):
        return False
    # And the floor, when a consumer set one. `__post_init__` already refused a
    # contract that asks for a rung it cannot reach, so reaching here with too
    # low a level means a check that was declared did not deliver it -- a prose
    # gate that found nothing to read, a grounding block with no claims.
    return verdict.level >= contract.requires_level
