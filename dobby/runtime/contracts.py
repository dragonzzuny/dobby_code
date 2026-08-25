"""Artifact contracts — what a node owes the node after it.

The default way to connect two agent steps is to paste the first one's prose
into the second one's prompt. It works in a demo and fails in a run, because
free text carries no way to tell a finished answer from a plausible one. The
second step cannot check what it was handed, so it proceeds on whatever arrived
and the failure surfaces three steps later as a wrong result rather than here as
a rejected handoff.

A contract makes the handoff checkable. Five fields, each answering a question
the next node would otherwise have to guess:

    input_refs         what this node was given, by id and digest — so the
                       attempt can be reproduced instead of re-imagined
    output_schema      the shape the output must have
    acceptance_checks  the commands that must pass for the output to count
    side_effect_class  what this node does to the world outside the run
    promotion_rule     when a produced artifact may be used by anything else

`PROPOSED -> VERIFIED -> PROMOTED` exists because those are three different
claims. Produced is what the worker returned. Verified is what the checks
accepted. Promoted is what later nodes may read. Collapsing them is how an
unchecked draft becomes an input.

The schema validator here is deliberately small — types, required keys, and
enums, no `$ref`, no remote schemas. The engine takes one dependency (PyYAML)
and this is not worth a second one; and a schema language nobody can hold in
their head produces contracts that are written once and never read.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict

# -- side effects -----------------------------------------------------------
#
# The class decides three separate things: whether approval is needed, whether
# the scheduler may run a second copy speculatively, and whether an idempotency
# key is mandatory. They are named rather than inferred, because the cost of
# inferring wrongly is duplicate money moving.
NONE = "NONE"
LOCAL_WRITE = "LOCAL_WRITE"
EXTERNAL_REVERSIBLE = "EXTERNAL_REVERSIBLE"
EXTERNAL_IRREVERSIBLE = "EXTERNAL_IRREVERSIBLE"

SIDE_EFFECT_CLASSES = (NONE, LOCAL_WRITE, EXTERNAL_REVERSIBLE,
                       EXTERNAL_IRREVERSIBLE)

#: Only a node that touches nothing outside the run may be raced against a
#: second copy of itself. Hedging a node with side effects sends the email twice.
HEDGEABLE = {NONE}

#: Nodes that must not run without a recorded human decision.
NEEDS_APPROVAL = {EXTERNAL_IRREVERSIBLE}

#: Nodes whose effects must carry an idempotency key, so a worker that dies
#: after acting but before recording does not act again on resume.
NEEDS_IDEMPOTENCY_KEY = {EXTERNAL_REVERSIBLE, EXTERNAL_IRREVERSIBLE}

# -- artifact lifecycle ------------------------------------------------------
PROPOSED = "PROPOSED"
VERIFIED = "VERIFIED"
PROMOTED = "PROMOTED"
REJECTED = "REJECTED"

ARTIFACT_STATES = (PROPOSED, VERIFIED, PROMOTED, REJECTED)

_ARTIFACT_TRANSITIONS = {
    PROPOSED: {VERIFIED, REJECTED},
    VERIFIED: {PROMOTED, REJECTED},
    PROMOTED: set(),
    REJECTED: set(),
}


def check_artifact_write(previous: str | None, to_state: str,
                        *, previous_digest: str = "",
                        digest_: str = "") -> None:
    """Whether a STORE may record `to_state`. Raises `ContractError` if not.

    `Artifact.transition` already refuses an illegal move, and it refuses it on
    an in-memory object. The store is a second door into the same state, and it
    was unlocked: `RunStore.put_artifact` took whatever `artifact.state` it was
    handed and wrote it. Demonstrated during an audit of this repository —

        put_artifact(state=PROMOTED)   ->  ['PROMOTED']
        put_artifact(state=REJECTED)   ->  ['REJECTED']

    on the same artifact id, while `_ARTIFACT_TRANSITIONS[PROMOTED]` is the
    empty set. The runner never does that, so nothing was broken; but
    `_promoted_inputs` reads the STORE, not the object, so the rule that decides
    what may become an input was being enforced in the one place that does not
    decide it.

    Two rules, and the second is the one that is easy to miss:

    1. A row appears as PROPOSED and moves only along the table. There is no
       other entry point, so PROMOTED cannot be conjured; it has to be walked
       to, and every step is checked here.
    2. A rewrite that keeps the state must keep the DIGEST. Otherwise the state
       machine holds and the payload underneath it is swapped — a gate that
       checks the label and not the contents is the same hole one level down.
    """
    if to_state not in ARTIFACT_STATES:
        raise ContractError(
            f"unknown artifact state {to_state!r}; expected one of "
            f"{ARTIFACT_STATES}")
    if previous is None:
        if to_state != PROPOSED:
            raise ContractError(
                f"an artifact enters the store as {PROPOSED}, not {to_state!r}. "
                f"A state is reached by transitioning to it, and this is the "
                f"door that check exists behind — see runtime/runner.py, which "
                f"records each step")
        return
    if previous == to_state:
        if digest_ and previous_digest and digest_ != previous_digest:
            raise ContractError(
                f"artifact rewritten in state {to_state} with a different "
                f"payload (digest {previous_digest[:12]} -> {digest_[:12]}); "
                f"the state machine would hold while the contents changed "
                f"underneath it")
        return
    if to_state in _ARTIFACT_TRANSITIONS.get(previous, set()):
        return
    allowed = sorted(_ARTIFACT_TRANSITIONS.get(previous, set()))
    raise ContractError(
        f"illegal artifact transition {previous} -> {to_state} at the "
        f"store; allowed: {allowed or 'none (terminal)'}")


class ContractError(ValueError):
    """A contract that cannot be satisfied by any output, or was violated."""


def digest(payload) -> str:
    """Content digest of an artifact payload.

    JSON with sorted keys, so two structurally identical payloads produced by
    different workers digest identically and the run can tell "the same answer"
    from "an answer that happens to read the same".
    """
    if isinstance(payload, (dict, list)):
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                         default=str).encode("utf-8")
    else:
        raw = str(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def idempotency_key(run_id: str, node_id: str, effect_version: str = "1") -> str:
    """The key that makes an external effect exactly-once across restarts.

    Deliberately derived from identity rather than from content: a retry that
    rewords the same email must still be recognised as the same effect. The
    `effect_version` is the escape hatch for the case where the effect really is
    a new one — bump it explicitly, never by accident.
    """
    return hashlib.sha256(
        f"{run_id}\x00{node_id}\x00{effect_version}".encode("utf-8")).hexdigest()


# -- the tiny schema validator ----------------------------------------------

_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list, "null": type(None),
}


def validate_schema(value, schema: dict, *, path: str = "$") -> list[str]:
    """Return a list of violations; empty means valid.

    Returns ALL violations rather than raising on the first, because the list is
    the repair instruction. Telling a worker one problem at a time turns a single
    repair round into four.
    """
    problems: list[str] = []
    if not schema:
        return problems

    expected = schema.get("type")
    if expected:
        wanted = _TYPES.get(expected)
        if wanted is None:
            raise ContractError(f"unknown schema type {expected!r} at {path}")
        # bool is an int in Python and almost never is one in a schema.
        if expected in ("number", "integer") and isinstance(value, bool):
            problems.append(f"{path}: expected {expected}, got boolean")
            return problems
        if not isinstance(value, wanted):
            problems.append(
                f"{path}: expected {expected}, got {type(value).__name__}")
            return problems

    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}.{key}: required, and missing")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems.extend(validate_schema(value[key], sub,
                                                path=f"{path}.{key}"))
    if isinstance(value, list):
        item_schema = schema.get("items")
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            problems.append(
                f"{path}: expected at least {min_items} item(s), got {len(value)}")
        if item_schema:
            for i, item in enumerate(value):
                problems.extend(validate_schema(item, item_schema,
                                                path=f"{path}[{i}]"))
    if isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < min_len:
            problems.append(
                f"{path}: expected at least {min_len} character(s), got {len(value)}")
    return problems


# -- the contract ------------------------------------------------------------

@dataclass
class ArtifactContract:
    """What a node must produce for its output to be usable.

    `acceptance_checks` are shell command templates run by the verifier. They are
    the deterministic layer and they outrank any model's opinion: a node whose
    tests fail is not finished however confidently it says it is.
    """

    output_schema: dict = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=list)
    side_effect_class: str = NONE
    #: Free-text rule, recorded in the report so a promotion can be argued with.
    #: The MACHINE rule is fixed and not configurable: schema clean AND every
    #: acceptance check passed. A promotion rule that a run could weaken at
    #: runtime is not a gate.
    promotion_rule: str = "schema satisfied and every acceptance check passed"
    #: Ids of the artifacts this node was given. Filled by the runner.
    input_refs: list[str] = field(default_factory=list)
    #: Bumped by hand when the same node id should be allowed to act again.
    effect_version: str = "1"
    #: Repo-relative paths a WRITING node says it will touch. Checked by
    #: `runtime/effects.py` before any acceptance check runs: a node that
    #: declared a side effect and left no trace of it did not do its work,
    #: whatever its output said. Empty means "check the tree instead", which is
    #: weaker and still fail-closed. Ignored entirely for non-writing classes.
    expected_paths: list[str] = field(default_factory=list)
    #: The grounded layer: does the quoted evidence exist, and does a
    #: recomputation agree with the reported number. See `verify.ground`.
    #:
    #:     {"claims_at": "claims",
    #:      "evidence_files": ["reports/measurements.md"],
    #:      "recompute": [{"field": "total", "command": "...", "tolerance": 0}]}
    grounding: dict = field(default_factory=dict)
    #: True when this node's output is a MODEL'S OPINION. Advisory artifacts are
    #: promoted like any other — they are a real product of a real step — but
    #: they are labelled everywhere they travel, and `.dobby/ontology.json`
    #: forbids a model assertion from counting as verification. A downstream
    #: node sees the label in its inputs and can decide; nothing lets an
    #: advisory verdict silently become the evidence that a gate passed.
    advisory: bool = False
    #: A dot path into the payload naming the field that carries PROSE a person
    #: will read, e.g. "summary". When set, the verifier runs
    #: `dobby/style.py`'s gate over it and refuses the artifact when the
    #: generated-prose signature is present.
    #:
    #: Named the same way `grounding.claims_at` names its field, and for the
    #: same reason: an acceptance check is a shell command and cannot see inside
    #: a payload, so a check about the payload's CONTENT has to be declared
    #: where the verifier can reach it.
    #:
    #: Why this exists at all: `style.py` had one caller in the whole
    #: repository — somebody typing `dobby style` — so a module written to keep
    #: generated writing out of a deliverable could describe it and never stop
    #: any.
    prose_at: str = ""
    #: This node is DELIBERATELY ungraded, and says so.
    #:
    #: Declaring nothing by accident and declaring that you grade nothing are
    #: different facts, and only the second is a decision. `runtime/bench.py`
    #: needs the second: its BASELINE arm exists to show what the gate is worth,
    #: and it can only do that by running without one. Refusing that arm would
    #: have deleted the experiment rather than fixed a bug.
    #:
    #: It is not an override of the gate. An ungraded artifact promotes and then
    #: travels LABELLED, the same way `advisory` does — a consumer is told that
    #: nothing checked this, instead of having to know. Setting it is a sentence
    #: someone has to write in a contract, which is the difference between a
    #: control condition and an oversight.
    ungraded: bool = False

    @property
    def declares_nothing(self) -> bool:
        """Whether this contract could refuse ANY output.

        No shape, no acceptance check, no side effect to observe, nothing to
        ground: there is no proposition here that a result could fail. A gate
        over such a contract passes vacuously, which reads in a report exactly
        like a gate that was satisfied.

        This is the same defect as `VerifierResult.not_run`, one step earlier.
        `not_run` catches a declared check that could not be executed;
        `declares_nothing` catches a check that was never declared. Both end as
        `all([])`, which is True, and both make an ungraded artifact
        indistinguishable from a graded one.

        A side effect COUNTS as a declaration. `runtime/effects.py` observes
        whether a LOCAL_WRITE node actually changed the tree, and a node that
        can be caught having done nothing is not a node making no claim. It is a
        weak claim, and weak is not vacuous.
        """
        return not (self.output_schema or self.acceptance_checks
                    or self.grounding
                    or self.side_effect_class != NONE)

    def __post_init__(self):
        if self.side_effect_class not in SIDE_EFFECT_CLASSES:
            raise ContractError(
                f"unknown side_effect_class {self.side_effect_class!r}; "
                f"expected one of {SIDE_EFFECT_CLASSES}")

    @property
    def needs_approval(self) -> bool:
        return self.side_effect_class in NEEDS_APPROVAL

    @property
    def needs_idempotency_key(self) -> bool:
        return self.side_effect_class in NEEDS_IDEMPOTENCY_KEY

    @property
    def hedgeable(self) -> bool:
        return self.side_effect_class in HEDGEABLE

    def check_shape(self, payload) -> list[str]:
        return validate_schema(payload, self.output_schema)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "ArtifactContract":
        return cls(**{k: v for k, v in (raw or {}).items()
                      if k in cls.__dataclass_fields__})


@dataclass
class Artifact:
    """A produced thing, its state, and the evidence for that state."""

    artifact_id: str
    run_id: str
    node_id: str
    kind: str
    payload: object
    state: str = PROPOSED
    digest_: str = ""
    #: Paths to files this artifact refers to (a patch, a report, a capture).
    paths: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    created: str = ""

    def __post_init__(self):
        if self.state not in ARTIFACT_STATES:
            raise ContractError(f"unknown artifact state {self.state!r}")
        if not self.digest_:
            self.digest_ = digest(self.payload)
        if not self.created:
            self.created = time.strftime("%Y-%m-%dT%H:%M:%S")

    def transition(self, to_state: str) -> "Artifact":
        allowed = _ARTIFACT_TRANSITIONS.get(self.state, set())
        if to_state not in allowed:
            raise ContractError(
                f"illegal artifact transition {self.state} -> {to_state}; "
                f"allowed: {sorted(allowed) or 'none (terminal)'}")
        self.state = to_state
        return self

    @property
    def usable(self) -> bool:
        """Whether a later node may read this. Only PROMOTED qualifies."""
        return self.state == PROMOTED

    def to_dict(self) -> dict:
        return {"artifact_id": self.artifact_id, "run_id": self.run_id,
                "node_id": self.node_id, "kind": self.kind, "state": self.state,
                "digest": self.digest_, "paths": list(self.paths),
                "evidence": dict(self.evidence), "created": self.created,
                "payload": self.payload}


# -- ready-made contracts ----------------------------------------------------
#
# Named because they are the shapes this harness already produces, and because a
# contract invented per run is a contract nobody validates twice.

PLAN_SCHEMA = {
    "type": "object",
    "required": ["steps"],
    "properties": {
        "steps": {"type": "array", "minItems": 1,
                  "items": {"type": "object",
                            "required": ["what"],
                            "properties": {"what": {"type": "string",
                                                    "minLength": 3},
                                           "why": {"type": "string"}}}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}

PATCHSET_SCHEMA = {
    "type": "object",
    "required": ["base_commit", "diff", "changed_files"],
    "properties": {
        "base_commit": {"type": "string", "minLength": 4},
        "diff": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
    },
}

RESEARCH_CLAIMS_SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {"type": "array", "minItems": 1,
                   "items": {"type": "object",
                             "required": ["claim", "evidence"],
                             "properties": {
                                 "claim": {"type": "string", "minLength": 3},
                                 "evidence": {"type": "array",
                                              "items": {"type": "string"}},
                                 "status": {"type": "string",
                                            "enum": ["supported", "unresolved",
                                                     "contradicted"]}}}},
    },
}

TEST_REPORT_SCHEMA = {
    "type": "object",
    "required": ["command", "exit_code"],
    "properties": {
        "command": {"type": "string", "minLength": 1},
        "exit_code": {"type": "integer"},
        "stdout_tail": {"type": "string"},
    },
}

REPORT_SCHEMA = {
    "type": "object",
    "required": ["summary"],
    "properties": {
        "summary": {"type": "string", "minLength": 3},
        "not_established": {"type": "array", "items": {"type": "string"}},
    },
}

#: What `AdvisoryJudgeWorker` returns. A judge's output is an OPINION and the
#: contract says so with `advisory=True`, but an opinion still has a shape: it
#: names a verdict. Declared because a node that declares nothing is refused at
#: the gate, and because the alternative — `ungraded=True` — would be untrue
#: here. The verdict is not graded; that it is a verdict at all is checkable.
JUDGE_SCHEMA = {
    "type": "object",
    "required": ["verdict_token"],
    "properties": {"verdict_token": {"type": "string"}},
}

SCHEMAS = {
    "judge": JUDGE_SCHEMA,
    "plan": PLAN_SCHEMA,
    "patchset": PATCHSET_SCHEMA,
    "research_claims": RESEARCH_CLAIMS_SCHEMA,
    "test_report": TEST_REPORT_SCHEMA,
    "report": REPORT_SCHEMA,
}


class PayloadTampered(ContractError):
    """A promoted artifact's file no longer hashes to what the gate saw."""


