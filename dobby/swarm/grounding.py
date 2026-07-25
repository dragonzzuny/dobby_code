"""The grounding gate: no ideation before prior art, no idea without an anchor.

The problem this solves
-----------------------
Unconstrained LLM ideation produces fluent nonsense. The published pattern is
consistent and specific: directly prompted models generate ideas rated MORE novel
than expert ideas but LESS feasible, and when those ideas are actually executed
they score significantly lower than human ones. The gap is operational, not
stylistic — the ideas read well and do not work. Optimizing a panel for novelty
therefore optimizes for the failure mode.

Two mitigations are known to work, and both are structural rather than a matter
of prompt wording:

1. **Ground generation in retrieved prior art.** Putting related literature (or,
   here, the project's own knowledge graph and codebase evidence) into context
   both reduces hallucination and measurably increases useful novelty, because
   the model is recombining real constraints instead of inventing plausible ones.
2. **Require each idea to be falsifiable.** An idea that names no test cannot be
   wrong, so it cannot be evaluated, so it survives review by being unfalsifiable
   rather than by being good.

This module enforces both as a hard gate that runs BEFORE any synthesis, with
deterministic checks only. It spends no tokens: an idea missing an evidence
anchor is rejected by structure, not by a model's opinion of it. That ordering
matters — a model asked "is this idea grounded?" will often say yes.

Cognitive-psychology basis
--------------------------
Three findings from the creativity literature shape the design:

- **Production blocking** (Diehl & Stroebe, 1987): face-to-face brainstorming
  groups generate FEWER and less creative solutions than the same number of
  people working alone, largely because members lose their own ideas while
  waiting for a turn. This is why `swarm/protocols.py` isolates the generation
  phase — isolation is not a convenience, it is the intervention.
- **Structured imagination / functional fixedness** (Duncker; Ward): when asked
  to imagine something novel, people import their most ACCESSIBLE existing
  knowledge, producing variations on the obvious. LLMs do the same thing, which
  is why lenses are assigned rather than chosen, and why `distance_from_prior_art`
  below penalizes an idea that merely restates retrieved evidence.
- **Geneplore** (Finke, Ward & Smith): creative cognition alternates between
  GENERATING pre-inventive structures and EXPLORING their implications, cycling
  until the structure survives exploration. `explore_cycle` implements that
  alternation, so an idea is refined against constraints rather than judged once.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

from .diversity import token_set, tokens

# --------------------------------------------------------------------------
# Rejection reasons. Named so a report can aggregate them and show WHICH kind of
# unfounded reasoning a panel is prone to, which is actionable in a way that a
# bare pass/fail count is not.
# --------------------------------------------------------------------------

NO_EVIDENCE = "no_evidence_anchor"
UNKNOWN_EVIDENCE = "evidence_id_not_in_corpus"
NO_TEST = "no_falsifiable_test"
VAGUE = "insufficient_specificity"
RESTATEMENT = "restates_prior_art"
OVERCLAIM = "unsupported_superlative"

#: Phrases that assert magnitude or certainty without a measurement. Their
#: presence is not proof of overclaiming, so they are only a violation when the
#: idea carries NO evidence anchor and NO test — i.e. when nothing could
#: substantiate them. Checking them alone would flag correct, well-evidenced
#: statements that happen to be emphatic.
_SUPERLATIVES = (
    "dramatically", "drastically", "massively", "orders of magnitude",
    "revolutionary", "game-chang", "breakthrough", "state-of-the-art",
    "seamless", "effortless", "guarantee", "always works", "never fails",
    "10x", "100x", "best-in-class", "world-class", "cutting-edge",
)

#: Markers of a concrete, checkable proposal. An idea that contains none of
#: these is describing an intention rather than a mechanism.
_MECHANISM_MARKERS = (
    "file", "function", "class", "module", "command", "flag", "field",
    "table", "column", "endpoint", "schema", "config", "test", "metric",
    "threshold", "algorithm", "index", "cache", "queue", "signal",
)

#: A falsifiable test must say how you would KNOW. These are the shapes that
#: qualify; prose intent ("we should verify this") does not.
_TEST_MARKERS = (
    "run ", "measure", "compare", "benchmark", "assert", "expect",
    "if ", "when ", "count", "diff", "reproduce", "fails when",
    "passes when", "threshold", "baseline", "a/b", "holdout",
)

#: Minimum content tokens for an idea body. Below this there is not enough text
#: to contain a mechanism, so specificity checks would be measuring noise.
_MIN_BODY_TOKENS = 12

#: Specificity floor: exactly one of the three signals firing fully.
#:
#: Written as a fraction rather than a decimal on purpose. `specificity` returns
#: the mean of three signals, so one signal firing yields 1/3 = 0.3333…, and a
#: literal threshold of 0.34 would silently demand MORE than one signal — which
#: is not what "at least one" means, and would reject an idea that names a real
#: file but no quantity. Comparing against the exact fraction removes that
#: mismatch between the stated rule and the arithmetic.
MIN_SPECIFICITY = 1.0 / 3.0

#: Novelty floor: 30% of the idea's vocabulary must be absent from its cited
#: evidence. Below that it is paraphrase of the prior art, not an extension of it.
MIN_DISTANCE = 0.30


@dataclasses.dataclass
class Evidence:
    """One retrieved prior-art item an idea may anchor to.

    `id` is the citable handle (a KG node id, a file path, a DOI). `verified`
    carries through the kit's confidence discipline: an idea anchored only to
    unverified evidence is grounded in something that may itself be wrong, and
    `IdeaAssessment` records that rather than treating all anchors as equal.
    """

    id: str
    summary: str
    path: str | None = None
    verified: bool = False

    def token_set(self) -> frozenset[str]:
        return token_set(f"{self.summary} {self.path or ''}")


@dataclasses.dataclass
class Idea:
    """A structured proposal. The schema IS the gate.

    Free-form prose cannot be checked for grounding, so ideation phases are
    required to return this shape. `evidence_ids` and `falsifiable_test` are the
    two fields that make the difference between a proposal and an assertion.
    """

    title: str
    body: str
    evidence_ids: tuple[str, ...] = ()
    falsifiable_test: str = ""
    lens: str = ""
    author: str = ""

    def text(self) -> str:
        return f"{self.title}\n{self.body}\n{self.falsifiable_test}"


@dataclasses.dataclass
class IdeaAssessment:
    """Verdict for one idea, with every reason named."""

    idea: Idea
    accepted: bool
    reasons: list[str]
    groundedness: float
    specificity: float
    distance_from_prior_art: float
    anchored_verified: int
    anchored_unverified: int
    notes: list[str]

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["idea"] = dataclasses.asdict(self.idea)
        return d


def has_prior_art(corpus: Sequence[Evidence]) -> tuple[bool, str]:
    """Whether ideation is permitted to start at all.

    The gate is intentionally at the ROUND level, not the idea level: once a
    panel has been launched without prior art, every idea it produces is
    ungrounded and rejecting them one at a time wastes the whole round's spend.
    Checking first costs nothing.
    """
    if not corpus:
        return False, ("no prior art retrieved: run retrieval (dobby context / "
                       "dobby research plan) before ideation — ungrounded "
                       "ideation reliably produces novel-sounding, "
                       "low-feasibility output")
    verified = sum(1 for e in corpus if e.verified)
    if verified == 0:
        return True, (f"{len(corpus)} evidence items, none VERIFIED: ideas will "
                      "be grounded in unverified claims; treat all output as "
                      "hypotheses")
    return True, f"{len(corpus)} evidence items ({verified} verified)"


def specificity(idea: Idea) -> float:
    """0..1 — how much concrete mechanism the idea names.

    Combines three cheap signals, each of which a vague idea fails and a
    concrete one passes: mechanism vocabulary, presence of a code-like
    identifier, and numeric quantities. None alone is sufficient — prose can
    mention "the config file" without proposing anything — so the score is the
    mean, and the threshold in `assess` is set where all three must contribute.
    """
    body = idea.body.lower()
    toks = tokens(idea.body)
    if len(toks) < _MIN_BODY_TOKENS:
        return 0.0
    mechanism = sum(1 for m in _MECHANISM_MARKERS if m in body)
    mechanism_score = min(1.0, mechanism / 3.0)
    # A path, dotted name, or snake/camel identifier: strong evidence the idea
    # points at something that exists rather than at a category of thing.
    identifier = 1.0 if re.search(
        r"[\w/]+\.(py|ts|js|json|yaml|md|toml)\b|\b\w+\.\w+\(|\b\w+_\w+\b",
        idea.body) else 0.0
    numeric = 1.0 if re.search(r"\b\d+(\.\d+)?\s*(%|ms|s\b|x\b|kb|mb|gb|tokens?)",
                               body) else 0.0
    # NOT rounded: this value is compared against MIN_SPECIFICITY, and rounding
    # 1/3 to 0.3333 would put it just below a threshold of exactly 1/3 — one
    # signal firing fully would then fail the "at least one signal" rule.
    # Rounding happens at display time only.
    return (mechanism_score + identifier + numeric) / 3.0


def groundedness(idea: Idea, corpus: Sequence[Evidence]) -> tuple[float, list[str]]:
    """0..1 — fraction of the idea's anchors that resolve to real evidence.

    An unresolvable anchor is the fabricated-citation failure in miniature: the
    idea *looks* grounded because it cites something, and the citation does not
    exist. Resolving every id against the corpus is what turns the appearance of
    grounding into the fact of it.
    """
    if not idea.evidence_ids:
        return 0.0, []
    known = {e.id for e in corpus}
    unknown = [eid for eid in idea.evidence_ids if eid not in known]
    resolved = len(idea.evidence_ids) - len(unknown)
    return round(resolved / len(idea.evidence_ids), 4), unknown


def distance_from_prior_art(idea: Idea, corpus: Sequence[Evidence]) -> float:
    """0..1 — how much the idea ADDS beyond the evidence it cites.

    Structured imagination predicts the dominant failure here: an idea that
    imports its most accessible knowledge is a restatement of the retrieved
    material wearing the word "novel". Measured as the fraction of the idea's
    content tokens absent from its anchored evidence. High grounding with near-
    zero distance is a summary, not a proposal; that combination is exactly what
    `assess` rejects as RESTATEMENT.
    """
    idea_tokens = token_set(f"{idea.title} {idea.body}")
    if not idea_tokens:
        return 0.0
    anchored = {e.id: e for e in corpus}
    covered: set[str] = set()
    for eid in idea.evidence_ids:
        ev = anchored.get(eid)
        if ev is not None:
            covered |= ev.token_set()
    if not covered:
        # Nothing to compare against. Distance is undefined rather than maximal;
        # returning 1.0 would reward an unanchored idea with a perfect novelty
        # score, which is the opposite of the intent.
        return 0.0
    return round(len(idea_tokens - covered) / len(idea_tokens), 4)


def has_falsifiable_test(idea: Idea) -> bool:
    """Whether the idea says how it could be shown wrong."""
    test = idea.falsifiable_test.lower().strip()
    if len(tokens(test)) < 4:
        return False
    return any(m in test for m in _TEST_MARKERS)


def overclaims(idea: Idea) -> list[str]:
    """Superlatives present in the idea. Only meaningful when unsubstantiated."""
    body = f"{idea.title} {idea.body}".lower()
    return [s for s in _SUPERLATIVES if s in body]


def assess(idea: Idea, corpus: Sequence[Evidence], *,
           min_specificity: float = MIN_SPECIFICITY,
           min_distance: float = MIN_DISTANCE,
           require_test: bool = True) -> IdeaAssessment:
    """Accept or reject one idea, deterministically, naming every reason.

    Thresholds default to `MIN_SPECIFICITY` (one concreteness signal) and
    `MIN_DISTANCE` (30% novel vocabulary); see those constants for why each is
    set where it is. Both are parameters so a project that finds the defaults
    too permissive can tighten them — but tightening specificity above one full
    signal starts rejecting ideas that name a real file without a number, which
    is a common shape for a correct proposal.
    """
    reasons: list[str] = []
    notes: list[str] = []

    ground, unknown = groundedness(idea, corpus)
    if not idea.evidence_ids:
        reasons.append(NO_EVIDENCE)
    elif unknown:
        reasons.append(UNKNOWN_EVIDENCE)
        notes.append(f"unresolvable evidence ids: {unknown} — these do not "
                     f"exist in the retrieved corpus (fabricated citation)")

    spec = specificity(idea)
    if spec < min_specificity:
        reasons.append(VAGUE)
        notes.append(f"specificity {spec:.4f} < {min_specificity:.4f}: names no file, "
                     "identifier, or quantity — an intention, not a mechanism")

    if require_test and not has_falsifiable_test(idea):
        reasons.append(NO_TEST)
        notes.append("no falsifiable test: the idea cannot be shown wrong, so "
                     "review can only agree with it")

    dist = distance_from_prior_art(idea, corpus)
    if idea.evidence_ids and not unknown and dist < min_distance:
        reasons.append(RESTATEMENT)
        notes.append(f"distance from cited prior art {dist} < {min_distance}: "
                     "restates the evidence rather than extending it")

    supers = overclaims(idea)
    if supers and (not idea.evidence_ids or not has_falsifiable_test(idea)):
        reasons.append(OVERCLAIM)
        notes.append(f"superlatives with nothing to substantiate them: {supers}")

    anchored = {e.id: e for e in corpus}
    verified = sum(1 for eid in idea.evidence_ids
                   if anchored.get(eid) is not None and anchored[eid].verified)
    unverified = len([eid for eid in idea.evidence_ids
                      if eid in anchored]) - verified
    if verified == 0 and unverified > 0:
        notes.append("anchored only to UNVERIFIED evidence: the idea inherits "
                     "that uncertainty and must be reported as a hypothesis")

    return IdeaAssessment(
        idea=idea, accepted=not reasons, reasons=reasons,
        groundedness=ground, specificity=round(spec, 4),
        distance_from_prior_art=dist,
        anchored_verified=verified, anchored_unverified=unverified,
        notes=notes)


def gate(ideas: Sequence[Idea], corpus: Sequence[Evidence], **kwargs) -> dict:
    """Run the whole panel's output through the gate and summarize.

    Reports the rejection HISTOGRAM, not just a count. A panel failing mostly on
    NO_TEST needs its prompt changed; one failing mostly on UNKNOWN_EVIDENCE has
    a fabrication problem; one failing on RESTATEMENT needs different lenses.
    Those are three different fixes and a single pass rate cannot distinguish
    them.
    """
    ok, prior_art_note = has_prior_art(corpus)
    assessments = [assess(i, corpus, **kwargs) for i in ideas]
    accepted = [a for a in assessments if a.accepted]
    histogram: dict[str, int] = {}
    for a in assessments:
        for r in a.reasons:
            histogram[r] = histogram.get(r, 0) + 1
    return {
        "prior_art_available": ok,
        "prior_art_note": prior_art_note,
        "total": len(assessments),
        "accepted": len(accepted),
        "rejected": len(assessments) - len(accepted),
        "rejection_histogram": dict(sorted(histogram.items(),
                                           key=lambda kv: -kv[1])),
        "assessments": [a.to_dict() for a in assessments],
        "accepted_titles": [a.idea.title for a in accepted],
        # Stated plainly so a caller cannot mistake an empty accept list for a
        # panel that simply had nothing to say.
        "verdict": ("no ideas survived the grounding gate" if not accepted
                    else f"{len(accepted)}/{len(assessments)} ideas grounded, "
                         "specific, and falsifiable"),
    }


def explore_cycle(assessments: Sequence[IdeaAssessment]) -> dict:
    """Geneplore's EXPLORE step: what each rejected idea needs to become viable.

    Rejection is not the end of an idea in the Geneplore account — a pre-inventive
    structure is refined by exploring its implications and regenerating. So each
    rejection is returned as a concrete repair instruction that can be fed
    straight back into the next generation phase, which is cheaper and more
    productive than discarding the round and re-prompting from scratch.
    """
    repairs = {
        NO_EVIDENCE: "cite at least one evidence id from the retrieved corpus, "
                     "or withdraw the idea",
        UNKNOWN_EVIDENCE: "replace the fabricated id with a real one from the "
                          "corpus; do not invent identifiers",
        NO_TEST: "add one command or observation that would show this is WRONG",
        VAGUE: "name the specific file, function, or quantity you would change",
        RESTATEMENT: "state what this ADDS beyond the cited evidence, or drop it",
        OVERCLAIM: "remove the magnitude claim or supply the measurement",
    }
    out = []
    for a in assessments:
        if a.accepted:
            continue
        out.append({
            "title": a.idea.title,
            "author": a.idea.author,
            "lens": a.idea.lens,
            "reasons": a.reasons,
            "repairs": [repairs[r] for r in a.reasons if r in repairs],
        })
    return {"needs_repair": len(out), "items": out,
            "guidance": "feed these repairs back into a second generation "
                        "phase (Geneplore generate→explore cycle) rather than "
                        "re-prompting the panel from scratch"}
