"""What a worker is shown, chosen and budgeted — not everything, truncated.

WHAT IT REPLACES

`ProviderWorker._prompt` inlined every promoted dependency as
`json.dumps(inputs, indent=1)[:8000]`. A character cap is better than no cap and
it is not a relevance budget. Four things follow from it:

- a large plan payload is re-injected into the implementer that already has the
  task;
- a critic re-consumes raw tool output it does not need;
- the stable prefix of every call changes, which is the part a prompt cache
  wants held still;
- and when artifacts accumulate, what survives the cut is whatever serialised
  FIRST, not whatever mattered most.

The last one is the real defect. Truncation by position means the evidence a
worker most needs is the evidence most likely to be missing.

THE SEPARATION THIS ENFORCES

Durable record and working context are different things and the store already
knows it. Every artifact stays on disk, addressed and promoted; what reaches the
prompt is a RANKED, budgeted selection of it, plus handles for the rest. A worker
that needs more asks for more.

HOW IT RANKS, AND WHY DETERMINISTICALLY

    4  a direct dependency of this node
    3  the most recent failure
    2  overlaps this node's write set
    2  named by an acceptance check
    1  recent

Arithmetic, because a model call to decide what to put in a model call is the
cost this module exists to remove. The weights are an ordering and not a
measurement, and they are stated here so an argument about them is possible.

WHAT IT PROMISES ABOUT TRUNCATION

That it is reported. `truncation` names what was left out and why, so a worker's
failure to use evidence it never received is diagnosable from the record instead
of being mistaken for a worker that ignored it.
"""

from __future__ import annotations

import dataclasses
import json


@dataclasses.dataclass(frozen=True)
class ContextBudget:
    """Character ceilings per section. Small on purpose.

    Characters rather than tokens because that is what can be measured here
    without a tokenizer per provider; the ratio is stable enough for a budget
    and the honest name for it is a character budget.
    """

    task_chars: int = 1_200
    scope_chars: int = 600
    evidence_chars: int = 4_000
    failure_chars: int = 2_000
    schema_chars: int = 1_500

    @property
    def total(self) -> int:
        return (self.task_chars + self.scope_chars + self.evidence_chars
                + self.failure_chars + self.schema_chars)


#: The prefix that must not move between calls. Everything variable goes after
#: it, so a provider that caches a stable prefix keeps the cache.
STABLE_POLICY = (
    "You are one step in a gated pipeline. Your output is checked by commands, "
    "not by agreement. Change only what the scope allows; report anything else "
    "as a finding. If you cannot do something honestly, say so in your output "
    "rather than omitting it."
)


def _clip(text: str, limit: int) -> tuple[str, int]:
    """Return the text within `limit`, and how many characters were dropped."""
    text = text or ""
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def rank_evidence(inputs: dict, *, depends_on=(), write_set=(),
                  acceptance=(), latest_failure_node: str = "") -> list[tuple]:
    """`[(node_id, score), ...]`, best first. Pure arithmetic, no model call."""
    depends, writes = set(depends_on or ()), set(write_set or ())
    checks = " ".join(acceptance or ())
    order = list(inputs)
    scored = []
    for index, node_id in enumerate(order):
        blob = json.dumps(inputs[node_id], ensure_ascii=False, default=str)
        score = 0
        if node_id in depends:
            score += 4
        if node_id == latest_failure_node:
            score += 3
        if writes and any(path in blob for path in writes):
            score += 2
        if node_id and node_id in checks:
            score += 2
        # Recency as the tie-break only: later entries were produced later.
        score += 1 if index >= len(order) - 1 else 0
        scored.append((node_id, score))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0]))


