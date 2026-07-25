"""Research support: precise search planning and claim–evidence verification.

Two capabilities, one discipline
-------------------------------
When a paper (or any external source) is handed to this harness, the job is not
to summarize it. It is to determine which of its claims are actually supported,
which artifacts would have to exist for the claims to be reproducible, and which
citations resolve to real work. Those are three separate checks and this module
keeps them separate, because a paper can be well-cited and unreproducible, or
reproducible and overclaimed.

Why searching needs a plan
--------------------------
A single query returns a single view. The observed failure of one-shot search is
not that it returns nothing, but that it returns *enough* — plausible results that
stop the search before the contradicting source is found. `plan_queries` therefore
decomposes an information need into deliberately DIFFERENT query shapes, including
one that searches for refutation. Searching for "X works" and searching for "X
fails" return different corpora, and only running the first is how a search
confirms whatever it started with.

Why citation checking is structural
-----------------------------------
Fabricated references are stylistically indistinguishable from real ones: the
author list, year, and venue all look right. Detection therefore cannot be
stylistic — it has to be *resolution*, checking each reference against a corpus
that was retrieved independently. `verify_citations` reports three severities
rather than a boolean because the remedies differ: an exact match needs nothing,
a metadata mismatch needs a correction, and an unresolvable reference means the
claim resting on it has no support at all.

No network access here
----------------------
This module PLANS searches and SCORES retrieved evidence. It never fetches. The
kit's MCP gateway deliberately exposes no network tool, which removes the
exfiltration leg of the lethal trifecta; adding a fetcher here would reintroduce
it in the one place that also handles untrusted external text. Retrieval is
performed by the caller's own tooling and the results are passed in.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

from .swarm.diversity import jaccard_distance, token_set, tokens

# --------------------------------------------------------------------------
# Search planning
# --------------------------------------------------------------------------

#: Query shapes, each retrieving a structurally different slice of the corpus.
#: `refutation` and `limitation` are the two that a naive search omits, and they
#: are the two that change conclusions.
QUERY_SHAPES: dict[str, str] = {
    "canonical": "the standard name of the thing, as its own literature calls it",
    "mechanism": "how it works, in implementation terms",
    "refutation": "evidence that it does NOT work, fails, or was not replicated",
    "limitation": "stated constraints, assumptions, and scope conditions",
    "alternative": "the competing approach, so the comparison exists",
    "recency": "the current state, to catch superseded results",
}


@dataclasses.dataclass
class QueryPlan:
    """A decomposed information need."""

    need: str
    queries: list[dict]
    stop_condition: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def plan_queries(need: str, *, year_hint: str | None = None) -> QueryPlan:
    """Decompose one information need into complementary queries.

    Terms are extracted from the need rather than paraphrased, because
    paraphrasing at plan time is where a search quietly changes subject. The
    refutation query is built by ADDING negative-result vocabulary to the same
    terms, so it searches the same topic from the opposite direction instead of
    searching a different topic.
    """
    terms = [t for t in tokens(need) if len(t) > 3][:6]
    core = " ".join(terms)
    queries = [
        {"shape": "canonical", "query": core,
         "rationale": QUERY_SHAPES["canonical"]},
        {"shape": "mechanism", "query": f"{core} how it works implementation",
         "rationale": QUERY_SHAPES["mechanism"]},
        {"shape": "refutation",
         "query": f"{core} does not work failed replication negative result "
                  f"criticism",
         "rationale": QUERY_SHAPES["refutation"]},
        {"shape": "limitation", "query": f"{core} limitations assumptions scope",
         "rationale": QUERY_SHAPES["limitation"]},
        {"shape": "alternative", "query": f"{core} alternative compared baseline",
         "rationale": QUERY_SHAPES["alternative"]},
    ]
    if year_hint:
        queries.append({
            "shape": "recency", "query": f"{core} {year_hint}",
            "rationale": QUERY_SHAPES["recency"]})
    return QueryPlan(
        need=need, queries=queries,
        stop_condition=(
            "stop when the refutation and limitation queries stop returning NEW "
            "objections — not when the canonical query looks satisfying. A "
            "search that ran only the canonical shape has confirmed its premise, "
            "not tested it"))


# --------------------------------------------------------------------------
# Claim extraction and verification
# --------------------------------------------------------------------------

#: Claim strength markers. Strength matters because the evidence bar scales with
#: it: "may improve" needs an example, "improves by 40%" needs the measurement.
STRENGTH_MARKERS: dict[str, tuple[str, ...]] = {
    "absolute": ("always", "never", "guarantees", "proves", "eliminates",
                 "all cases", "impossible", "cannot fail"),
    "quantified": ("%", "times faster", "x faster", "reduces", "increases",
                   "outperforms", "improves by", "sota", "state-of-the-art"),
    "comparative": ("better than", "worse than", "compared to", "versus",
                    "outperforms", "exceeds"),
    "hedged": ("may", "might", "could", "suggests", "indicates", "appears",
               "we believe", "likely", "potentially"),
}

#: Artifacts a claim of a given kind needs before it is reproducible. This is the
#: checklist a paper is held to — not "is there a repo link" but "is there the
#: specific thing this specific claim requires".
REQUIRED_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "quantified": ("the measurement script or command",
                   "the exact dataset split used",
                   "the baseline it is measured against",
                   "the number of runs and the variance"),
    "comparative": ("the competing method's configuration",
                    "equal-budget confirmation (same compute/tokens)",
                    "the metric definition"),
    "absolute": ("the proof or the exhaustive case enumeration",
                 "the stated scope in which 'always' holds"),
    "hedged": ("at least one worked example",),
}


@dataclasses.dataclass
class Claim:
    """One extracted assertion, with its strength classified."""

    text: str
    strength: str
    #: Evidence ids (from an independently retrieved corpus) offered in support.
    evidence_ids: tuple[str, ...] = ()
    source_locator: str = ""

    def required_artifacts(self) -> tuple[str, ...]:
        return REQUIRED_ARTIFACTS.get(self.strength, ())


def _marker_present(marker: str, text_lower: str) -> bool:
    """Whether `marker` appears as a whole word (or whole phrase) in the text.

    Substring matching is wrong here and fails silently in the worst direction:
    `"proves" in "improves"` is True, so "improves throughput by 30%" would be
    classified as an ABSOLUTE claim and held to the "supply the proof" bar instead
    of the "supply the measurement" bar — sending the reader after the wrong
    artifact. `"x" in "..."` has the same problem for `10x` inside `10xyz`.

    Markers containing non-word characters (`%`, `/`) are matched with a boundary
    only on the word side, because `\\b` does not apply next to a symbol.
    """
    if not marker:
        return False
    left = r"\b" if marker[0].isalnum() else ""
    right = r"\b" if marker[-1].isalnum() else ""
    return re.search(left + re.escape(marker) + right, text_lower) is not None


def classify_strength(text: str) -> str:
    """Strongest marker present wins; absent markers mean a plain assertion.

    Order matters: a sentence containing both a hedge and a number ("may reduce
    latency by 40%") is held to the QUANTIFIED bar, because the number is the part
    a reader will act on and the hedge does not make it unmeasurable.
    """
    low = text.lower()
    for strength in ("absolute", "quantified", "comparative"):
        if any(_marker_present(m, low) for m in STRENGTH_MARKERS[strength]):
            return strength
    if any(_marker_present(m, low) for m in STRENGTH_MARKERS["hedged"]):
        return "hedged"
    return "plain"


def extract_claims(text: str, *, max_claims: int = 40) -> list[Claim]:
    """Split prose into candidate claims and classify each.

    Sentence-level and deliberately mechanical: this is a triage step whose output
    a human or a model then works through. Attempting semantic claim extraction
    here would introduce exactly the unverifiable interpretation layer the module
    exists to keep out of the verification path.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text)
                 if len(s.strip()) > 25]
    claims = []
    for idx, sentence in enumerate(sentences[:max_claims]):
        # A sentence with no verb-like content is a heading, not a claim.
        if len(tokens(sentence)) < 4:
            continue
        claims.append(Claim(text=sentence, strength=classify_strength(sentence),
                            source_locator=f"sentence:{idx + 1}"))
    return claims


