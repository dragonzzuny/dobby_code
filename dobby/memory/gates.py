"""Promotion gating and lossy compression with a measured leakage budget.

Two jobs, one principle
-----------------------
Moving a fact UP a tier and COMPRESSING a fact are the same operation seen from
two sides: both replace detail with a shorter representation, and both can
silently destroy the one token that made the fact useful. So both go through the
same discipline — decide explicitly what is dropped, then MEASURE what was lost
against the load-bearing content, and refuse the operation when the loss exceeds
a declared budget.

The gating discipline (the useful half of "LSTM")
------------------------------------------------
A learned recurrent compressor is not implementable in a stdlib kit with no
training loop, and claiming one would be unverifiable. What a gated recurrent
cell actually contributes is a *per-item decision structure*, and that transfers
directly:

- **forget gate** → what leaves the tier (expired, superseded, contradicted)
- **input gate** → what is admitted at all (novel enough, evidenced enough)
- **output gate** → what is exposed to the next step (surfaced in a context pack)

Implemented as three explicit, inspectable predicates. The value is that every
item's fate has a named reason, which is what makes a memory system auditable;
that property is lost the moment the decision becomes a learned weight.

Compression guideline optimization
----------------------------------
`CompressionGuideline` follows the ACON result: rather than tuning a compression
model, keep the compression RULE in natural language and improve it from paired
failures — cases where the full context succeeded and the compressed context
failed. Each such pair names something the rule wrongly discarded, and the rule
gains a preservation clause. This is model-agnostic (no parameter updates) and
the guideline is a diffable artifact, which matters because a compression policy
that changes silently changes what the agent can remember.

Leakage, defined
----------------
"Leakage" here is the fraction of LOAD-BEARING tokens absent from the compressed
form — not overall token loss, which is the point of compressing. Load-bearing
tokens are identifiers, paths, numbers, and negations: the content whose removal
changes meaning rather than length. Dropping prose is compression; dropping
`not` or a file path is corruption.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Sequence

from ..swarm.diversity import token_set, tokens
from .tiers import TIER_INDEX, TIERS, MemoryItem

# --------------------------------------------------------------------------
# Load-bearing content detection
# --------------------------------------------------------------------------

#: Identifiers, paths, and dotted names. Losing one turns "edit router.py line 40"
#: into "edit the file", which is not a shorter version of the same fact.
_IDENTIFIER_RE = re.compile(
    r"""(?:
        [\w./\\-]+\.(?:py|ts|tsx|js|mjs|cjs|json|ya?ml|toml|md|sh|ps1)\b
      | \b[a-z_][a-z0-9_]{2,}\.[a-z_][a-z0-9_]{2,}\b
      | \b[a-z][a-z0-9]*_[a-z0-9_]+\b
      | \b[A-Z][A-Z0-9_]{2,}\b
    )""",
    re.VERBOSE)

#: Quantities with or without units. A threshold without its number is advice.
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|x|kb|mb|gb|tokens?|days?)?\b",
                        re.IGNORECASE)

#: Negations and exception markers. These invert meaning, so they are the highest-
#: value tokens in the text and the easiest for a summarizer to drop as filler.
_NEGATIONS = frozenset("""
not never no none cannot can't won't don't doesn't isn't aren't without
except unless fails failed broken missing absent forbidden must-not
""".split())


def load_bearing(text: str) -> set[str]:
    """Tokens whose removal would change the MEANING of `text`, not its length."""
    out: set[str] = set()
    out |= {m.group(0) for m in _IDENTIFIER_RE.finditer(text)}
    out |= {m.group(0).strip() for m in _NUMBER_RE.finditer(text)}
    out |= {t for t in re.findall(r"[a-z']+", text.lower()) if t in _NEGATIONS}
    return {t for t in out if t}


def leakage(original: str, compressed: str) -> dict:
    """Measure what compression destroyed, separating loss from corruption."""
    orig_lb = load_bearing(original)
    comp_lb = load_bearing(compressed)
    lost = sorted(orig_lb - comp_lb)
    orig_tokens = len(tokens(original))
    comp_tokens = len(tokens(compressed))
    ratio = (comp_tokens / orig_tokens) if orig_tokens else 1.0
    return {
        "load_bearing_total": len(orig_lb),
        "load_bearing_lost": len(lost),
        "lost_items": lost[:40],
        "leakage_rate": round(len(lost) / len(orig_lb), 4) if orig_lb else 0.0,
        "token_ratio": round(ratio, 4),
        "compression_ratio": round(1.0 - ratio, 4),
        # Both numbers are needed to judge a compression: high compression with
        # zero leakage is the goal, and high compression with high leakage is
        # the failure that looks like success on a token count alone.
        "verdict": _leak_verdict(len(lost), len(orig_lb), ratio),
    }


def _leak_verdict(lost: int, total: int, ratio: float) -> str:
    if total == 0:
        return "no load-bearing content to preserve"
    rate = lost / total
    if rate == 0.0:
        return f"lossless on load-bearing content at {(1 - ratio) * 100:.0f}% reduction"
    if rate <= 0.05:
        return f"acceptable: {rate * 100:.1f}% of load-bearing tokens lost"
    return (f"REJECT: {rate * 100:.1f}% of load-bearing tokens lost "
            f"({lost}/{total}) — this is corruption, not compression")


#: Maximum tolerable load-bearing loss. 5% is not a tuning choice: at typical
#: item sizes (10–40 load-bearing tokens) it means "at most one, and only in
#: larger items", i.e. essentially lossless with room for tokenizer edge cases.
MAX_LEAKAGE = 0.05


# --------------------------------------------------------------------------
# The three gates
# --------------------------------------------------------------------------

@dataclasses.dataclass
class GateDecision:
    """One item's fate, with the reason recorded."""

    item_id: str
    gate: str            # "forget" | "input" | "output"
    passed: bool
    reason: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def forget_gate(item: MemoryItem, *, now: float | None = None,
                superseded_by: str | None = None,
                contradicted: bool = False) -> GateDecision:
    """Should this item leave its tier? `passed=True` means FORGET it.

    Verified items are never forgotten for age alone. The kit's authority rule
    says a newer unverified claim cannot supersede an older verified one, so
    letting a TTL quietly delete verified knowledge would achieve by timeout what
    the rule forbids by assertion.
    """
    if contradicted:
        return GateDecision(item.id, "forget", True,
                            "contradicted by a Decision node: keeping both "
                            "sides at the same tier makes retrieval nondeterministic")
    if superseded_by:
        return GateDecision(item.id, "forget", True,
                            f"superseded by {superseded_by}")
    if item.verified:
        return GateDecision(item.id, "forget", False,
                            "verified: exempt from age-based expiry")
    if item.expired(now):
        return GateDecision(item.id, "forget", True,
                            f"unverified and past its tier TTL "
                            f"({item.age_days(now):.1f}d at tier '{item.tier}')")
    return GateDecision(item.id, "forget", False, "within TTL")


