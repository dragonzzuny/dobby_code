"""Detect the prose signature that makes generated text read as generated.

The problem is not vocabulary, it is uniformity
-----------------------------------------------
The obvious approach — ban a list of words — fails, because the words are not
the tell. Human writing contains "however" and "furthermore" too. What marks
generated prose is **regularity**: sentences that cluster around one length,
clauses stacked in the same shape, a comma after every connective, hedges
layered until nothing is asserted, and lists that arrive in threes because three
sounds complete.

So the primary signals here are *distributional*. Mean sentence length says
little; the **standard deviation** says a great deal, because a human writing
naturally produces a five-word sentence next to a thirty-word one and a model
does not. A detector built only on a banned-phrase list flags a careful human
and misses a fluent model.

Severity, and why one occurrence can be enough
----------------------------------------------
Three tiers, following the taxonomy in the `im-not-ai` / Humanize-KR project
(MIT licensed; taxonomy adopted, code not vendored):

- **S1 deterministic** — one occurrence is sufficient evidence. A comma after a
  Korean connective ending is not a stylistic choice; it is a translation
  artifact that native writers do not produce.
- **S2 strong** — meaningful at three or more occurrences. Any single hedge is
  fine; a hedge in every paragraph is a voice.
- **S3 weak** — only counts when it overlaps other signals. Em dashes are
  legitimate punctuation, and flagging them alone would flag good writing.

Rewriting is bounded, and the bound is the point
------------------------------------------------
A rewrite that changes more than half the text has not edited the author's
writing, it has replaced it with the rewriter's. `rewrite_budget` therefore
declares a target change rate of ≤30% and an abort above 50%, so a "humanizing"
pass cannot quietly become ghostwriting.

This module detects and instructs. It does not rewrite: that needs a model, and
inventing a heuristic paraphraser here would be the lossy step the whole
discipline exists to prevent.
"""

from __future__ import annotations

import dataclasses
import re
import statistics
from collections.abc import Sequence

S1 = "S1"   # one occurrence is enough
S2 = "S2"   # meaningful at 3+
S3 = "S3"   # only in overlap with others

#: English phrases that mark generated prose. Each is a *connective or framing*
#: habit rather than a content word, because content words are not the tell.
_EN_PHRASES: dict[str, str] = {
    "it's worth noting": S2,
    "it is worth noting": S2,
    "it's important to note": S2,
    "it is important to note": S2,
    "that being said": S2,
    "at the end of the day": S2,
    "when it comes to": S2,
    "in today's": S2,
    "in the realm of": S1,
    "in the ever-evolving": S1,
    "delve into": S1,
    "delving into": S1,
    "navigate the complexities": S1,
    "a testament to": S1,
    "plays a crucial role": S1,
    "plays a vital role": S1,
    "it's not just": S2,
    "not only that": S2,
    "let's dive in": S2,
    "unlock the potential": S1,
    "in conclusion": S2,
    "to summarize": S2,
    "furthermore": S3,
    "moreover": S3,
    "additionally": S3,
    "however, it": S3,
}

#: Korean signals. Weighted from the Humanize-KR taxonomy: translationese and
#: connective-comma habits are the deterministic ones because native writing does
#: not produce them.
_KO_PHRASES: dict[str, str] = {
    "결론적으로": S2,
    "시사하는 바가 크다": S1,
    "~에 있어서": S1,
    "에 있어서": S1,
    "를 통해": S3,
    "을 통해": S3,
    "이라고 할 수 있다": S2,
    "라고 할 수 있다": S2,
    "할 수 있을 것으로 보인다": S1,
    "될 것으로 예상된다": S2,
    "혁신적": S2,
    "획기적": S2,
    "매우 중요하다": S2,
    "다양한": S3,
    "첫째": S3,
    "둘째": S3,
    "핵심적인 역할": S1,
    "귀추가 주목된다": S1,
}

#: Korean connective endings. A comma directly after one of these is the S1
#: translation artifact: Korean grammar already marks the clause boundary, so the
#: comma is imported English punctuation.
_KO_CONNECTIVE_ENDINGS = ("하고", "이고", "지만", "는데", "으며", "하며",
                          "면서", "라서", "어서", "아서", "니까", "거나")