@dataclasses.dataclass
class ClaimVerdict:
    """Whether one claim is supported by the evidence actually retrieved."""

    claim: Claim
    supported: bool
    support_score: float
    matched_evidence: list[str]
    missing_artifacts: list[str]
    note: str

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["claim"] = dataclasses.asdict(self.claim)
        return d


def verify_claim(claim: Claim, corpus: Sequence[dict], *,
                 min_support: float = 0.20) -> ClaimVerdict:
    """Score a claim against retrieved evidence records.

    `corpus` entries are dicts with at least `id` and `text`. Matching is lexical
    overlap — the same stdlib-only constraint as everywhere else — so the score is
    a SCREEN: it reliably finds "no evidence mentions this at all" and cannot
    adjudicate a subtle mismatch between what a source says and what the claim
    says it says. That limit is stated in the verdict note rather than hidden,
    because a support score is exactly the kind of number that gets quoted as if
    it were a judgment.
    """
    claim_tokens = token_set(claim.text)
    matches: list[tuple[str, float]] = []
    for record in corpus:
        overlap = 1.0 - jaccard_distance(claim_tokens,
                                         token_set(record.get("text", "")))
        if overlap >= min_support:
            matches.append((record.get("id", "?"), round(overlap, 4)))
    matches.sort(key=lambda pair: -pair[1])
    best = matches[0][1] if matches else 0.0

    missing = list(claim.required_artifacts()) if claim.strength in (
        "quantified", "comparative", "absolute") else []

    if not matches:
        note = ("no retrieved evidence overlaps this claim: it is UNSUPPORTED "
                "here, which is not the same as false — it means nothing in the "
                "corpus speaks to it")
    elif claim.strength == "absolute":
        note = ("absolute claims are not established by lexical overlap; this "
                "needs the proof or the enumerated scope named above")
    elif claim.strength == "quantified":
        note = ("a number is only verified by re-running its measurement; "
                "overlap shows the topic matches, not that the value does")
    else:
        note = "topical support found; verify the direction of the claim by hand"

    return ClaimVerdict(
        claim=claim,
        supported=bool(matches) and claim.strength not in ("absolute",),
        support_score=best,
        matched_evidence=[m[0] for m in matches[:5]],
        missing_artifacts=missing,
        note=note)


