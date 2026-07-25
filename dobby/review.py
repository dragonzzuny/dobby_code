"""Code review built on QA/QC process evidence, not on a generic checklist.

Perspective-based reading, not checklist-based reading
-----------------------------------------------------
Controlled inspection experiments compare two strategies. **Checklist-based
reading (CBR)** gives every reviewer the same list of questions. **Perspective-
based reading (PBR)** gives each reviewer a distinct role and asks them to read
the artifact only as that role would. PBR reports lower cost per defect and less
inspection time at comparable or better detection rates, and reviewers spend less
time per defect found.

The mechanism is the same one `swarm/protocols.py` relies on: N reviewers with one
checklist find overlapping defects, because they read in the same order and their
attention is drawn to the same places. N reviewers with N perspectives partition
the space. So PBR is primary here, and a checklist is retained only for the
mechanical items that no perspective naturally owns (a licence header, a missing
newline) — the class of thing a checklist is genuinely good at.

QA and QC are separated
-----------------------
Quality **assurance** is process: the standards, gates, and reviews that keep
defects from being introduced. Quality **control** is inspection of outputs:
running the tests, validating the build. A programme needs both, and merging them
produces the common failure where a green CI run is reported as evidence of
quality. `qa_findings` (was the process followed?) and `qc_findings` (does the
output actually behave?) are therefore computed and reported separately.

Severity is priced, not guessed
-------------------------------
Published containment data gives defect cost a shape: a defect found during
development costs roughly 6× less than one found in production, and roughly 100×
less than one that causes a live incident. Those ratios are used directly as the
weights in `priority_score`, which is what lets the review order findings by
expected cost avoided rather than by how alarming each one sounds.

Defect taxonomy
---------------
Two families, because they need different responses:

- **Functional** — logic, interface, timing, resource. The code does the wrong
  thing. Blocks a merge.
- **Evolvability** — structure, naming, duplication, documentation. The code does
  the right thing and will be expensive to change. Does not block a merge, and
  suppressing it entirely is how a codebase becomes unmaintainable.

Mixing the two in one ranked list is the most common way review feedback becomes
noise: a naming nit listed beside a race condition trains the author to skim.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

# --------------------------------------------------------------------------
# Taxonomy
# --------------------------------------------------------------------------

FUNCTIONAL = "functional"
EVOLVABILITY = "evolvability"

#: Defect types under each family. Kept flat and short: a taxonomy nobody can
#: hold in their head gets collapsed to "other" in practice.
DEFECT_TYPES: dict[str, tuple[str, ...]] = {
    FUNCTIONAL: ("logic", "interface", "timing", "resource", "security",
                 "data", "error_handling", "compatibility"),
    EVOLVABILITY: ("structure", "naming", "duplication", "documentation",
                   "testability", "complexity"),
}

#: Where a defect can be caught, cheapest first, with the RELATIVE cost of
#: catching it there. Anchored on the published containment ratios: development
#: is the baseline, production is ~6x, a live incident is ~100x.
CONTAINMENT_COST: dict[str, float] = {
    "authoring": 0.5,
    "review": 1.0,
    "ci": 2.0,
    "staging": 4.0,
    "production": 6.0,
    "incident": 100.0,
}

#: Severity = impact on function if the defect ships. Distinct from priority,
#: which is urgency; conflating them is why "everything is critical" happens.
SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 1.0,    # data loss, security breach, or total failure
    "major": 0.6,       # a documented capability is wrong or unavailable
    "minor": 0.25,      # wrong in a recoverable, work-aroundable way
    "cosmetic": 0.05,   # visible but harmless
}

#: ISO/IEC 25010 product-quality characteristics, used as reviewer perspectives.
#: Using a published characteristic set rather than invented roles means the
#: perspectives partition quality along axes someone already argued about, and
#: gives a review a defensible claim to coverage.
PERSPECTIVES: dict[str, dict] = {
    "functional_suitability": {
        "reads_as": "a user exercising the documented behaviour",
        "hunts": FUNCTIONAL,
        "question": "Find one input or state where the code does not do what its "
                    "name, docstring, or ticket says. Give the input and the "
                    "wrong result.",
    },
    "reliability": {
        "reads_as": "an on-call engineer at 3am",
        "hunts": FUNCTIONAL,
        "question": "Find the failure that has no handler, no retry, and no log. "
                    "What does the operator see when it happens?",
    },
    "security": {
        "reads_as": "an attacker with access to the inputs",
        "hunts": FUNCTIONAL,
        "question": "Trace untrusted input to a privileged action. Name the "
                    "entry point, the sink, and the missing check.",
    },
    "performance_efficiency": {
        "reads_as": "the system at 100x current load",
        "hunts": FUNCTIONAL,
        "question": "Find the operation whose cost grows fastest with input "
                    "size. State the growth rate and the input that triggers it.",
    },
    "compatibility": {
        "reads_as": "an existing consumer that was not changed",
        "hunts": FUNCTIONAL,
        "question": "Find a caller, platform, encoding, or config that this "
                    "change breaks without telling anyone.",
    },
    "maintainability": {
        "reads_as": "the person changing this in six months",
        "hunts": EVOLVABILITY,
        "question": "Find the part that cannot be modified safely without "
                    "reading the whole file. Name what would have to be "
                    "understood first.",
    },
    "testability": {
        "reads_as": "someone writing the test that would have caught this",
        "hunts": EVOLVABILITY,
        "question": "Name the behaviour introduced here that no test can "
                    "observe, and what would have to change to make it "
                    "observable.",
    },
}

#: The residue a checklist genuinely handles: mechanical, binary, and owned by no
#: perspective. Deliberately short — a long checklist reintroduces the CBR cost.
MECHANICAL_CHECKLIST: tuple[str, ...] = (
    "new files carry the project's licence/header convention",
    "no debugging leftovers (print/console.log/breakpoint/TODO-without-issue)",
    "no committed secrets, tokens, or absolute local paths",
    "generated files were regenerated, not hand-edited",
    "public API changes are reflected in the docs that describe them",
    "no test marked skip/only without a linked reason",
)


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Finding:
    """One review finding, classified so it can be priced and ordered."""

    title: str
    file: str
    line: int | None
    family: str                  # FUNCTIONAL | EVOLVABILITY
    defect_type: str
    severity: str
    perspective: str = ""
    #: The concrete input/state → wrong output. A finding without this is an
    #: opinion; the field is required for a functional finding to be actionable.
    failure_scenario: str = ""
    #: Where this would otherwise have been caught. Drives the cost model: a
    #: defect that CI would catch anyway is worth less attention in review than
    #: one that only shows up as an incident.
    would_escape_to: str = "production"
    evidence: str = ""

    def __post_init__(self) -> None:
        if self.family not in DEFECT_TYPES:
            raise ValueError(f"unknown family {self.family!r}; "
                             f"expected one of {list(DEFECT_TYPES)}")
        if self.defect_type not in DEFECT_TYPES[self.family]:
            raise ValueError(
                f"{self.defect_type!r} is not a {self.family} defect type; "
                f"expected one of {DEFECT_TYPES[self.family]}")
        if self.severity not in SEVERITY_WEIGHT:
            raise ValueError(f"unknown severity {self.severity!r}; "
                             f"expected one of {list(SEVERITY_WEIGHT)}")
        if self.would_escape_to not in CONTAINMENT_COST:
            raise ValueError(
                f"unknown escape stage {self.would_escape_to!r}; "
                f"expected one of {list(CONTAINMENT_COST)}")

    def priority_score(self) -> float:
        """Expected cost avoided by fixing this now.

        `severity × containment_cost`. This is the number the review orders by,
        and it deliberately does NOT include the reviewer's confidence or the
        fix's difficulty: both are properties of the response, not of the defect,
        and folding them in is how genuinely severe findings get deprioritized for
        being inconvenient.
        """
        return round(SEVERITY_WEIGHT[self.severity]
                     * CONTAINMENT_COST[self.would_escape_to], 3)

    def blocks_merge(self) -> bool:
        """Functional defects at major or above block; evolvability never does.

        Evolvability findings are recorded and never block, because blocking on
        them trains authors to argue with the reviewer instead of fixing the
        logic error two lines down.
        """
        return (self.family == FUNCTIONAL
                and self.severity in ("critical", "major"))

    def actionable(self) -> tuple[bool, str]:
        """Whether this finding can be acted on as written."""
        if self.family == FUNCTIONAL and not self.failure_scenario.strip():
            return False, ("a functional finding without a concrete "
                           "input→wrong-output scenario cannot be reproduced, "
                           "so it cannot be fixed or refuted")
        if not self.file.strip():
            return False, "no file named: the author cannot locate it"
        return True, "has a location and a reproducible scenario"

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["priority_score"] = self.priority_score()
        d["blocks_merge"] = self.blocks_merge()
        actionable, why = self.actionable()
        d["actionable"] = actionable
        d["actionable_note"] = why
        return d


# --------------------------------------------------------------------------
# Review planning (PBR assignment)
# --------------------------------------------------------------------------

def assign_perspectives(reviewer_count: int, *,
                        risk_areas: Sequence[str] = ()) -> list[dict]:
    """Assign distinct ISO-25010 perspectives to reviewers, risk-first.

    `risk_areas` is how risk-based testing enters: named perspectives are
    assigned FIRST so the highest-risk axis is guaranteed a dedicated reader even
    when there are fewer reviewers than perspectives. Without this, a small panel
    silently drops whichever perspectives sort last, and the dropped one is as
    likely to be `security` as `naming`.
    """
    if reviewer_count <= 0:
        return []
    prioritized = [p for p in risk_areas if p in PERSPECTIVES]
    rest = [p for p in PERSPECTIVES if p not in prioritized]
    ordered = prioritized + rest
    assigned = []
    for i in range(reviewer_count):
        name = ordered[i % len(ordered)]
        spec = PERSPECTIVES[name]
        assigned.append({
            "reviewer": i,
            "perspective": name,
            "reads_as": spec["reads_as"],
            "hunts": spec["hunts"],
            "instruction": spec["question"],
            "prioritized_by_risk": name in prioritized,
            "duplicate_of_earlier": i >= len(ordered),
        })
    return assigned


def review_plan(reviewer_count: int, *, risk_areas: Sequence[str] = (),
                include_checklist: bool = True) -> dict:
    """A complete PBR plan, stating its own coverage gaps.

    Coverage is reported as the perspectives NOT covered, because that is the
    number a merge decision needs. A review that covered two of seven
    perspectives is a valid review with a stated scope; the same review reported
    as "reviewed" is a misrepresentation.
    """
    assignments = assign_perspectives(reviewer_count, risk_areas=risk_areas)
    covered = {a["perspective"] for a in assignments}
    uncovered = [p for p in PERSPECTIVES if p not in covered]
    unknown_risk = [p for p in risk_areas if p not in PERSPECTIVES]
    return {
        "strategy": "perspective_based_reading",
        "rationale": ("PBR reports lower cost per defect and less inspection "
                      "time than checklist-based reading; distinct perspectives "
                      "partition the search space instead of overlapping it"),
        "reviewer_count": reviewer_count,
        "assignments": assignments,
        "perspectives_covered": sorted(covered),
        "perspectives_uncovered": uncovered,
        "mechanical_checklist": list(MECHANICAL_CHECKLIST) if include_checklist
                                else [],
        "checklist_rationale": ("retained only for binary, mechanical items no "
                                "perspective owns — a long checklist "
                                "reintroduces the CBR cost it exists to avoid"),
        "unknown_risk_areas": unknown_risk,
        "coverage_note": (
            f"{len(covered)}/{len(PERSPECTIVES)} perspectives covered; "
            f"UNCOVERED: {uncovered}. Report this scope with the verdict — a "
            "partial review is valid, calling it complete is not"
            if uncovered else
            f"all {len(PERSPECTIVES)} perspectives covered"),
    }


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

def qa_findings(process: dict) -> list[dict]:
    """Quality ASSURANCE: was the process that prevents defects followed?

    Checks the process, not the code. A change can be defect-free and still fail
    here — for example by shipping new logic with no test, which is a defect that
    has not happened yet.
    """
    out = []

    def fail(check: str, detail: str, fix: str) -> None:
        out.append({"kind": "qa", "check": check, "ok": False,
                    "detail": detail, "fix": fix})

    if process.get("new_logic") and not process.get("new_tests"):
        fail("tests_for_new_logic",
             "new business logic with no accompanying test",
             "add a test that fails without this change — the shift-left gate")
    if not process.get("requirements_linked"):
        fail("traceability",
             "the change is not linked to a requirement, issue, or ledger row",
             "link it; an unlinked change cannot be verified against intent")
    if process.get("touched_protected_paths"):
        fail("protected_paths",
             f"touched protected paths: {process['touched_protected_paths']}",
             "revert those files and escalate with a named restore path")
    if process.get("generated_files_edited"):
        fail("generated_files",
             f"hand-edited generated files: {process['generated_files_edited']}",
             "fix the source and regenerate")
    if process.get("scope_files") and process.get("changed_files"):
        outside = sorted(set(process["changed_files"])
                         - set(process["scope_files"]))
        if outside:
            fail("scope_discipline",
                 f"changed files outside the declared scope: {outside}",
                 "revert them and report as findings instead")
    return out


def qc_findings(outputs: dict) -> list[dict]:
    """Quality CONTROL: does the output actually behave?

    Deliberately treats "no checks were run" as a failure rather than a pass. An
    unmeasured output is not a passing output, and reporting it as one is the
    exact failure the QA/QC separation exists to prevent.
    """
    out = []

    def fail(check: str, detail: str, fix: str) -> None:
        out.append({"kind": "qc", "check": check, "ok": False,
                    "detail": detail, "fix": fix})

    ran = outputs.get("checks_run") or []
    if not ran:
        fail("checks_executed",
             "no output-side check was run: the change is unverified",
             "run the project's test/lint/build commands and record the verdicts")
    for check in ran:
        if check.get("exit_code") not in (0, None):
            fail(f"check:{check.get('name', '?')}",
                 f"exited {check['exit_code']}",
                 "fix the failure; never soften a FAIL to 'mostly fine'")
    if outputs.get("produced_artifacts") and not outputs.get("artifacts_validated"):
        fail("output_validation",
             "artifacts were produced but never independently validated",
             "validate the OUTPUT — a producing command exiting 0 is not "
             "validation")
    return out


def verdict(findings: Sequence[Finding], *, plan: dict | None = None,
            qa: Sequence[dict] = (), qc: Sequence[dict] = ()) -> dict:
    """Merge findings, process checks, and output checks into one decision.

    Ordering is by `priority_score` (severity × containment cost), and the two
    defect families are reported in SEPARATE lists so a naming nit never appears
    beside a race condition.
    """
    functional = sorted((f for f in findings if f.family == FUNCTIONAL),
                        key=lambda f: -f.priority_score())
    evolvability = sorted((f for f in findings if f.family == EVOLVABILITY),
                          key=lambda f: -f.priority_score())
    blockers = [f for f in findings if f.blocks_merge()]
    unactionable = [f for f in findings if not f.actionable()[0]]
    qa_fails = [c for c in qa if not c["ok"]]
    qc_fails = [c for c in qc if not c["ok"]]

    if blockers or qc_fails:
        decision = "REQUEST_CHANGES"
    elif qa_fails:
        decision = "APPROVE_WITH_PROCESS_GAPS"
    else:
        decision = "APPROVE"

    reasons = []
    if blockers:
        reasons.append(f"{len(blockers)} functional blocker(s): "
                       + ", ".join(f"{f.file}:{f.line} {f.title}"
                                   for f in blockers[:4]))
    if qc_fails:
        reasons.append(f"{len(qc_fails)} output check(s) failed: "
                       + ", ".join(c["check"] for c in qc_fails[:4]))
    if qa_fails:
        reasons.append(f"{len(qa_fails)} process gap(s): "
                       + ", ".join(c["check"] for c in qa_fails[:4]))
    if unactionable:
        reasons.append(f"{len(unactionable)} finding(s) are not actionable as "
                       "written and were NOT counted toward the decision")

    return {
        "decision": decision,
        "reasons": reasons or ["no blockers, no failed output checks, "
                               "no process gaps"],
        "functional": [f.to_dict() for f in functional],
        "evolvability": [f.to_dict() for f in evolvability],
        "blockers": [f.to_dict() for f in blockers],
        "unactionable": [f.to_dict() for f in unactionable],
        "qa_checks_failed": qa_fails,
        "qc_checks_failed": qc_fails,
        "total_priority_at_risk": round(
            sum(f.priority_score() for f in findings), 3),
        "coverage": plan.get("coverage_note") if plan else
                    "no review plan recorded: perspective coverage is unknown",
        "scope_caveat": ("this verdict covers only the perspectives in the plan; "
                         "uncovered perspectives are unreviewed, not clean"),
    }


def escape_metrics(*, found_in_review: int, found_in_ci: int,
                   found_in_production: int) -> dict:
    """Defect containment metrics — the QA dashboard numbers that matter.

    Defect escape rate (share of defects that got past review) and the implied
    cost multiple are reported together, because the rate alone understates the
    problem: escaping five cosmetic defects and one incident-causing defect are
    the same rate and very different outcomes.
    """
    total = found_in_review + found_in_ci + found_in_production
    if total == 0:
        return {"total": 0,
                "note": "no defects recorded; with no data an escape rate of 0 "
                        "would be indistinguishable from perfect containment"}
    escaped = found_in_ci + found_in_production
    cost = (found_in_review * CONTAINMENT_COST["review"]
            + found_in_ci * CONTAINMENT_COST["ci"]
            + found_in_production * CONTAINMENT_COST["production"])
    ideal = total * CONTAINMENT_COST["review"]
    return {
        "total": total,
        "found_in_review": found_in_review,
        "found_in_ci": found_in_ci,
        "found_in_production": found_in_production,
        "escape_rate": round(escaped / total, 4),
        "production_escape_rate": round(found_in_production / total, 4),
        "relative_cost": round(cost, 2),
        "cost_if_all_caught_in_review": round(ideal, 2),
        "cost_multiple": round(cost / ideal, 2) if ideal else None,
        "note": (f"{escaped}/{total} defects escaped review, costing "
                 f"{round(cost / ideal, 2) if ideal else '?'}x the "
                 "catch-in-review baseline"),
    }