#: Hedges. Individually fine, stacked they assert nothing.
_EN_HEDGES = ("may", "might", "could", "perhaps", "possibly", "arguably",
              "somewhat", "relatively", "generally", "typically", "often",
              "tends to", "seems to", "appears to", "in some cases")
_KO_HEDGES = ("수 있다", "것으로 보인다", "듯하다", "편이다", "경향이 있다",
              "일 수도", "아마도", "대체로", "비교적")

#: Sentence-length standard deviation below this reads as machine-regular. Human
#: prose typically lands well above it; the threshold is set low so a genuinely
#: terse, deliberate writer is not flagged.
UNIFORMITY_STDEV_FLOOR = 5.0

#: Commas per sentence above this is the comma habit the user actually notices.
COMMA_PER_SENTENCE_CEILING = 1.6

#: Mean words per sentence above this is the pedantic-length signal.
LONG_SENTENCE_CEILING = 24.0

#: Rewrite bounds. Above the abort rate a "humanizing" pass is ghostwriting.
REWRITE_TARGET_RATE = 0.30
REWRITE_ABORT_RATE = 0.50

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。？！])\s+|\n{2,}")
_WORD = re.compile(r"[\w']+", re.UNICODE)


@dataclasses.dataclass
class Signal:
    """One detected pattern."""

    code: str
    severity: str
    detail: str
    count: int
    fix: str
    samples: list[str] = dataclasses.field(default_factory=list)

    def counts(self) -> bool:
        """Whether this signal is strong enough to act on by itself."""
        if self.severity == S1:
            return self.count >= 1
        if self.severity == S2:
            return self.count >= 3
        return False      # S3 never counts alone

    def to_dict(self) -> dict:
        return dataclasses.asdict(self) | {"acts_alone": self.counts()}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text or "") if s.strip()]


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def measure(text: str) -> dict:
    """Distributional statistics — the signals that a phrase list cannot see."""
    sents = sentences(text)
    if not sents:
        return {"sentences": 0}
    lengths = [len(_words(s)) for s in sents]
    commas = text.count(",") + text.count("，") + text.count("、")
    return {
        "sentences": len(sents),
        "words": sum(lengths),
        "mean_sentence_words": round(statistics.mean(lengths), 1),
        # The headline number. Uniformity, not length, is what reads as machine.
        "sentence_stdev": (round(statistics.stdev(lengths), 2)
                           if len(lengths) > 1 else 0.0),
        "shortest": min(lengths),
        "longest": max(lengths),
        "commas": commas,
        "commas_per_sentence": round(commas / len(sents), 2),
        "em_dashes": text.count("—") + text.count("–"),
        "semicolons": text.count(";"),
        "bullets": len(re.findall(r"^\s*[-*•]\s+", text, re.MULTILINE)),
        "bold_runs": len(re.findall(r"\*\*[^*]+\*\*", text)),
    }


def _find_phrases(text: str, table: dict[str, str]) -> list[Signal]:
    low = text.lower()
    out = []
    for phrase, severity in table.items():
        count = low.count(phrase.lower())
        if count:
            out.append(Signal(
                code=f"phrase:{phrase}", severity=severity,
                detail=f"{count}x {phrase!r}", count=count,
                fix=f"delete {phrase!r} or replace it with the specific claim "
                    "it is standing in for",
                samples=[phrase]))
    return out


def _connective_commas(text: str) -> Signal | None:
    """Korean: a comma directly after a connective ending. S1.

    Korean marks the clause boundary morphologically, so the comma is imported
    English punctuation and native writers do not produce it. This is the single
    highest-precision Korean signal in the taxonomy.
    """
    hits = []
    for ending in _KO_CONNECTIVE_ENDINGS:
        for match in re.finditer(re.escape(ending) + r"\s*,", text):
            hits.append(match.group(0))
    if not hits:
        return None
    return Signal(
        code="ko:connective_comma", severity=S1,
        detail=f"{len(hits)} comma(s) directly after a connective ending "
               f"({', '.join(sorted(set(hits))[:4])})",
        count=len(hits),
        fix="delete the comma. Korean already marks the clause boundary; the "
            "comma is imported English punctuation",
        samples=sorted(set(hits))[:4])