# --------------------------------------------------------------------------
# Citation resolution
# --------------------------------------------------------------------------

EXACT = "exact"
METADATA_MISMATCH = "metadata_mismatch"
UNRESOLVABLE = "unresolvable"


@dataclasses.dataclass
class Reference:
    """A citation as written in the source."""

    raw: str
    title: str = ""
    year: str = ""
    identifier: str = ""      # DOI / arXiv id / URL


def parse_reference(raw: str) -> Reference:
    """Pull the checkable fields out of a reference string.

    Only fields that can be RESOLVED are extracted. Author lists are omitted
    deliberately: they are the field most often subtly wrong in a fabricated
    reference and the hardest to match reliably, so treating a name mismatch as
    signal would produce false accusations at a high rate.
    """
    year = ""
    m = re.search(r"\b(19|20|21)\d{2}\b", raw)
    if m:
        year = m.group(0)
    identifier = ""
    for pattern in (r"10\.\d{4,9}/[-._;()/:a-z0-9A-Z]+",
                    r"arxiv[:\s/]*(\d{4}\.\d{4,5})",
                    r"https?://\S+"):
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            identifier = m.group(0)
            break
    # Title heuristic: the longest quoted span, else the longest comma-free run.
    quoted = re.findall(r"[\"“']([^\"”']{12,})[\"”']", raw)
    if quoted:
        title = max(quoted, key=len)
    else:
        parts = [p.strip() for p in raw.split(",")]
        title = max(parts, key=len) if parts else raw
    return Reference(raw=raw.strip(), title=title.strip(), year=year,
                     identifier=identifier)


