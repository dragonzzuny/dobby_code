"""Diversity measurement for a set of independently produced answers.

Why this module exists
----------------------
Running N agents does not give N opinions. Published analysis of multi-agent
ideation finds that interaction itself *contracts* exploration — dense
communication topologies converge prematurely, authority-shaped roles suppress
minority views, and adding agents yields diminishing marginal diversity. The
failure is named **structural coupling**: the group's answers collapse toward one
answer, while the orchestrator, seeing six confident and mutually agreeing
reports, reads that collapse as consensus and therefore as confidence.

That inversion — agreement produced by coupling being read as evidence — is the
specific thing this module exists to catch. It gives the orchestrator a number
for how much genuine spread a panel actually produced, so a fan-out can be
reported as "six answers, effectively one" when that is what happened.

What is measured, and what is NOT
---------------------------------
Everything here is **lexical**, computed with the standard library only. There
are no embeddings, matching the kit's existing retrieval decision (ADR-2): the
engine must run anywhere with `python3` + PyYAML and no model server. The
consequence is stated rather than hidden — two answers that say the same thing in
different words score as diverse, and two that differ only by a negation score as
similar. So these metrics are a **screen, not a judgment**: they reliably catch
the collapse case (near-identical text) and the wide-spread case, and they are
weak in the middle. Where a real semantic verdict is needed, `swarm/search.py`
escalates to an adjudicating model, which is expensive and therefore gated.

`effective_n` is the headline number: the answer to "how many independent
opinions did I actually buy?" It is derived from mean pairwise distance rather
than from agent count, so paying for six correlated agents reports honestly as
roughly one.
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Sequence

#: Tokens shorter than this carry little topical signal and inflate similarity
#: between unrelated texts (every answer contains "the", "is", "to").
_MIN_TOKEN_LEN = 3

#: Very common English/agent-report words that appear in essentially every
#: answer. Removing them keeps two unrelated answers from scoring as similar
#: purely because both are written as prose reports. Deliberately short: an
#: aggressive stop list would start deleting domain content.
_STOPWORDS = frozenset("""
the and for that this with are was you not but from have has had will would
can could should its it's there their they them then than when what which who
should does did doing done being been also into over under about above below
use used using make makes made need needs needed given give gives
""".split())

#: Unicode word characters. `\w` is Unicode-aware in Python 3, so this matches
#: Hangul, Han, Kana, Cyrillic, Greek, and accented Latin as well as ASCII.
#:
#: An earlier version of this file used `[a-z0-9_]+`, which matches NO Hangul —
#: so three completely unrelated Korean answers tokenized to nothing, scored a
#: mean pairwise distance of 0.0, and were reported as a COLLAPSED panel worth
#: 1.0 opinions. Every module downstream of this function inherited that: memory
#: routing, the grounding gate, claim verification, case retrieval. The engine's
#: own `core/kg.py` had handled Hangul from the start; this module regressed it.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

#: Scripts whose words are not separated the way Latin's are. Korean attaches
#: particles (파일 → 파일은 / 파일이 / 파일을), and Han/Kana run together with no
#: spaces at all, so whole-token matching reports two mentions of the same word
#: as unrelated. Character bigrams recover the shared stem without a
#: morphological analyser, which a stdlib-only kit cannot ship.
_CJK_RANGES = (
    (0xAC00, 0xD7A3),    # Hangul syllables
    (0x1100, 0x11FF),    # Hangul jamo
    (0x3040, 0x30FF),    # Hiragana + Katakana
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0x3400, 0x4DBF),    # CJK extension A
)

#: Minimum token length for scripts written with spaces. Shorter tokens are
#: mostly function words and inflate similarity between unrelated texts.
_MIN_TOKEN_LEN = 3

#: Minimum for CJK/Hangul, where a two-character token is routinely a full
#: content word (예산 "budget", 버그 "bug", 圧縮 "compression"). Applying the
#: Latin minimum here would discard exactly the content-bearing words.
_MIN_CJK_TOKEN_LEN = 2


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _has_cjk(token: str) -> bool:
    return any(_is_cjk_char(ch) for ch in token)


def tokens(text: str) -> list[str]:
    """Content tokens of `text`, lowercased, stopworded, order preserved.

    For CJK and Hangul tokens, character bigrams are emitted ALONGSIDE the whole
    token rather than instead of it. Both are needed: the whole token keeps
    `압축률` distinct from `압축기`, and the bigrams let `파일은` and `파일이`
    recognise each other as the same noun. Emitting only bigrams would make every
    text sharing a common syllable look related; emitting only whole tokens is
    the bug this replaced.

    Order is preserved because `distinct_ngrams` needs adjacency; set-based
    metrics discard it themselves.
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text.lower()):
        # A token can be MIXED — `build_snapshot에서`, `py의`, `tokens.py의`.
        # Bigramming the whole thing shreds the identifier into `bu`, `ui`,
        # `il`, `ld`, `d_`… which is worse than useless: any two snake_case
        # names then share bigrams and score as related. Split into script runs
        # and apply each rule where it belongs, so the identifier survives whole
        # and only the attached particle is bigrammed.
        for part in _split_scripts(raw):
            if _has_cjk(part):
                if len(part) >= _MIN_CJK_TOKEN_LEN:
                    out.append(part)
                if len(part) >= 3:
                    out.extend(part[i:i + 2] for i in range(len(part) - 1))
            elif len(part) >= _MIN_TOKEN_LEN and part not in _STOPWORDS:
                out.append(part)
    return out


