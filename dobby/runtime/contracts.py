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

SCHEMAS = {
    "plan": PLAN_SCHEMA,
    "patchset": PATCHSET_SCHEMA,
    "research_claims": RESEARCH_CLAIMS_SCHEMA,
    "test_report": TEST_REPORT_SCHEMA,
    "report": REPORT_SCHEMA,
}


def artifact_path(data_dir: str, run_id: str, artifact_id: str) -> str:
    """Where an artifact's payload is stored on disk.

    Payloads live as files, not as blobs in the event log, because a patch or a
    captured stdout is exactly the thing that blows past the record-size ceiling
    `core/jsonl.py` refuses to write.
    """
    return os.path.join(data_dir, "state", "runtime", run_id, "artifacts",
                        f"{artifact_id}.json")
