"""Geneplore, actually cycled: generate, assess, repair, generate again.

WHAT WAS MISSING

`swarm/grounding.py` implements both halves of the Geneplore account. It
generates the assessment, and `explore_cycle` turns every rejection into a
concrete repair instruction. Then it returns them with a `guidance` string that
asks a human to "feed these repairs back into a second generation phase". So the
cycle was documented, the repairs were computed, and the loop was closed by hand
every time — which means in practice it usually was not closed at all, and a
panel's first-round output became its final output.

This module closes it. Nothing here re-decides what a good idea is: the accept
rule stays in `grounding.assess`, the repairs stay in `explore_cycle`. What this
adds is the part that is genuinely mechanical — carrying accepted ideas forward,
re-prompting only for the rejected ones, and knowing when to stop.

WHY IT REFUSES TO START WITHOUT PRIOR ART

AGENTS.md: "Never propose ideas before retrieving prior art." An empty corpus
makes `groundedness` vacuous — every idea is equally unanchored, so the gate
rejects everything for the same reason and the repairs all read "cite something",
round after round. Cycling on that burns provider calls to rediscover that the
retrieval step was skipped. It is refused up front and named as such.

WHY A ROUND THAT CHANGED NOTHING ENDS THE CYCLE

`project/loop.py` states the principle for work items: repeating something
unchanged is the one action guaranteed not to help. The same holds here, and it
is the failure mode a naive retry loop has — a generator that ignores the repair
instruction returns the same ideas, they fail the same way, and the loop spends
its whole budget confirming that. So each round's output is fingerprinted, and a
round that returns what the previous one already had stops the cycle with
NO_PROGRESS rather than with ROUNDS_EXHAUSTED. Those are different diagnoses:
one says the generator is not listening, the other says the problem was hard.

WHAT THE CALLER SUPPLIES

`generate` is a callable, not a provider id. `project/architecture.py` takes
`propose` the same way and for the same reason: the decision this module makes is
testable without spending anything, and a caller may drive it from a panel, a
single provider, or a fixture.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Callable

from ..swarm.grounding import (Evidence, Idea, assess, explore_cycle,
                               has_prior_art)

# -- stop reasons ------------------------------------------------------------
#: Enough ideas cleared the gate.
SATISFIED = "satisfied"
#: The round budget ran out with the floor unmet.
ROUNDS_EXHAUSTED = "rounds_exhausted"
#: A round returned nothing the previous round had not already produced.
NO_PROGRESS = "no_progress"
#: The generator returned no ideas at all.
GENERATOR_EMPTY = "generator_empty"
#: Refused before the first call: ideation was not grounded in anything.
NO_PRIOR_ART = "no_prior_art"

STOP_REASONS = (SATISFIED, ROUNDS_EXHAUSTED, NO_PROGRESS, GENERATOR_EMPTY,
                NO_PRIOR_ART)

#: A ceiling on the cycle, for the same reason `loop.DRAIN_CEILING` exists: a
#: generator that keeps producing new-but-rejected ideas would otherwise spend a
#: budget nobody chose.
ROUND_CEILING = 6


def fingerprint(idea: Idea) -> str:
    """Identity for progress detection: what an idea SAYS, not who said it.

    Lens and author are excluded deliberately. The same proposal reassigned to a
    different lens is the same proposal, and counting it as new is how a cycle
    reports progress it did not make.
    """
    body = f"{idea.title} {idea.body} {idea.falsifiable_test}"
    return hashlib.sha256(body.strip().lower().encode("utf-8")).hexdigest()[:16]


def repair_brief(repairs: dict, *, topic: str = "") -> str:
    """The next round's instruction, built from what the gate actually rejected.

    Names each idea and the specific repair for each of its reasons. A brief that
    only said "the last round was rejected, try again" would leave the generator
    to guess which of six failure modes applied, and the guess is usually
    "be more novel" — which is the direction that makes groundedness worse.
    """
    lines = []
    if topic:
        lines.append(f"TOPIC: {topic}")
    lines.append(
        "Your previous ideas were rejected by a structural gate. This is not a "
        "matter of taste: each repair below is the exact condition that failed. "
        "Repair these ideas — do not replace them with different ones, and do "
        "not resubmit them unchanged.")
    for item in repairs.get("items", []):
        lines.append("")
        lines.append(f"IDEA: {item['title']}")
        lines.append(f"  failed: {', '.join(item['reasons'])}")
        for repair in item["repairs"]:
            lines.append(f"  fix: {repair}")
    lines.append("")
    lines.append(
        "Return the repaired ideas in the same structured shape: title, body, "
        "evidence_ids drawn from the supplied corpus, falsifiable_test.")
    return "\n".join(lines)


#: The generation prompt. The corpus is inlined because an idea must anchor to an
#: id that exists, and a model asked to cite without being shown the citable set
#: invents ids — which `groundedness` then rejects as UNKNOWN_EVIDENCE, spending
#: a round to discover that the prompt was underspecified.
IDEA_PROMPT = """{instruction}