def input_gate(candidate: MemoryItem, existing: Sequence[MemoryItem], *,
               min_novelty: float = 0.25) -> GateDecision:
    """Should this item be admitted to its tier? `passed=True` means ADMIT.

    Admission requires NOVELTY against what the tier already holds. A store that
    admits near-duplicates degrades in a specific way: retrieval returns five
    phrasings of one fact and the context pack spends its budget saying the same
    thing repeatedly, crowding out the second fact the task needed.
    """
    if not candidate.body.strip() and not candidate.title.strip():
        return GateDecision(candidate.id, "input", False, "empty content")
    cand = token_set(candidate.text())
    if not cand:
        return GateDecision(candidate.id, "input", False,
                            "no content tokens after stopwording")
    same_tier = [e for e in existing if e.tier == candidate.tier]
    for other in same_tier:
        overlap = cand & token_set(other.text())
        similarity = len(overlap) / len(cand) if cand else 0.0
        if similarity > (1.0 - min_novelty):
            # Verified beats unverified regardless of arrival order.
            if candidate.verified and not other.verified:
                return GateDecision(
                    candidate.id, "input", True,
                    f"near-duplicate of {other.id} but VERIFIED where the "
                    "incumbent is not: admit and supersede")
            return GateDecision(
                candidate.id, "input", False,
                f"{similarity * 100:.0f}% content overlap with {other.id} "
                f"(needs {min_novelty * 100:.0f}% novelty)")
    return GateDecision(candidate.id, "input", True, "novel at this tier")