def verify_payload(payload, expected_digest: str, *, artifact_id: str = "") -> None:
    """The content the state vouches for is the content being handed over.

    `check_artifact_write` closed the door on forging the STATE. This is the
    same door one layer down, and it was still open: `Runner._read_payload`
    loaded the artifact FILE by path and never compared it to the digest the
    store had recorded. Demonstrated on a real run —

        DB state  PROMOTED
        DB digest e1d55fe5…              (hash of {"value": 41})
        file on disk edited by hand
        what the next node received  {"value": 999999, "injected": ...}

    The gate passed on one payload and the consumer got another. The digest was
    computed, stored, and never read, which is the most expensive way to not
    have a checksum.

    Refusing here rather than in the reader keeps this testable without a store,
    and makes the failure a named class the scheduler can classify instead of a
    silent substitution.
    """
    actual = digest(payload)
    if actual != expected_digest:
        raise PayloadTampered(
            f"artifact {artifact_id or '?'} does not match the digest recorded "
            f"when it was promoted ({expected_digest[:12]} on the record, "
            f"{actual[:12]} on disk). The gate graded different content from "
            f"the content being handed to the next step")


def artifact_path(data_dir: str, run_id: str, artifact_id: str) -> str:
    """Where an artifact's payload is stored on disk.

    Payloads live as files, not as blobs in the event log, because a patch or a
    captured stdout is exactly the thing that blows past the record-size ceiling
    `core/jsonl.py` refuses to write.
    """
    return os.path.join(data_dir, "state", "runtime", run_id, "artifacts",
                        f"{artifact_id}.json")