def _split_scripts(token: str) -> list[str]:
    """Split a token at CJK/non-CJK boundaries, preserving order.

    `build_snapshot에서` → `['build_snapshot', '에서']`
    `파일path` → `['파일', 'path']`
    Pure tokens pass through as a single part, so the common case costs one
    scan and no allocation beyond the list.
    """
    if not token:
        return []
    parts: list[str] = []
    current = token[0]
    current_is_cjk = _is_cjk_char(token[0])
    for ch in token[1:]:
        ch_is_cjk = _is_cjk_char(ch)
        if ch_is_cjk == current_is_cjk:
            current += ch
        else:
            parts.append(current)
            current, current_is_cjk = ch, ch_is_cjk
    parts.append(current)
    return parts


def token_set(text: str) -> frozenset[str]:
    return frozenset(tokens(text))


def jaccard_distance(a: frozenset[str], b: frozenset[str]) -> float:
    """1 − |A∩B|/|A∪B|. Two empty texts are treated as identical (distance 0).

    Jaccard is chosen over cosine on raw counts because answer LENGTH varies
    wildly between providers — a terse local model and a verbose frontier model
    can make the same point, and a count-weighted metric would score them as
    different mostly because one repeated itself.
    """
    union = a | b
    if not union:
        return 0.0
    return 1.0 - (len(a & b) / len(union))


def mean_pairwise_distance(texts: Sequence[str]) -> float:
    """Average Jaccard distance over all unordered pairs. 0 for fewer than 2.

    This is the primary spread statistic. All pairs are used rather than a
    sample: panels here are small (2–8), so the exact value is cheap, and a
    sampled estimate would add variance to a number used for gating.
    """
    sets = [token_set(t) for t in texts]
    n = len(sets)
    if n < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += jaccard_distance(sets[i], sets[j])
            pairs += 1
    return total / pairs


def effective_n(texts: Sequence[str]) -> float:
    """How many *independent* answers the panel is worth.

    Defined as `1 + (n − 1) · MPD`, which is the honest linear reading of the
    two anchors that matter:

    - MPD = 0 (all answers identical) → 1.0. Six identical answers are one
      answer, regardless of how many providers were billed for it.
    - MPD = 1 (no shared content token) → n. Fully disjoint answers are worth
      their full count.

    Intermediate values interpolate. No exponential or entropy-based form is used
    because the metric's own inputs are a lexical proxy — a more elaborate
    functional form would imply precision the underlying signal does not carry.
    """
    n = len(texts)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0
    return 1.0 + (n - 1) * mean_pairwise_distance(texts)


def distinct_ngrams(texts: Sequence[str], n: int = 2) -> float:
    """Unique n-grams ÷ total n-grams across the panel. Lexical breadth.

    Complements MPD: MPD asks "are these answers different from each other?",
    distinct-n asks "does the panel as a whole use varied language, or does one
    phrasing dominate every answer?" A panel can score moderate MPD while every
    member repeats the same stock framing, which shows up here and not there.
    """
    grams: list[tuple[str, ...]] = []
    for text in texts:
        toks = tokens(text)
        grams.extend(tuple(toks[i:i + n]) for i in range(len(toks) - n + 1))
    if not grams:
        return 0.0
    return len(set(grams)) / len(grams)


def coverage(texts: Sequence[str]) -> int:
    """Size of the union of content tokens: total ground the panel touched."""
    covered: set[str] = set()
    for text in texts:
        covered |= token_set(text)
    return len(covered)


def redundant_pairs(texts: Sequence[str], threshold: float = 0.15
                    ) -> list[tuple[int, int, float]]:
    """Index pairs whose distance is below `threshold` — near-duplicate answers.

    Returned so a report can name WHICH agents duplicated each other instead of
    only stating that duplication occurred. The default 0.15 marks pairs sharing
    roughly 85% of their content vocabulary, which in practice means one answer
    restated rather than two answers agreeing.
    """
    sets = [token_set(t) for t in texts]
    out = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            d = jaccard_distance(sets[i], sets[j])
            if d < threshold:
                out.append((i, j, round(d, 4)))
    return out