def output_gate(item: MemoryItem, query: str, *,
                min_relevance: float = 0.10) -> GateDecision:
    """Should this item be surfaced for `query`? `passed=True` means EXPOSE.

    The last line of defence for the context budget. An item can be correctly
    stored, correctly promoted, and still be the wrong thing to put in front of
    the model for this task.
    """
    q = token_set(query)
    if not q:
        return GateDecision(item.id, "output", False, "empty query")
    overlap = token_set(item.text()) & q
    relevance = len(overlap) / len(q)
    if relevance < min_relevance:
        return GateDecision(item.id, "output", False,
                            f"relevance {relevance:.2f} < {min_relevance}")
    return GateDecision(item.id, "output", True,
                        f"relevance {relevance:.2f}"
                        + (" (verified)" if item.verified else ""))


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------

def promote(children: Sequence[MemoryItem], *, parent_id: str, title: str,
            summary: str, verified: bool | None = None,
            source: str = "promotion") -> tuple[MemoryItem, dict]:
    """Build a parent one tier UP that indexes `children`, and audit the loss.

    The parent's tier is derived from the children rather than passed in, so a
    caller cannot accidentally create the tier skip that `tiers.integrity()`
    would later flag. Mixed-tier children are refused outright: a summary
    spanning two abstraction levels has no well-defined place in the hierarchy.

    `verified` defaults to the AND of the children — a summary of partly
    unverified detail is itself unverified. Allowing it to default to True would
    let promotion manufacture confidence, which is the one thing a memory
    hierarchy must never do.
    """
    if not children:
        raise ValueError("cannot promote an empty child set")
    tiers = {c.tier for c in children}
    if len(tiers) > 1:
        raise ValueError(
            f"children span multiple tiers {sorted(tiers)}: a parent must "
            "summarize exactly one abstraction level")
    child_tier = next(iter(tiers))
    depth = TIER_INDEX[child_tier]
    if depth == 0:
        raise ValueError("'nation' is the root tier; nothing can be promoted above it")
    parent_tier = TIERS[depth - 1]

    combined = "\n".join(c.text() for c in children)
    audit = leakage(combined, f"{title}\n{summary}")
    resolved_verified = (all(c.verified for c in children)
                         if verified is None else verified)

    parent = MemoryItem(
        id=parent_id, tier=parent_tier, title=title, body=summary,
        children=tuple(c.id for c in children),
        verified=resolved_verified, source=source,
        payload=_payload_for(parent_tier, children, summary))
    audit["accepted"] = audit["leakage_rate"] <= MAX_LEAKAGE
    audit["parent_tier"] = parent_tier
    audit["child_tier"] = child_tier
    audit["child_count"] = len(children)
    audit["verified"] = resolved_verified
    if not audit["accepted"]:
        audit["action"] = ("promotion REFUSED: rewrite the summary to retain the "
                           "listed load-bearing tokens, or promote fewer children")
    return parent, audit


def _payload_for(tier: str, children: Sequence[MemoryItem],
                 summary: str) -> dict:
    """Build the tier-appropriate payload. Imported lazily to avoid a cycle."""
    from .tiers import (build_forest_payload, build_mountain_payload,
                        build_nation_payload, build_tree_payload)
    if tier == "nation":
        return build_nation_payload(children)
    if tier == "mountain":
        return build_mountain_payload(children)
    if tier == "forest":
        return build_forest_payload(children)
    if tier == "tree":
        return build_tree_payload(summary)
    return {"mechanism": "none"}