def _hedge_stacks(text: str) -> Signal | None:
    """Two or more hedges in one sentence. Individually fine, stacked they
    assert nothing — and a sentence that asserts nothing cannot be wrong, which
    is why models produce them."""
    stacked = []
    for sent in sentences(text):
        low = sent.lower()
        n = sum(1 for h in _EN_HEDGES if h in low)
        n += sum(1 for h in _KO_HEDGES if h in sent)
        if n >= 2:
            stacked.append(sent[:90])
    if not stacked:
        return None
    return Signal(
        code="hedge_stack", severity=S2,
        detail=f"{len(stacked)} sentence(s) carry two or more hedges",
        count=len(stacked),
        fix="keep at most one hedge per sentence, or state the claim and its "
            "condition separately",
        samples=stacked[:3])


def _rule_of_three(text: str) -> Signal | None:
    """Three-item lists arriving in a row. Three sounds complete, so models
    produce it whether or not the subject has three parts."""
    triples = re.findall(r"\b\w+(?:\s+\w+){0,2},\s+\w+(?:\s+\w+){0,2},\s+and\s+"
                         r"\w+(?:\s+\w+){0,2}\b", text, re.IGNORECASE)
    if len(triples) < 2:
        return None
    return Signal(
        code="rule_of_three", severity=S2,
        detail=f"{len(triples)} three-item lists — a cadence, not a count",
        count=len(triples),
        fix="use the number of items the subject actually has; two or four is "
            "fine and reads as observed rather than composed",
        samples=[t[:70] for t in triples[:3]])


def analyze(text: str) -> dict:
    """Detect the generated-prose signature, with distributional signals first."""
    stats = measure(text)
    if not stats["sentences"]:
        return {"signals": [], "stats": stats, "verdict": "empty"}

    signals: list[Signal] = []

    if stats["sentences"] >= 4 and stats["sentence_stdev"] < UNIFORMITY_STDEV_FLOOR:
        signals.append(Signal(
            code="uniform_sentence_length", severity=S1,
            detail=f"sentence-length stdev {stats['sentence_stdev']} < "
                   f"{UNIFORMITY_STDEV_FLOOR} across {stats['sentences']} "
                   f"sentences (mean {stats['mean_sentence_words']} words)",
            count=1,
            fix="break the rhythm: put a short sentence next to a long one. "
                "This is the strongest single tell, and no vocabulary change "
                "fixes it"))

    if stats["commas_per_sentence"] > COMMA_PER_SENTENCE_CEILING:
        signals.append(Signal(
            code="comma_density", severity=S2,
            detail=f"{stats['commas_per_sentence']} commas per sentence "
                   f"(> {COMMA_PER_SENTENCE_CEILING})",
            count=stats["commas"],
            fix="split the clause into its own sentence instead of appending it "
                "with a comma"))

    if stats["mean_sentence_words"] > LONG_SENTENCE_CEILING:
        signals.append(Signal(
            code="long_sentences", severity=S2,
            detail=f"mean {stats['mean_sentence_words']} words per sentence "
                   f"(> {LONG_SENTENCE_CEILING})",
            count=stats["sentences"],
            fix="cut the subordinate clause that carries no new information"))

    if stats["em_dashes"] and stats["sentences"]:
        rate = stats["em_dashes"] / stats["sentences"]
        if rate > 0.25:
            signals.append(Signal(
                code="em_dash_rate", severity=S3,
                detail=f"{stats['em_dashes']} em dashes across "
                       f"{stats['sentences']} sentences ({rate:.0%})",
                count=stats["em_dashes"],
                fix="a comma, a colon, or a full stop carries most of these"))

    for signal in (_connective_commas(text), _hedge_stacks(text),
                   _rule_of_three(text)):
        if signal:
            signals.append(signal)

    signals.extend(_find_phrases(text, _EN_PHRASES))
    signals.extend(_find_phrases(text, _KO_PHRASES))

    acting = [s for s in signals if s.counts()]
    # S3 signals count only in overlap: three weak signals together are evidence,
    # one alone flags legitimate writing.
    weak = [s for s in signals if s.severity == S3]
    if len(weak) >= 3:
        acting.extend(weak)

    return {
        "stats": stats,
        "signals": [s.to_dict() for s in signals],
        "acting_signals": [s.code for s in acting],
        "score": len(acting),
        "verdict": _verdict(acting, stats),
        "top_fixes": [s.fix for s in sorted(
            acting, key=lambda s: {S1: 0, S2: 1, S3: 2}[s.severity])[:4]],
    }