def summarise(payload) -> str:
    """One line about an artifact: enough to decide whether to ask for it."""
    if isinstance(payload, dict):
        for key in ("summary", "objective", "verdict", "command", "claim"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        return f"object with keys {sorted(payload)[:8]}"
    if isinstance(payload, list):
        return f"list of {len(payload)} item(s)"
    return str(payload)[:200]


@dataclasses.dataclass
class ContextPack:
    """What one worker is actually shown, and what it was not."""

    stable_policy: str = STABLE_POLICY
    task: str = ""
    scope: dict = dataclasses.field(default_factory=dict)
    evidence: list = dataclasses.field(default_factory=list)
    references: list = dataclasses.field(default_factory=list)
    failure: str = ""
    schema: dict = dataclasses.field(default_factory=dict)
    truncation: dict = dataclasses.field(default_factory=dict)

    def render(self) -> str:
        """The prompt. Stable prefix first, then task, scope, evidence, schema."""
        parts = [self.stable_policy, "", self.task]

        if self.scope:
            lines = ["", "## Scope"]
            if self.scope.get("write_set"):
                lines.append(f"You may change ONLY: "
                             f"{', '.join(self.scope['write_set'])}")
            if self.scope.get("acceptance"):
                lines.append("Done when these pass, and they will be run "
                             "against your work:")
                lines.extend(f"  {c}" for c in self.scope["acceptance"])
            parts.append("\n".join(lines))

        if self.evidence:
            lines = ["", "## Verified inputs",
                     "The PROMOTED outputs of the steps this one depends on. "
                     "Unverified output is not an input, on purpose."]
            for row in self.evidence:
                lines.append(f"\n### {row['id']} — {row['summary']}")
                if row.get("excerpt"):
                    lines.append(row["excerpt"])
            parts.append("\n".join(lines))

        if self.references:
            parts.append("\n## Available, not included\n"
                         "Ask for these by id if you need them:\n"
                         + "\n".join(f"  {r['id']} — {r['summary']}"
                                     for r in self.references))

        if self.failure:
            parts.append("\n## What failed last time\n"
                         "The runtime's own record. The approach that produced "
                         "it did not work.\n" + self.failure)

        if self.schema:
            parts.append("\n## Required output\n"
                         "Reply with ONE JSON document and nothing else, "
                         "satisfying this schema exactly. A field you cannot "
                         "fill honestly is a reason to say so inside the "
                         "document, not to omit it.\n"
                         + json.dumps(self.schema, ensure_ascii=False, indent=1))

        if self.truncation.get("dropped_chars"):
            parts.append(f"\n(Context was budgeted: "
                         f"{self.truncation['dropped_chars']} characters of "
                         f"evidence were not included, and "
                         f"{len(self.references)} artifact(s) are referenced by "
                         f"id instead. Ask if you need them.)")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def build(node, context: dict, *, schema=None,
          budget: ContextBudget | None = None) -> ContextPack:
    """Assemble the pack for one node from what the runner already has."""
    budget = budget or ContextBudget()
    inputs = context.get("inputs") or {}
    contract = getattr(node, "contract", None)
    write_set = tuple(getattr(contract, "expected_paths", ()) or ())
    acceptance = tuple(getattr(contract, "acceptance_checks", ()) or ())

    task, task_dropped = _clip(node.instruction.strip(), budget.task_chars)
    ranked = rank_evidence(inputs, depends_on=getattr(node, "depends_on", ()),
                           write_set=write_set, acceptance=acceptance,
                           latest_failure_node=context.get("failed_node", ""))

    evidence, references = [], []
    spent, dropped = 0, 0
    for node_id, _score in ranked:
        blob = json.dumps(inputs[node_id], ensure_ascii=False, indent=1,
                          default=str)
        summary = summarise(inputs[node_id])
        room = budget.evidence_chars - spent
        if room <= 0:
            references.append({"id": node_id, "summary": summary})
            dropped += len(blob)
            continue
        excerpt, cut = _clip(blob, room)
        spent += len(excerpt)
        dropped += cut
        evidence.append({"id": node_id, "summary": summary, "excerpt": excerpt})
        if cut:
            references.append({"id": node_id, "summary": summary})

    failure, failure_cut = _clip(context.get("failure_detail", ""),
                                 budget.failure_chars)

    return ContextPack(
        task=task,
        scope={"write_set": list(write_set), "acceptance": list(acceptance)},
        evidence=evidence, references=references, failure=failure,
        schema=schema or {},
        truncation={"dropped_chars": dropped + task_dropped + failure_cut,
                    "referenced_not_included": len(references),
                    "budget": dataclasses.asdict(budget),
                    "note": ("evidence is ranked by dependency, latest failure, "
                             "write-set overlap and acceptance reference, then "
                             "budgeted; what was cut is listed by id, not "
                             "silently lost")})