# --------------------------------------------------------------------------
# Compression guideline (ACON-style, learned from paired failures)
# --------------------------------------------------------------------------

#: The starting rule. Every clause exists because dropping that class of token is
#: the observed way a summary stops being usable.
DEFAULT_CLAUSES: tuple[str, ...] = (
    "Preserve every file path, identifier, command, and flag verbatim.",
    "Preserve every number together with its unit and its comparison direction.",
    "Preserve negations and exceptions; 'does not X' must never become 'X'.",
    "Preserve the distinction between VERIFIED and unverified claims.",
    "Drop narrative, restatement, and hedging.",
    "Prefer one concrete sentence over three general ones.",
)


@dataclasses.dataclass
class CompressionGuideline:
    """A versioned, diffable natural-language compression rule.

    Stored as JSON with its full revision history. History is kept because a
    clause is only justified by the failure that produced it: without the
    originating pair, a future maintainer cannot tell a hard-won preservation
    rule from an arbitrary one, and will delete it.
    """

    clauses: list[str] = dataclasses.field(
        default_factory=lambda: list(DEFAULT_CLAUSES))
    revisions: list[dict] = dataclasses.field(default_factory=list)
    version: int = 1

    def render(self) -> str:
        head = ("Compress the following so that a later agent can act on it "
                "without the original. Rules:")
        body = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(self.clauses))
        return f"{head}\n{body}"

    def learn_from_failure(self, *, full_context: str, compressed_context: str,
                           failure_note: str) -> dict:
        """Add a preservation clause derived from one full-succeeds/compressed-fails pair.

        The clause is derived from the MEASURED loss, not from an opinion about
        it: `leakage` names the load-bearing tokens the compression dropped, and
        those token classes become the new rule. That keeps the guideline
        grounded in observed damage instead of accumulating plausible-sounding
        advice, which is how such rule lists usually rot.
        """
        audit = leakage(full_context, compressed_context)
        lost = audit["lost_items"]
        if not lost:
            return {"changed": False,
                    "reason": "no load-bearing tokens were lost, so this failure "
                              "is not attributable to compression — investigate "
                              "the task, prompt, or tool path instead",
                    "audit": audit}
        kinds = _classify_lost(lost)
        clause = ("Preserve " + ", ".join(sorted(kinds))
                  + f" — a compressed context omitting {lost[:5]} caused: "
                  + failure_note.strip())
        if clause in self.clauses:
            return {"changed": False, "reason": "clause already present",
                    "audit": audit}
        self.clauses.append(clause)
        self.version += 1
        rev = {"version": self.version, "added_clause": clause,
               "lost_tokens": lost[:20], "failure_note": failure_note.strip(),
               "leakage_rate": audit["leakage_rate"],
               "t": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self.revisions.append(rev)
        return {"changed": True, "revision": rev, "audit": audit}

    # -- persistence -----------------------------------------------------
    def save(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(self), f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "CompressionGuideline":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(clauses=list(data.get("clauses") or DEFAULT_CLAUSES),
                   revisions=list(data.get("revisions") or []),
                   version=int(data.get("version") or 1))


def _classify_lost(lost: Sequence[str]) -> set[str]:
    """Turn concrete lost tokens into the token CLASSES a rule can name."""
    kinds: set[str] = set()
    for token in lost:
        low = token.lower()
        if low in _NEGATIONS:
            kinds.add("negations and exception markers")
        elif re.fullmatch(r"\d+(?:\.\d+)?\s*\w*", token.strip()):
            kinds.add("numeric quantities with units")
        elif "." in token and "/" not in token and "\\" not in token:
            kinds.add("dotted identifiers (module.attribute)")
        elif "/" in token or "\\" in token or re.search(r"\.\w+$", token):
            kinds.add("file paths")
        elif token.isupper():
            kinds.add("constant and policy names")
        else:
            kinds.add("snake_case identifiers")
    return kinds
