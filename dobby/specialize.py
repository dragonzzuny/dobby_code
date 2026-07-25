"""Specialization: become a domain expert without losing general competence.

The requirement
---------------
A harness dropped into a new repository must be useful immediately (generalist),
and must get measurably better at THAT domain as sessions accumulate
(specialist). Those two goals conflict in a specific, well-known way: the cheapest
route to domain performance is to overfit the retrieval weights, policies, and
skills to the domain's vocabulary, and an overfitted harness is worse than the
generic one on the next project — and often on the same project's next unusual
task.

The mechanism: a dual gate
--------------------------
Every specialization step must clear TWO measurements at once:

1. **Domain gain** — the project's own gold set improves.
2. **Generic no-regression** — the kit's shipped generic gold set does not get
   worse, per case, not on average.

The per-case rule on the generic side is what makes this work. An average can
hide a trade: five generic cases improving by a little while two collapse reads
as progress on the mean and is a real loss of general competence. So the generic
side is checked case by case and any regression rejects the step.

This mirrors the federation-regression rule the kit already applies to
cross-project harvesting, applied inward to a single project's own drift.

Mastery is measured, not asserted
---------------------------------
`MasteryLevel` is derived from counted evidence — how much domain knowledge is
verified, how many promotions survived their gate, how the domain gold scores —
never from elapsed sessions. A harness that has run fifty sessions and learned
nothing is not an expert, and a level that rises with time alone would say it was.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Callable, Sequence

#: Mastery bands, lowest first. Each band names what the harness has EARNED the
#: right to do, because that is the operational consequence of the level.
LEVELS: tuple[str, ...] = ("novice", "oriented", "competent", "proficient", "expert")

LEVEL_LICENCE: dict[str, str] = {
    "novice": "generic policies only; ask before any domain-specific assumption",
    "oriented": "may cite the project's own conventions; still verifies each one",
    "competent": "may route by domain vocabulary; may propose domain skills as "
                 "candidates",
    "proficient": "may promote domain skills to active after the dual gate; may "
                  "tune retrieval weights within the no-regression bound",
    "expert": "may lead with domain judgment and shortcut generic exploration, "
              "and must still report the evidence for each shortcut",
}


@dataclasses.dataclass
class MasteryEvidence:
    """Counted facts about what has actually been learned in this project."""

    verified_domain_nodes: int = 0
    unverified_domain_nodes: int = 0
    promoted_candidates: int = 0
    rejected_candidates: int = 0
    domain_gold_cases: int = 0
    domain_gold_score: float = 0.0
    generic_gold_score: float = 0.0
    sessions: int = 0

    def verified_ratio(self) -> float:
        total = self.verified_domain_nodes + self.unverified_domain_nodes
        return round(self.verified_domain_nodes / total, 4) if total else 0.0

    def promotion_precision(self) -> float:
        """Share of proposals that survived their gate.

        Low precision with many promotions is the overfitting signature: the loop
        is generating candidates that pass a weak local check. It is reported
        alongside the count so a high promotion count cannot be read as skill on
        its own.
        """
        total = self.promoted_candidates + self.rejected_candidates
        return round(self.promoted_candidates / total, 4) if total else 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self) | {
            "verified_ratio": self.verified_ratio(),
            "promotion_precision": self.promotion_precision(),
        }


def mastery_level(ev: MasteryEvidence) -> tuple[str, str]:
    """Derive the band and state exactly which evidence set it.

    Thresholds are gates in sequence, so a level cannot be reached by excelling
    on one axis: an expert needs verified knowledge AND a scored domain gold AND
    surviving promotions. The reason string is returned so a report never has to
    say "level: expert" without saying why.
    """
    if ev.domain_gold_cases == 0:
        return ("novice",
                "no domain gold authored: nothing measures whether domain "
                "knowledge helps, so no level above novice is claimable "
                "(run the author-evals skill)")
    if ev.verified_domain_nodes < 5:
        return ("oriented",
                f"only {ev.verified_domain_nodes} verified domain nodes "
                "(<5): the project is mapped but not confirmed")
    if ev.verified_ratio() < 0.5:
        return ("oriented",
                f"verified ratio {ev.verified_ratio()} < 0.5: most domain "
                "knowledge is still model assertion, not observation")
    if ev.domain_gold_score < 0.5:
        return ("competent",
                f"domain gold {ev.domain_gold_score} < 0.5: retrieval finds the "
                "right knowledge less than half the time")
    if ev.promoted_candidates < 3:
        return ("competent",
                f"only {ev.promoted_candidates} promotions have survived the "
                "dual gate (<3): improvements are not yet reproducible")
    if ev.domain_gold_score < 0.75 or ev.promotion_precision() < 0.3:
        return ("proficient",
                f"domain gold {ev.domain_gold_score}, promotion precision "
                f"{ev.promotion_precision()}: strong but not yet consistently so")
    return ("expert",
            f"domain gold {ev.domain_gold_score} with "
            f"{ev.verified_domain_nodes} verified nodes and "
            f"{ev.promoted_candidates} gated promotions")


# --------------------------------------------------------------------------
# The dual gate
# --------------------------------------------------------------------------

@dataclasses.dataclass
class GateResult:
    """Outcome of one specialization step, with both sides of the gate shown."""

    accepted: bool
    domain_before: float
    domain_after: float
    domain_gain: float
    generic_before: dict[str, float]
    generic_after: dict[str, float]
    generic_regressions: list[dict]
    reason: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def dual_gate(*, domain_fitness: Callable[[], float],
              generic_per_case: Callable[[], dict[str, float]],
              apply: Callable[[], None],
              rollback: Callable[[], None],
              min_domain_gain: float = 0.005,
              regression_tolerance: float = 0.0) -> GateResult:
    """Apply a specialization step only if it helps the domain and harms nothing.

    The callables are injected rather than imported so this gate is testable
    without a knowledge graph, and so it can wrap any kind of change — retrieval
    weights, a new policy, a promoted skill — without knowing what the change was.

    `rollback` is invoked on ANY rejection, including an exception raised while
    measuring. A step that fails halfway must not leave the harness in a state
    that is neither the old one nor the new one; that state is unmeasurable and
    therefore unrecoverable without a snapshot.

    `regression_tolerance` defaults to 0.0 — strictly no per-case generic loss.
    A nonzero tolerance is offered because floating-point scoring can jitter, but
    it must be set deliberately: any positive value permits trading general
    competence for domain performance, which is the failure this gate exists to
    prevent.
    """
    domain_before = domain_fitness()
    generic_before = dict(generic_per_case())

    try:
        apply()
    except Exception as exc:  # noqa: BLE001
        rollback()
        return GateResult(
            accepted=False, domain_before=domain_before,
            domain_after=domain_before, domain_gain=0.0,
            generic_before=generic_before, generic_after=generic_before,
            generic_regressions=[],
            reason=f"apply() raised {type(exc).__name__}: {exc} — rolled back")

    try:
        domain_after = domain_fitness()
        generic_after = dict(generic_per_case())
    except Exception as exc:  # noqa: BLE001
        rollback()
        return GateResult(
            accepted=False, domain_before=domain_before,
            domain_after=domain_before, domain_gain=0.0,
            generic_before=generic_before, generic_after=generic_before,
            generic_regressions=[],
            reason=f"measurement raised {type(exc).__name__}: {exc} — rolled back")

    regressions = []
    for case, before in generic_before.items():
        after = generic_after.get(case)
        if after is None:
            # A generic case that stopped being scorable is itself a regression:
            # the harness lost the ability to answer something it could answer.
            regressions.append({"case": case, "before": round(before, 4),
                                "after": None,
                                "note": "case no longer scorable"})
        elif before - after > regression_tolerance:
            regressions.append({"case": case, "before": round(before, 4),
                                "after": round(after, 4),
                                "delta": round(after - before, 4)})

    gain = domain_after - domain_before

    if regressions:
        rollback()
        names = ", ".join(str(r["case"]) for r in regressions[:4])
        return GateResult(
            accepted=False, domain_before=round(domain_before, 4),
            domain_after=round(domain_after, 4), domain_gain=round(gain, 4),
            generic_before=generic_before, generic_after=generic_after,
            generic_regressions=regressions,
            reason=(f"REJECTED: {len(regressions)} generic case(s) regressed "
                    f"({names}). Domain gain {gain:+.4f} does not license a loss "
                    "of general competence"))

    if gain < min_domain_gain:
        rollback()
        return GateResult(
            accepted=False, domain_before=round(domain_before, 4),
            domain_after=round(domain_after, 4), domain_gain=round(gain, 4),
            generic_before=generic_before, generic_after=generic_after,
            generic_regressions=[],
            reason=(f"REJECTED: domain gain {gain:+.4f} below the "
                    f"{min_domain_gain} threshold — an unmeasurable improvement "
                    "still adds maintenance cost and drift risk"))

    return GateResult(
        accepted=True, domain_before=round(domain_before, 4),
        domain_after=round(domain_after, 4), domain_gain=round(gain, 4),
        generic_before=generic_before, generic_after=generic_after,
        generic_regressions=[],
        reason=(f"ACCEPTED: domain {domain_before:.4f} → {domain_after:.4f} "
                f"({gain:+.4f}) with zero per-case generic regression"))


# --------------------------------------------------------------------------
# Persistent specialization record
# --------------------------------------------------------------------------

class SpecializationLedger:
    """Append-only history of specialization steps, plus current mastery.

    Kept as a committed artifact because it is the answer to "why does this
    harness behave differently in this repo than in a fresh one?" A harness whose
    behaviour has drifted with no record of the drift cannot be reviewed, and
    cannot be reverted to a known-good point.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {"domain": None, "created": time.strftime("%Y-%m-%d"),
                    "steps": [], "evidence": MasteryEvidence().to_dict()}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.path)
        return self.path

    def record(self, kind: str, result: GateResult, detail: dict | None = None
               ) -> dict:
        """Log a step — accepted OR rejected.

        Rejections are recorded deliberately: they are the negative memory that
        stops the improvement loop from re-proposing the same losing change every
        session, which is otherwise the default behaviour of a stateless loop.
        """
        step = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "accepted": result.accepted,
            "domain_gain": result.domain_gain,
            "generic_regressions": len(result.generic_regressions),
            "reason": result.reason,
            "detail": detail or {},
        }
        self.data.setdefault("steps", []).append(step)
        return step

    def already_rejected(self, kind: str, detail: dict) -> dict | None:
        """Whether this exact change was tried and rejected before."""
        for step in reversed(self.data.get("steps", [])):
            if (not step["accepted"] and step["kind"] == kind
                    and step.get("detail") == detail):
                return step
        return None

    def set_evidence(self, ev: MasteryEvidence) -> dict:
        self.data["evidence"] = ev.to_dict()
        level, reason = mastery_level(ev)
        self.data["level"] = level
        self.data["level_reason"] = reason
        self.data["licence"] = LEVEL_LICENCE[level]
        return {"level": level, "reason": reason,
                "licence": LEVEL_LICENCE[level]}

    def summary(self) -> dict:
        steps = self.data.get("steps", [])
        accepted = [s for s in steps if s["accepted"]]
        return {
            "domain": self.data.get("domain"),
            "level": self.data.get("level", "novice"),
            "level_reason": self.data.get("level_reason",
                                          "no evidence recorded yet"),
            "licence": self.data.get("licence", LEVEL_LICENCE["novice"]),
            "steps_total": len(steps),
            "steps_accepted": len(accepted),
            "steps_rejected": len(steps) - len(accepted),
            "cumulative_domain_gain": round(
                sum(s["domain_gain"] for s in accepted), 4),
            "evidence": self.data.get("evidence", {}),
            "path": self.path,
        }
