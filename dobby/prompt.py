"""Compile a casually-stated request into a prompt an agent can execute.

The trade this module is actually making
----------------------------------------
"Efficient prompting" is used to mean two things that pull in opposite
directions: **fewer tokens** and **better results**. A well-specified prompt is
almost always *longer* than the casual sentence it came from, so a compiler
optimising for brevity produces exactly the vague instruction that fails.

The cost that dominates is neither: it is **retries**. A prompt missing its
acceptance criterion produces a plausible wrong answer, which costs the round
that produced it, the round that reviews it, and the round that corrects it.
Against three rounds, two hundred extra input tokens is free.

So this compiles for *specification*, reports the token cost of doing so, and
never claims the result is shorter. `compile_prompt` returns both numbers and
lets the caller see the trade rather than asserting it was worth making.

What it refuses to do
---------------------
**It does not guess.** An unresolved ambiguity becomes a listed question, not an
invented answer. This is the whole difference between a compiler and a
hallucination: "fix the login bug" does not name a file, and a compiler that
picks one has not specified the task, it has *changed* it — and the agent will
then confidently do the wrong work with a well-structured prompt.

The five slots
--------------
Derived from what agent instructions actually fail to state, in the order the
failures cost most:

1. **objective** — what must be true afterwards. Missing this is fatal: without
   it there is no way to tell done from stopped.
2. **acceptance** — the command or observation that proves it. Missing this is
   why "it should work now" gets reported as completion.
3. **scope** — what may be touched. Missing this is how a one-line fix becomes
   a refactor.
4. **context** — the facts the agent would otherwise guess at.
5. **output contract** — the shape the answer must take, so a caller can parse
   it without asking twice.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence

from .swarm.diversity import tokens as content_tokens
from .tokens import estimate_tokens

#: Pronouns and deictic references with no antecedent in the request itself.
#: These are the highest-value ambiguity signal: the speaker knows what "this"
#: means and the agent does not, and nothing in the text can supply it.
_DANGLING_REFERENTS = (
    "이거", "그거", "저거", "이것", "그것", "여기", "거기", "아까", "그때",
    "this", "that", "it", "these", "those", "there", "the thing", "the one",
    "same as before", "like before", "as usual",
)

#: Verbs that state an intention without a completion condition. "improve" has
#: no end state; "make the p95 under 200ms" does.
_UNBOUNDED_VERBS = (
    "개선", "최적화", "정리", "다듬", "손보", "좋게",
    "improve", "optimize", "optimise", "clean up", "tidy", "refactor",
    "enhance", "polish", "modernize", "modernise", "better", "fix up",
    "look at", "check out", "handle", "deal with", "sort out",
)

#: Markers that an acceptance criterion is present.
_ACCEPTANCE_MARKERS = (
    "test", "테스트", "pass", "통과", "assert", "expect", "verify", "검증",
    "measure", "측정", "benchmark", "under ", "less than", "at least",
    "should return", "should equal", "exit 0", "green", "reproduce",
)

#: Markers that scope is stated.
_SCOPE_MARKERS = (
    "only", "just", "만", "안에서", "within", "in the file", "in module",
    "do not touch", "건드리지", "leave", "without changing", "except",
)

#: File-ish and identifier-ish tokens, which are what turn a request into a
#: locatable one.
_LOCATOR_RE = re.compile(
    r"[\w./\\-]+\.(?:py|ts|tsx|js|mjs|json|ya?ml|toml|md|sql|sh|ps1)\b"
    r"|\b[a-z_][a-z0-9_]{2,}\.[a-z_][a-z0-9_]{2,}\b"
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b"
    r"|\b[A-Z][A-Z0-9_]{2,}\b")

#: A request shorter than this cannot contain five slots' worth of information,
#: whatever it says.
_MIN_TOKENS_FOR_A_SPEC = 8


@dataclasses.dataclass
class Gap:
    """One thing the request does not say, and what it costs."""

    slot: str
    detail: str
    question: str
    #: Rough number of wasted rounds if the agent guesses wrong here. Used to
    #: order the questions, so a caller asked for only one gets the expensive one.
    retry_cost: int = 1

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Compiled:
    """The compiled prompt, with the trade it made stated in numbers."""

    original: str
    prompt: str
    gaps: list[Gap]
    slots: dict[str, str]
    original_tokens: int
    compiled_tokens: int

    @property
    def specified(self) -> bool:
        """True when no gap would change what the agent does."""
        return not self.gaps

    def cost(self) -> dict:
        delta = self.compiled_tokens - self.original_tokens
        avoided = sum(g.retry_cost for g in self.gaps)
        return {
            "original_tokens": self.original_tokens,
            "compiled_tokens": self.compiled_tokens,
            "delta_tokens": delta,
            "grew_by_pct": (round(100 * delta / self.original_tokens, 1)
                            if self.original_tokens else None),
            "unresolved_gaps": len(self.gaps),
            "estimated_retry_rounds_at_risk": avoided,
            "note": (
                "the compiled prompt is LONGER, which is the intended trade: "
                f"{delta} extra input tokens against {avoided} round(s) at risk "
                "of being wasted on a guessed requirement. A compiler that "
                "optimised for brevity would produce the vague prompt that "
                "causes the retry"
                if delta > 0 else
                "the compiled prompt is not longer; the request was already "
                "specific enough that structuring it cost nothing"),
        }

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "specified": self.specified,
            "slots": self.slots,
            "gaps": [g.to_dict() for g in self.gaps],
            "cost": self.cost(),
        }


def find_gaps(request: str, *, context_known: Sequence[str] = ()) -> list[Gap]:
    """What the request does not say, ordered by what guessing wrong would cost.

    `context_known` lets a caller declare facts already established elsewhere
    (a ledger, a prior turn), so the compiler does not re-ask what the session
    already settled — re-asking a resolved question is its own kind of waste.
    """
    low = request.lower()
    # Drop empties FIRST. Callers assemble this list from optional slots, so it
    # routinely arrives as ['', '', '', ''] — a list that is falsy in intent and
    # truthy in Python. Testing the raw list suppressed the dangling-referent
    # gap whenever any slot was merely offered-and-blank, which made
    # `compile_prompt` disagree with `clarifying_question` about the same text.
    supplied = [c for c in context_known if c and c.strip()]
    known = " ".join(supplied).lower()
    gaps: list[Gap] = []

    def has(markers) -> bool:
        return any(m in low or m in known for m in markers)

    locators = _LOCATOR_RE.findall(request)

    # 1. objective — the most expensive thing to guess.
    if any(v in low for v in _UNBOUNDED_VERBS) and not has(_ACCEPTANCE_MARKERS):
        verb = next(v for v in _UNBOUNDED_VERBS if v in low)
        gaps.append(Gap(
            slot="objective",
            detail=f"'{verb}' names a direction, not an end state — there is no "
                   "way to tell done from stopped",
            question=f"What must be true for '{verb}' to be finished? Give the "
                     "state, not the activity.",
            retry_cost=3))

    # 2. acceptance — why "it should work now" gets reported as completion.
    if not has(_ACCEPTANCE_MARKERS):
        gaps.append(Gap(
            slot="acceptance",
            detail="no command or observation is named that would prove the "
                   "work succeeded",
            question="What command or observation proves this is done? A "
                     "producing command exiting 0 is not proof.",
            retry_cost=2))

    # 3. scope — how a one-line fix becomes a refactor.
    if not locators and not has(_SCOPE_MARKERS):
        gaps.append(Gap(
            slot="scope",
            detail="no file, module, or boundary is named, so the change "
                   "surface is unbounded",
            question="Which files or modules may be touched, and which must "
                     "not be?",
            retry_cost=2))

    # 4. dangling referents — the speaker knows, the text does not carry it.
    dangling = [r for r in _DANGLING_REFERENTS
                if re.search(rf"(?:^|[\s\W]){re.escape(r)}(?:$|[\s\W])", low)]
    if dangling and not locators and not supplied:
        gaps.append(Gap(
            slot="context",
            detail=f"unresolved reference(s) {dangling[:3]}: nothing in the "
                   "request says what they point at",
            question=f"What does {dangling[0]!r} refer to? Name it explicitly.",
            retry_cost=3))

    # 5. too short to be a specification at all.
    if len(content_tokens(request)) < _MIN_TOKENS_FOR_A_SPEC:
        gaps.append(Gap(
            slot="objective",
            detail=f"the request is {len(content_tokens(request))} content "
                   "tokens; it cannot contain a specification",
            question="Restate the request with the outcome, the file(s), and "
                     "the check that proves it.",
            retry_cost=3))

    # Highest retry cost first, so a caller who asks one question asks the
    # expensive one.
    gaps.sort(key=lambda g: (-g.retry_cost, g.slot))
    # Deduplicate by slot, keeping the costliest reason per slot.
    seen: set[str] = set()
    unique: list[Gap] = []
    for g in gaps:
        if g.slot in seen:
            continue
        seen.add(g.slot)
        unique.append(g)
    return unique


def compile_prompt(request: str, *,
                   objective: str = "", acceptance: str = "",
                   scope: str = "", context: Sequence[str] = (),
                   output_contract: str = "",
                   role: str = "", constraints: Sequence[str] = ()) -> Compiled:
    """Build a structured prompt from a request plus whatever is known.

    Every slot the caller supplies is used verbatim. Slots left empty are NOT
    invented — they become gaps, and the prompt says explicitly that they are
    unspecified. An agent told "the acceptance criterion was not stated" will
    ask; an agent given a fabricated one will confidently satisfy the wrong
    thing.
    """
    gaps = find_gaps(request, context_known=list(context) + [
        objective, acceptance, scope, output_contract])

    slots = {
        "objective": objective.strip(),
        "acceptance": acceptance.strip(),
        "scope": scope.strip(),
        "context": "\n".join(f"- {c}" for c in context if c.strip()),
        "output_contract": output_contract.strip(),
    }

    lines: list[str] = []
    if role:
        lines.append(f"ROLE: {role}")
    lines.append(f"REQUEST (verbatim): {request.strip()}")

    if slots["objective"]:
        lines.append(f"\nOBJECTIVE (what must be true afterwards):\n"
                     f"{slots['objective']}")
    if slots["acceptance"]:
        lines.append(f"\nACCEPTANCE (what proves it):\n{slots['acceptance']}")
    if slots["scope"]:
        lines.append(f"\nSCOPE (what may be touched):\n{slots['scope']}")
    if slots["context"]:
        lines.append(f"\nESTABLISHED FACTS (do not re-derive):\n"
                     f"{slots['context']}")
    if constraints:
        lines.append("\nCONSTRAINTS:\n"
                     + "\n".join(f"- {c}" for c in constraints))
    if slots["output_contract"]:
        lines.append(f"\nOUTPUT CONTRACT:\n{slots['output_contract']}")

    if gaps:
        # Naming the gaps IN the prompt is the mechanism. It converts a silent
        # assumption into a visible one, which is the only form an agent can act
        # correctly on.
        lines.append("\nUNSPECIFIED — do NOT guess these:")
        for g in gaps:
            lines.append(f"- {g.slot}: {g.detail}")
        lines.append(
            "\nIf any unspecified item changes what you would do, STOP and ask "
            "the single highest-impact question rather than proceeding on an "
            "assumption. If it does not change what you would do, proceed and "
            "record the assumption in your report.")

    prompt = "\n".join(lines)
    return Compiled(
        original=request,
        prompt=prompt,
        gaps=gaps,
        slots=slots,
        original_tokens=estimate_tokens(request),
        compiled_tokens=estimate_tokens(prompt))


def clarifying_question(request: str, **kwargs) -> dict:
    """The single question worth asking, or None if the request is specified.

    One question, not a list. A caller handed five questions answers them
    partially or not at all; a caller handed the one that would waste three
    rounds answers it. `find_gaps` orders by retry cost precisely so this can
    take the head.
    """
    gaps = find_gaps(request, **kwargs)
    if not gaps:
        return {"needed": False,
                "note": "the request names an outcome, a check, and a scope"}
    top = gaps[0]
    return {
        "needed": True,
        "slot": top.slot,
        "question": top.question,
        "why": top.detail,
        "rounds_at_risk": top.retry_cost,
        "other_gaps": [g.slot for g in gaps[1:]],
        "note": ("ask this one. A caller handed five questions answers them "
                 "partially; handed the expensive one, they answer it"),
    }