@dataclasses.dataclass
class DiversityReport:
    """Everything a ledger needs to justify trusting (or distrusting) a panel."""

    n: int
    mean_pairwise_distance: float
    effective_n: float
    distinct_2gram: float
    coverage_tokens: int
    redundant_pairs: list[tuple[int, int, float]]
    labels: list[str]
    verdict: str
    advice: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


#: Below this MPD a panel is treated as collapsed: its members are restatements
#: of one another and a majority vote among them carries no more evidence than a
#: single call. Set at 0.25 because `redundant_pairs` already flags <0.15 as
#: near-duplicate; 0.25 is the point where spread is too small to survive the
#: lexical proxy's own noise.
COLLAPSE_MPD = 0.25

#: Above this, the panel disagrees so broadly that the members are likely
#: answering different questions — a prompt-clarity problem, not a healthy
#: spread. Worth surfacing because the fix is upstream (tighten the prompt),
#: not downstream (add more voters).
SCATTER_MPD = 0.85


def analyze(texts: Sequence[str], labels: Sequence[str] | None = None
            ) -> DiversityReport:
    """Score a panel and say what to do about it."""
    texts = list(texts)
    names = list(labels) if labels else [f"agent{i}" for i in range(len(texts))]
    if len(names) != len(texts):
        raise ValueError(f"labels ({len(names)}) must match texts ({len(texts)})")

    mpd = mean_pairwise_distance(texts)
    eff = effective_n(texts)
    dup = redundant_pairs(texts)

    if len(texts) < 2:
        verdict = "single"
        advice = ("one answer is not a panel: no independent check was "
                  "performed, so do not report agreement as corroboration")
    elif mpd < COLLAPSE_MPD:
        verdict = "collapsed"
        advice = ("structural coupling: answers are near-restatements, so "
                  f"{len(texts)} agents bought ~{eff:.1f} opinions. Re-run with "
                  "the independent (NGT) phase enforced, different providers, "
                  "or deliberately opposed role prompts")
    elif mpd > SCATTER_MPD:
        verdict = "scattered"
        advice = ("answers share almost no vocabulary, which usually means the "
                  "prompt admitted different readings. Tighten the task "
                  "statement before adding voters")
    else:
        verdict = "healthy"
        advice = (f"{len(texts)} agents produced ~{eff:.1f} independent "
                  "opinions; majority agreement here is meaningful evidence")

    return DiversityReport(
        n=len(texts),
        mean_pairwise_distance=round(mpd, 4),
        effective_n=round(eff, 3),
        distinct_2gram=round(distinct_ngrams(texts, 2), 4),
        coverage_tokens=coverage(texts),
        redundant_pairs=[(names.index(names[i]) if False else i, j, d)
                         for i, j, d in dup],
        labels=names,
        verdict=verdict,
        advice=advice,
    )


def coupling_ratio(before: Sequence[str], after: Sequence[str]) -> dict:
    """Measure how much a SHARING round contracted the panel's spread.

    Call with the independent-phase answers (`before`) and the post-discussion
    answers (`after`). The ratio `MPD_after / MPD_before` is the operational
    definition of structural coupling used here:

    - ratio < 1 → sharing narrowed the panel. Some narrowing is convergence on a
      correct answer; heavy narrowing is the collapse the literature warns about.
    - ratio ≈ 1 → members held their positions; disagreement is substantive.
    - ratio > 1 → sharing widened the panel, usually because one member raised a
      consideration the others had missed. This is the outcome worth paying for.

    `contraction` is reported as a plain proportion so a ledger can quote it
    without the reader having to invert a ratio in their head.
    """
    mpd_before = mean_pairwise_distance(before)
    mpd_after = mean_pairwise_distance(after)
    if mpd_before == 0.0:
        # Nothing to contract: the panel was already collapsed before sharing,
        # so a ratio would be undefined rather than 0 or infinite. Saying so is
        # more useful than emitting a number that invites misreading.
        return {"mpd_before": 0.0, "mpd_after": round(mpd_after, 4),
                "ratio": None, "contraction": None,
                "note": "panel was already collapsed before sharing; coupling "
                        "cannot be attributed to the sharing round"}
    ratio = mpd_after / mpd_before
    return {
        "mpd_before": round(mpd_before, 4),
        "mpd_after": round(mpd_after, 4),
        "ratio": round(ratio, 4),
        "contraction": round(max(0.0, 1.0 - ratio), 4),
        "coupled": ratio < 0.6,
        "note": ("sharing contracted spread by "
                 f"{max(0.0, 1.0 - ratio) * 100:.0f}%"),
    }


def entropy_of_votes(votes: Sequence[str]) -> float:
    """Shannon entropy (bits) of a categorical vote distribution.

    Used where members return a LABEL rather than prose — "real"/"not real",
    option A/B/C. 0 bits means unanimity, which for a decorrelated panel is
    strong evidence and for a coupled one means nothing; that is exactly why the
    prose metrics above must be reported alongside any vote tally.
    """
    if not votes:
        return 0.0
    counts: dict[str, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    total = len(votes)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())