def _verdict(acting: Sequence[Signal], stats: dict) -> str:
    if not acting:
        return ("no acting signal: the prose does not carry the generated "
                "signature this checks for")
    s1 = [s for s in acting if s.severity == S1]
    if s1:
        return (f"{len(acting)} acting signal(s), {len(s1)} deterministic "
                f"({', '.join(s.code for s in s1[:3])}). One S1 occurrence is "
                "sufficient evidence on its own")
    return (f"{len(acting)} acting signal(s), none deterministic — the pattern "
            "is present but each piece is individually defensible")


def rewrite_budget(original: str, rewritten: str) -> dict:
    """Bound a humanizing rewrite, and abort it when it becomes ghostwriting.

    Change is measured on word multisets rather than characters, so reordering a
    clause counts as a small edit and replacing the content counts as a large
    one — which matches what "did this stay the author's writing" actually means.
    """
    before = _words(original.lower())
    after = _words(rewritten.lower())
    if not before:
        return {"rate": 0.0, "verdict": "nothing to compare"}

    from collections import Counter
    kept = sum((Counter(before) & Counter(after)).values())
    rate = 1.0 - (kept / len(before))

    if rate > REWRITE_ABORT_RATE:
        verdict = (f"ABORT: {rate:.0%} of the original words are gone "
                   f"(> {REWRITE_ABORT_RATE:.0%}). This is not an edit of the "
                   "author's writing, it is a replacement of it")
        accepted = False
    elif rate > REWRITE_TARGET_RATE:
        verdict = (f"{rate:.0%} changed, over the {REWRITE_TARGET_RATE:.0%} "
                   "target: acceptable only if every edit is traceable to a "
                   "named signal")
        accepted = True
    else:
        verdict = f"{rate:.0%} changed — a surgical edit"
        accepted = True

    return {"rate": round(rate, 4), "words_before": len(before),
            "words_after": len(after), "words_kept": kept,
            "accepted": accepted, "verdict": verdict,
            "target_rate": REWRITE_TARGET_RATE,
            "abort_rate": REWRITE_ABORT_RATE}


def rewrite_instruction(report: dict) -> str:
    """The instruction for a model that will do the rewriting.

    Names the specific signals found rather than saying "make it sound human",
    which produces a different generated voice rather than fewer signals.
    """
    if not report.get("acting_signals"):
        return ("No acting signal was found. Do not rewrite: an unmotivated "
                "pass replaces one voice with another and changes meaning for "
                "no measured gain.")
    lines = [
        "Edit the text to remove the signals listed below. Rules:",
        f"- Change at most {int(REWRITE_TARGET_RATE * 100)}% of the words. "
        f"Above {int(REWRITE_ABORT_RATE * 100)}% the edit is rejected as a "
        "rewrite rather than an edit.",
        "- Preserve every fact, number, file path, identifier, and negation "
        "exactly. Style is the target; content is not.",
        "- Do not substitute a different set of stock phrases for these ones.",
        "",
        "Signals to remove:",
    ]
    for fix in report["top_fixes"]:
        lines.append(f"- {fix}")
    stats = report.get("stats", {})
    if stats.get("sentence_stdev", 99) < UNIFORMITY_STDEV_FLOOR:
        lines.append(
            f"- Sentence lengths cluster at {stats.get('mean_sentence_words')} "
            f"words (stdev {stats.get('sentence_stdev')}). Vary them "
            "deliberately: this is the tell that no word substitution fixes.")
    return "\n".join(lines)