def verify_citations(references: Sequence[str], corpus: Sequence[dict]
                     ) -> dict:
    """Resolve each reference against an independently retrieved corpus.

    Three severities, because three different things go wrong:

    - `exact` — title and year both align with a corpus record.
    - `metadata_mismatch` — the work exists but a field is wrong. Correctable,
      and NOT evidence of fabrication.
    - `unresolvable` — nothing in the corpus matches. This is the severity that
      matters: any claim resting on this reference currently has no support.

    An empty corpus produces all-unresolvable and says so, rather than reporting a
    clean bill of health, because "nothing to check against" and "everything
    checks out" are opposite situations that a boolean would merge.
    """
    if not corpus:
        return {
            "checked": 0, "corpus_size": 0,
            "verdict": ("NOT CHECKED: the corpus is empty. Citation "
                        "verification requires independently retrieved records "
                        "— absence of a corpus is not absence of a problem"),
            "results": [],
        }

    index = [(r.get("id", "?"), token_set(r.get("text", "")),
              str(r.get("year", ""))) for r in corpus]
    results = []
    for raw in references:
        ref = parse_reference(raw)
        title_tokens = token_set(ref.title)
        best_id, best_overlap, best_year = None, 0.0, ""
        for rid, rtokens, ryear in index:
            if not title_tokens:
                continue
            overlap = len(title_tokens & rtokens) / len(title_tokens)
            if overlap > best_overlap:
                best_id, best_overlap, best_year = rid, overlap, ryear
        if best_overlap < 0.5:
            severity, detail = UNRESOLVABLE, (
                f"no corpus record matches the title (best overlap "
                f"{best_overlap:.2f} < 0.50). Any claim citing this is "
                "currently unsupported")
        elif ref.year and best_year and ref.year != best_year:
            severity, detail = METADATA_MISMATCH, (
                f"matched {best_id} but the year differs: cited {ref.year}, "
                f"corpus says {best_year}")
        else:
            severity, detail = EXACT, f"resolves to {best_id}"
        results.append({
            "raw": ref.raw[:200], "title": ref.title[:160],
            "year": ref.year, "identifier": ref.identifier,
            "severity": severity, "matched": best_id,
            "title_overlap": round(best_overlap, 4), "detail": detail,
        })
    counts: dict[str, int] = {}
    for r in results:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    unresolvable = counts.get(UNRESOLVABLE, 0)
    return {
        "checked": len(results), "corpus_size": len(corpus),
        "counts": counts, "results": results,
        "verdict": (f"{unresolvable}/{len(results)} references do not resolve — "
                    "treat every claim resting on them as unsupported"
                    if unresolvable else
                    f"all {len(results)} references resolve to corpus records"),
    }


# --------------------------------------------------------------------------
# Reproducibility checklist
# --------------------------------------------------------------------------

def reproducibility_report(claims: Sequence[Claim], *,
                           artifacts_present: Sequence[str] = ()) -> dict:
    """What would have to exist to reproduce this source's strongest claims.

    Driven by the claims themselves rather than by a generic checklist, so the
    output names the artifact each specific claim needs. A generic checklist gets
    ticked; a claim-derived one has to be answered.
    """
    present = {a.lower() for a in artifacts_present}
    rows = []
    for claim in claims:
        needed = claim.required_artifacts()
        if not needed:
            continue
        missing = [n for n in needed
                   if not any(p in n.lower() or n.lower() in p for p in present)]
        rows.append({
            "claim": claim.text[:200],
            "strength": claim.strength,
            "locator": claim.source_locator,
            "required": list(needed),
            "missing": missing,
            "reproducible": not missing,
        })
    blocked = [r for r in rows if not r["reproducible"]]
    return {
        "load_bearing_claims": len(rows),
        "reproducible": len(rows) - len(blocked),
        "blocked": len(blocked),
        "rows": rows,
        "verdict": (f"{len(blocked)}/{len(rows)} load-bearing claims cannot be "
                    "reproduced with the artifacts on hand"
                    if blocked else
                    f"all {len(rows)} load-bearing claims have their required "
                    "artifacts"),
    }