You may anchor an idea ONLY to these evidence ids. Inventing an id is detected
and rejected:

{corpus}

Return one JSON object and nothing else:

{{"ideas": [{{"title": "...", "body": "...", "evidence_ids": ["..."],
             "falsifiable_test": "...", "lens": "..."}}]}}

`body` must name a concrete file, module, or quantity. `falsifiable_test` must
be an observation or command that would show the idea is WRONG. Aim for
{count} ideas."""


def render_corpus(corpus: Sequence[Evidence]) -> str:
    return "\n".join(
        f"- {e.id}: {e.summary}"
        + (f" [{e.path}]" if e.path else "")
        + ("" if e.verified else " (UNVERIFIED)")
        for e in corpus) or "(none)"


def provider_generator(provider_id: str | None, corpus: Sequence[Evidence], *,
                       root: str = ".", allow_network: bool = False,
                       timeout_s: int | None = None, count: int = 3,
                       role: str = "architect"
                       ) -> Callable[[str, int], list[Idea]]:
    """A `generate` backed by a provider CLI, in a role that may not write.

    A provider that fails, times out, or answers in prose returns NO ideas
    rather than raising. That is deliberate: `refine` already has a stop reason
    for an empty round, and turning a provider hiccup into an exception would
    lose the transcript of the rounds that did work.

    A provider that EDITED THE TREE is the one exception and does raise. It is
    not a hiccup and it is not an empty round — it is a process that was asked
    for ideas and changed the repository instead, and continuing the cycle would
    generate the next round against a tree nobody measured. See
    `project/readonly.py`; this generator defaults to the `architect` role and
    inherited exactly the overstated guarantee that module was written to fix.
    """
    from ..providers.catalog import registry
    from ..providers.detect import resolve_role
    from ..runtime.workers import extract_json
    from .readonly import run_read_only

    resolved = provider_id or resolve_role(role, allow_network=allow_network)
    if not resolved:
        raise ValueError(
            f"no provider on this machine fills the {role!r} role; install one "
            f"or pass an explicit provider")
    spec = registry().get(resolved)
    rendered = render_corpus(corpus)

    def generate(instruction: str, index: int) -> list[Idea]:
        result = run_read_only(
            spec, IDEA_PROMPT.format(instruction=instruction, corpus=rendered,
                                     count=count),
            root=root, timeout_s=timeout_s, role=role)
        if not result.ok:
            return []
        payload = extract_json(result.text)
        if not isinstance(payload, dict):
            return []
        out = []
        for row in payload.get("ideas") or []:
            if not isinstance(row, dict):
                continue
            out.append(Idea(
                title=str(row.get("title", "")),
                body=str(row.get("body", "")),
                evidence_ids=tuple(str(e) for e in (row.get("evidence_ids")
                                                    or [])),
                falsifiable_test=str(row.get("falsifiable_test", "")),
                lens=str(row.get("lens", "")),
                author=resolved))
        return out

    generate.provider_id = resolved
    return generate


def refine(generate: Callable[[str, int], Sequence[Idea]],
           corpus: Sequence[Evidence], *, base_instruction: str,
           topic: str = "", rounds: int = 3, min_accepted: int = 1,
           assess_kwargs: dict | None = None) -> dict:
    """Cycle generation against the grounding gate until it clears, or stop and say why.

    `generate(instruction, round_index)` returns ideas. Round 0 receives
    `base_instruction`; every later round receives a brief built from the
    previous round's rejections.

    The return value carries every round, not just the last. A cycle reported as
    a final answer hides whether the gate was cleared on the first try or on the
    fourth after three identical failures, and those say different things about
    whether the result should be trusted.
    """
    kwargs = dict(assess_kwargs or {})
    ceiling = min(max(1, int(rounds)), ROUND_CEILING)

    ok, note = has_prior_art(corpus)
    if not ok:
        return {
            "stopped": NO_PRIOR_ART,
            "detail": note,
            "rounds": [],
            "accepted": [],
            "accepted_count": 0,
            "min_accepted": min_accepted,
        }

    accepted: list[Idea] = []
    accepted_ids: set[str] = set()
    seen: set[str] = set()
    transcript: list[dict] = []
    instruction = base_instruction
    stopped, detail = ROUNDS_EXHAUSTED, (
        f"{ceiling} round(s) produced {0} accepted idea(s)")

    for index in range(ceiling):
        produced = list(generate(instruction, index) or [])
        prints = [fingerprint(i) for i in produced]

        if not produced:
            stopped, detail = GENERATOR_EMPTY, (
                f"round {index} returned no ideas; there is nothing to assess "
                f"and nothing to repair")
            transcript.append({"round": index, "generated": 0, "new": 0,
                               "accepted_this_round": 0, "rejected": 0,
                               "rejection_histogram": {}})
            break

        fresh = [p for p in prints if p not in seen]
        if index and not fresh:
            stopped, detail = NO_PROGRESS, (
                f"round {index} returned {len(produced)} idea(s), none of which "
                f"differ from what earlier rounds already produced: the repair "
                f"instruction was not applied, so another round cannot help")
            transcript.append({"round": index, "generated": len(produced),
                               "new": 0, "accepted_this_round": 0,
                               "rejected": len(produced),
                               "rejection_histogram": {}})
            break
        seen.update(prints)

        assessments = [assess(i, corpus, **kwargs) for i in produced]
        histogram: dict[str, int] = {}
        gained = 0
        for a in assessments:
            for reason in a.reasons:
                histogram[reason] = histogram.get(reason, 0) + 1
            if a.accepted:
                key = fingerprint(a.idea)
                if key not in accepted_ids:
                    accepted_ids.add(key)
                    accepted.append(a.idea)
                    gained += 1

        transcript.append({
            "round": index,
            "generated": len(produced),
            "new": len(fresh),
            "accepted_this_round": gained,
            "rejected": sum(1 for a in assessments if not a.accepted),
            "rejection_histogram": dict(sorted(histogram.items(),
                                               key=lambda kv: -kv[1])),
        })

        if len(accepted) >= min_accepted:
            stopped, detail = SATISFIED, (
                f"{len(accepted)} idea(s) cleared the gate by round {index}")
            break

        rejected = [a for a in assessments if not a.accepted]
        repairs = explore_cycle(rejected)
        transcript[-1]["repairs_issued"] = repairs["needs_repair"]
        instruction = repair_brief(repairs, topic=topic)
        stopped, detail = ROUNDS_EXHAUSTED, (
            f"{ceiling} round(s) produced {len(accepted)} accepted idea(s), "
            f"fewer than the {min_accepted} required")

    return {
        "stopped": stopped,
        "detail": detail,
        "rounds": transcript,
        "accepted": [{"title": i.title, "body": i.body,
                      "evidence_ids": list(i.evidence_ids),
                      "falsifiable_test": i.falsifiable_test,
                      "lens": i.lens, "author": i.author} for i in accepted],
        "accepted_count": len(accepted),
        "min_accepted": min_accepted,
        # The cycle proves the ideas are grounded, specific and falsifiable. It
        # proves nothing about whether they are worth doing, and a caller reading
        # only `accepted_count` would infer otherwise.
        "unverified": ("the gate is structural: an accepted idea is anchored, "
                       "specific and falsifiable, which is not the same as "
                       "correct or valuable"),
    }
