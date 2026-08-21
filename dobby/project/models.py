"""The five objects a project needs so a new session does not start from zero.

The runtime made one RUN durable. That is the wrong unit for work that outlives
an afternoon: a run ends, and the next session opens a repository it has never
seen, re-derives what the test command is, re-decides what matters, and often
re-implements something that was finished on Tuesday.

These five fix the unit. A `ProjectManifest` says what the project IS and how it
is checked. A `Baseline` says whether it is currently sound. A `Portfolio` of
`WorkItem`s says what remains and what "done" means for each. A
`SessionEnvelope` is the minimum a fresh worker needs to act correctly without
reading a transcript.

Structured, not prose
---------------------
All of it is JSON. A markdown progress file is the obvious choice and it is the
wrong one: the agent that edits it can also summarise it, and summarising an
acceptance criterion silently changes the definition of done. Free text is for
the human-readable handoff that `Trajectory.handoff` already writes; this is the
machine's copy, and the machine's copy is the one the next session obeys.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict

# -- work item states --------------------------------------------------------
OPEN = "OPEN"
READY = "READY"
IN_PROGRESS = "IN_PROGRESS"
VERIFYING = "VERIFYING"
DONE = "DONE"
BLOCKED = "BLOCKED"
NEEDS_REPLAN = "NEEDS_REPLAN"
CANCELLED = "CANCELLED"

WORK_ITEM_STATES = (OPEN, READY, IN_PROGRESS, VERIFYING, DONE, BLOCKED,
                    NEEDS_REPLAN, CANCELLED)

#: States a selector may hand to a worker. `DONE` is absent by construction,
#: which is invariant PK-3: finished work is not selectable again without an
#: explicit reopen.
SELECTABLE = (OPEN, READY, IN_PROGRESS)

#: Terminal for the purposes of "is there work left".
CLOSED_STATES = {DONE, CANCELLED}

#: Harness annotations appended to an item's outcome — repair directives from
#: `reattempt.py`, replan notes, anything this system writes to itself. Every
#: marker that gets appended there starts with this prefix.
#:
#: Everything from the first occurrence is EXCLUDED from
#: `architect_contract_digest`. Including it meant the loop's own note to itself
#: changed what counted as a different question: a failure that had not changed
#: at all looked new because the harness had annotated the item about it, and the
#: architect was paid to answer the same question twice. The item's contract is
#: what a PERSON stated the outcome to be; the annotations are what happened.
ANNOTATION_PREFIX = "\n\n--- "

#: Above this, an item is not handed straight to an implementation worker. It
#: gets a discovery step or an architect decision first — the one judgement this
#: kernel does not make deterministically.
UNCERTAINTY_ESCALATION = 3


class ProjectError(ValueError):
    """A project object that cannot be trusted to mean what it says."""


def digest_of(payload) -> str:
    """sha256 over canonical JSON. Sorted keys, so equal content digests equal."""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                     default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass(frozen=True)
class ProjectManifest:
    """What this project is, and what counts as checking it.

    Frozen. A manifest that a worker can edit is a contract a worker can weaken,
    and the whole point of `manifest_digest` appearing in every envelope is that
    a change to the contract invalidates sessions built on the old one.
    """

    project_id: str
    root: str
    repo_digest: str
    stack: tuple = ()
    smoke_checks: tuple = ()
    capability_inventory: dict = field(default_factory=dict)
    policy_version: str = "1"
    created_at: str = ""

    def __post_init__(self):
        if not self.project_id:
            raise ProjectError("a manifest needs a project_id")
        if not self.smoke_checks:
            # Not fatal, and it is recorded rather than invented: a project
            # nobody can check is a project whose baseline is meaningless, and
            # saying so is more useful than a fabricated command.
            object.__setattr__(self, "smoke_checks", ())
        if not self.created_at:
            object.__setattr__(self, "created_at", _now())

    #: Fields that are NOT part of the contract, and why each is excluded.
    #:
    #: `created_at` — two initialisations of an unchanged repository describe
    #: the same project; a clock-dependent digest would invalidate every
    #: envelope on re-init.
    #: `repo_digest` — that is the state of the TREE, which moves constantly and
    #: is checked separately by the baseline. Folding it in here would collapse
    #: two signals that need different responses: "the code changed, re-run the
    #: checks" and "the definition of checking changed, everything before this
    #: is evidence about a different project".
    #: `capability_inventory` — that is the MACHINE. Installing a second agent
    #: CLI must not invalidate a baseline.
    _NOT_CONTRACT = ("created_at", "repo_digest", "capability_inventory")

    @property
    def manifest_digest(self) -> str:
        """Digest of the CONTRACT: what this project is and how it is checked."""
        payload = {k: v for k, v in asdict(self).items()
                   if k not in self._NOT_CONTRACT}
        return digest_of(payload)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["stack"] = list(self.stack)
        out["smoke_checks"] = list(self.smoke_checks)
        out["manifest_digest"] = self.manifest_digest
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "ProjectManifest":
        data = {k: v for k, v in (raw or {}).items()
                if k in cls.__dataclass_fields__}
        data["stack"] = tuple(data.get("stack") or ())
        data["smoke_checks"] = tuple(data.get("smoke_checks") or ())
        return cls(**data)


@dataclass
class WorkItem:
    """One unit of work with a definition of done that a machine can apply.

    `acceptance_checks` are commands, not descriptions. An item whose acceptance
    is a sentence is an item whose completion is an opinion, and the runtime
    already has a gate that runs commands — this is what feeds it.
    """

    work_item_id: str
    project_id: str
    title: str
    outcome: str = ""
    acceptance_checks: list = field(default_factory=list)
    depends_on: list = field(default_factory=list)
    priority: int = 0
    impact: int = 0
    uncertainty: int = 0
    state: str = OPEN
    evidence_refs: list = field(default_factory=list)
    latest_run_id: str | None = None
    blocked_reason: str = ""
    #: The plan that made this item gradeable, if an architect was asked.
    #: Recorded rather than inferred: without it, an item whose
    #: uncertainty was the reason for the call still reads as uncertain
    #: after the call, and the loop asks the same question forever.
    planned_by: str | None = None
    version: int = 1

    def __post_init__(self):
        if self.state not in WORK_ITEM_STATES:
            raise ProjectError(
                f"unknown work item state {self.state!r}; expected one of "
                f"{WORK_ITEM_STATES}")

    @property
    def needs_architect(self) -> bool:
        """Whether this should get a decision before it gets an implementation.

        High uncertainty, or an item with no machine-checkable acceptance. Both
        mean the same thing operationally: sending this to a worker now produces
        something nobody can grade.

        An applied plan clears the uncertainty gate and NOT the acceptance one.
        That asymmetry is the point: the architect's job was to make the item
        gradeable, so a plan that left it with no acceptance check did not do
        it, and no amount of planning may substitute for something that can be
        run.
        """
        if not self.acceptance_checks:
            return True
        if self.planned_by:
            return False
        return self.uncertainty >= UNCERTAINTY_ESCALATION

    @property
    def architect_contract_digest(self) -> str:
        """Everything about this item that changes what an architect is asked.

        `ArchitectureRequest` used to fold in uncertainty, acceptance checks and
        evidence refs, which is most of the gradeability question but not the
        whole prompt. `build_prompt` also shows the architect the TITLE and the
        OUTCOME, so a person rewriting the outcome — the ordinary way a vague
        item gets sharpened — changed what was being asked while the request
        digest stayed put, and the dedupe answered the new question with the old
        answer.

        `depends_on` is here for the same reason: an item that just gained a
        dependency is a different planning problem.

        `version` was here as a catch-all and has been REMOVED. It bumps on every
        write, including the ones that change nothing an architect is shown — a
        state transition, a run being attached, a repair directive the harness
        appended to itself. With it in, almost every item the loop touched became
        a new question, which defeats the dedupe it was meant to protect. A guard
        that makes the thing it guards useless is not a guard.

        What replaces it is a TEST rather than a runtime trick:
        `test_replan.py` reads `build_prompt` and asserts every `item.<field>` it
        references appears in the set below. A future field that reaches the
        prompt fails that test instead of silently falling outside identity.
        """
        return digest_of({
            "work_item_id": self.work_item_id,
            "title": self.title,
            "outcome": self.outcome.split(ANNOTATION_PREFIX)[0],
            "acceptance_checks": sorted(self.acceptance_checks),
            "depends_on": sorted(self.depends_on),
            "uncertainty": self.uncertainty,
            "evidence_refs": sorted(self.evidence_refs),
        })

    #: The fields `architect_contract_digest` covers. Named so a test can read
    #: it, rather than re-deriving the list and drifting from the digest.
    CONTRACT_FIELDS = ("work_item_id", "title", "outcome", "acceptance_checks",
                       "depends_on", "uncertainty", "evidence_refs")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkItem":
        return cls(**{k: v for k, v in (raw or {}).items()
                      if k in cls.__dataclass_fields__})


@dataclass
class Portfolio:
    """Every work item in one project, and the version that guards edits."""

    project_id: str
    version: int = 1
    items: list = field(default_factory=list)

    def by_id(self) -> dict:
        return {item.work_item_id: item for item in self.items}

    def get(self, work_item_id: str) -> WorkItem | None:
        return self.by_id().get(work_item_id)

    def remaining(self) -> list:
        return [i for i in self.items if i.state not in CLOSED_STATES]

    def coverage(self) -> dict:
        by_state: dict[str, int] = {}
        for item in self.items:
            by_state[item.state] = by_state.get(item.state, 0) + 1
        done = by_state.get(DONE, 0)
        return {"items": len(self.items), "by_state": by_state,
                "done": done,
                "remaining": len(self.remaining()),
                "fraction_done": round(done / len(self.items), 4)
                if self.items else None}

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "version": self.version,
                "items": [i.to_dict() for i in self.items],
                "coverage": self.coverage()}


@dataclass(frozen=True)
class Baseline:
    """Whether the project was sound the last time anybody checked.

    Recorded per (git sha, manifest digest). A baseline taken against a
    different commit is not evidence about this one, and treating it as though
    it were is how a session builds a feature on top of a broken tree.
    """

    project_id: str
    git_sha: str
    manifest_digest: str
    #: The working tree at the moment of measurement, including uncommitted
    #: edits. `git_sha` alone does not move when a file is edited and not
    #: committed, so a baseline keyed only on HEAD certifies code that has since
    #: changed — which is the failure this whole object exists to prevent.
    repo_digest: str = ""
    smoke_results: tuple = ()
    passed: bool = False
    created_at: str = ""
    note: str = ""

    def __post_init__(self):
        if not self.created_at:
            object.__setattr__(self, "created_at", _now())

    def matches(self, git_sha: str, manifest_digest: str,
                repo_digest: str | None = None) -> bool:
        """Whether this baseline is evidence about the state described.

        `repo_digest` is compared only when both sides have one, so a baseline
        recorded before that field existed still answers the question it could
        answer rather than failing closed on everything.
        """
        if self.git_sha != git_sha or self.manifest_digest != manifest_digest:
            return False
        if repo_digest and self.repo_digest:
            return self.repo_digest == repo_digest
        return True

    def staleness(self, git_sha: str, manifest_digest: str,
                  repo_digest: str | None = None) -> str:
        """Which of the three moved, in the words a session should report."""
        if self.manifest_digest != manifest_digest:
            return ("the manifest digest changed, so this baseline is evidence "
                    "about a different contract — a different stack or a "
                    "different definition of checking")
        if self.git_sha != git_sha:
            return (f"the tree is at {git_sha[:12]} and the baseline was taken "
                    f"at {self.git_sha[:12]}")
        if repo_digest and self.repo_digest and self.repo_digest != repo_digest:
            return ("the working tree has uncommitted changes since the "
                    "baseline, so its result describes code that no longer "
                    "exists here")
        return ""

    def to_dict(self) -> dict:
        out = asdict(self)
        out["smoke_results"] = list(self.smoke_results)
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Baseline":
        data = {k: v for k, v in (raw or {}).items()
                if k in cls.__dataclass_fields__}
        data["smoke_results"] = tuple(data.get("smoke_results") or ())
        return cls(**data)


@dataclass(frozen=True)
class SessionEnvelope:
    """The minimum a fresh worker needs, and nothing it would have to summarise.

    Deliberately not a transcript. A worker that reads the last session's
    conversation re-derives the last session's reasoning, including its mistakes.
    What it needs is narrower and harder: which contract is in force, whether the
    tree is sound, which item is active, what has already been verified, what is
    still broken, and the one next action.
    """

    session_id: str
    project_id: str
    portfolio_version: int
    manifest_digest: str
    baseline_git_sha: str
    active_work_item_id: str | None = None
    verified_artifact_ids: tuple = ()
    open_failures: tuple = ()
    unconfirmed_effects: tuple = ()
    next_action: str = ""
    #: An architecture request that was opened and never settled — the
    #: state a crash mid-call leaves. A fresh session must not start work
    #: on an item whose plan is still in flight.
    pending_request_digest: str | None = None
    needs_rebaseline: bool = False
    created_at: str = ""
    closed_at: str | None = None

    def __post_init__(self):
        if not self.created_at:
            object.__setattr__(self, "created_at", _now())

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("verified_artifact_ids", "open_failures",
                    "unconfirmed_effects"):
            out[key] = list(getattr(self, key))
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "SessionEnvelope":
        data = {k: v for k, v in (raw or {}).items()
                if k in cls.__dataclass_fields__}
        for key in ("verified_artifact_ids", "open_failures",
                    "unconfirmed_effects"):
            data[key] = tuple(data.get(key) or ())
        return cls(**data)
