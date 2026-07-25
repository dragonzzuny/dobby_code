"""Ideation protocols as decorrelation devices.

The central idea
----------------
A fan-out of N agents given the SAME prompt produces correlated answers, because
they are N samples from broadly similar training distributions answering an
identically framed question. Hoping they disagree is not a mechanism. Structured
ideation methods — Nominal Group Technique, SCAMPER, Six Thinking Hats, dialectic
debate, Double Diamond — are mechanisms: each one hands its participants a
*structurally different* frame, so their outputs cannot collapse into one another
without a participant abandoning its assigned frame.

This reframing is what makes these human-workshop techniques worth implementing
here. They were designed to stop human groups from groupthinking, and the failure
they prevent (premature convergence under social pressure) is formally the same
one that multi-agent LLM panels exhibit as structural coupling. The intervention
transfers: assign different lenses, and measure the spread you got
(`swarm/diversity.py`) rather than assuming it.

Two properties are enforced throughout:

- **Divergence is isolated from convergence.** Every protocol has a phase where
  participants cannot see each other's work. NGT exists specifically because
  discussion-first groups converge before the minority view is stated; the same
  ordering is imposed on every protocol here.
- **Lenses are assigned, not chosen.** If an agent picks its own angle it picks
  the most obvious one, which is the same one its peers pick. Assignment is what
  guarantees coverage of the unobvious.

Protocols are DATA (lens text + phase order), so a project can add its own
domain lenses without touching orchestration code in `swarm/search.py`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

# --------------------------------------------------------------------------
# Lens catalogues.
#
# Each lens is a directive fragment appended to the shared task. Written in the
# imperative and deliberately narrow: a lens that says "think broadly" produces
# the default answer, which defeats the point.
# --------------------------------------------------------------------------

#: SCAMPER — seven transformation operators applied to an existing design.
#: Strongest when the task is "improve / rethink THIS", because each operator
#: forces a change the others cannot produce.
SCAMPER: dict[str, str] = {
    "substitute": "Replace one core component, material, rule, or actor with "
                  "something else. State what you swapped and what breaks.",
    "combine": "Merge this with an adjacent system, feature, or step so the two "
               "share machinery. State the merged interface.",
    "adapt": "Borrow a mechanism from a DIFFERENT domain that solves the same "
             "structural problem. Name the source domain explicitly.",
    "modify": "Change a magnitude — scale, frequency, granularity, ordering, "
              "or precision — by at least 10x in one direction.",
    "put_to_other_use": "Keep the artifact unchanged and find a second purpose "
                        "it already serves. Do not redesign it.",
    "eliminate": "Delete the component that seems most essential. Show what the "
                 "system looks like without it and what absorbs its job.",
    "reverse": "Invert the order, the direction of data flow, or the "
               "responsibility (who asks vs who is asked).",
}

#: Six Thinking Hats — de Bono. Separates modes of judgment that normally run
#: simultaneously and cancel each other out. Strongest for evaluating a proposal.
SIX_HATS: dict[str, str] = {
    "white": "Facts only. State what is measured or verifiable, and mark every "
             "unknown as unknown. No interpretation, no recommendation.",
    "red": "Reaction and intuition. What feels wrong or right here, stated as "
           "feeling, without needing to justify it.",
    "black": "Risk and failure. How does this break, get abused, or cost more "
             "than expected? Be specific about the failure path.",
    "yellow": "Value and upside. What genuinely works, and under what conditions "
              "does it work best?",
    "green": "Alternatives. Generate options nobody has proposed yet, including "
             "ones that sound unreasonable.",
    "blue": "Process. Is this the right question? What would need to be true to "
            "decide, and what is the cheapest test?",
}

#: Adversarial lenses for verification panels. Each verifier gets a different
#: failure MODE to hunt, because three identical skeptics find one bug three
#: times while three specialized ones find three bugs.
CRITIC_LENSES: dict[str, str] = {
    "correctness": "Find an input or state where this produces a wrong result. "
                   "Give the concrete input and the wrong output.",
    "security": "Find the untrusted input, the privileged action, and the path "
                "between them. Name the file and line.",
    "performance": "Find the operation whose cost grows fastest with input size "
                   "and state the growth rate.",
    "reproducibility": "Try to make the claim fail on a second run: hidden "
                       "state, ordering, clock, locale, or platform dependence.",
    "contract": "Find where this violates the interface its consumer expects — "
                "shape, units, encoding, error signalling.",
    "simplicity": "Find the part that could be deleted entirely without losing "
                  "required behaviour.",
}

#: Dialectic roles. Cheapest useful protocol at n=3, and the only one whose
#: convergence step is itself a distinct role rather than a merge.
DIALECTIC: dict[str, str] = {
    "thesis": "Argue FOR the proposal as strongly as the evidence allows. Cite "
              "the strongest concrete support.",
    "antithesis": "Argue AGAINST it as strongly as the evidence allows. You may "
                  "not concede; find the real weakness.",
    "synthesis": "You have both arguments. Identify which specific claims "
                 "survive both, which are refuted, and what remains untested.",
}


@dataclasses.dataclass(frozen=True)
class Phase:
    """One stage of a protocol."""

    name: str
    #: True when participants work WITHOUT seeing each other's output. The
    #: property that makes a protocol a decorrelation device rather than a
    #: discussion.
    isolated: bool
    #: How many participants act in this phase. None = all of them.
    participants: int | None
    directive: str


@dataclasses.dataclass(frozen=True)
class Protocol:
    """A named ideation process: ordered phases plus the lenses it assigns."""

    id: str
    display: str
    lenses: dict[str, str]
    phases: tuple[Phase, ...]
    #: Smallest panel at which the protocol means anything. Running dialectic
    #: with one agent, or NGT with one, is a single call wearing a costume.
    min_panel: int
    best_for: str

    def lens_names(self) -> list[str]:
        return list(self.lenses)

    def assign(self, n: int) -> list[tuple[str, str]]:
        """Assign lenses to `n` participants, most-distinct-first.

        When `n` exceeds the lens count the cycle repeats — but repeats are the
        LAST resort and are reported by `assignment_note`, because two agents on
        the same lens are correlated by construction and the caller should know
        its effective panel size is smaller than its billed one.
        """
        names = self.lens_names()
        if not names:
            return [(f"p{i}", "") for i in range(n)]
        return [(names[i % len(names)], self.lenses[names[i % len(names)]])
                for i in range(n)]

    def assignment_note(self, n: int) -> str | None:
        extra = n - len(self.lenses)
        if self.lenses and extra > 0:
            return (f"panel of {n} exceeds {len(self.lenses)} distinct lenses; "
                    f"{extra} agent(s) reuse a lens and are correlated with "
                    f"their twin by construction — effective distinct frames: "
                    f"{len(self.lenses)}")
        return None


# --------------------------------------------------------------------------
# Protocol definitions.
# --------------------------------------------------------------------------

NGT = Protocol(
    id="ngt",
    display="Nominal Group Technique",
    # No lens catalogue: NGT's decorrelation comes purely from the isolation of
    # its first phase, which is what makes it the right default. It composes
    # with any other protocol's lenses rather than competing with them.
    lenses={},
    phases=(
        Phase("silent_generation", isolated=True, participants=None,
              directive="Write your complete answer alone. You will not see "
                        "any other answer before submitting. Do not hedge "
                        "toward a consensus you cannot see."),
        Phase("round_robin", isolated=False, participants=None,
              directive="Here are all answers, unattributed. State only what "
                        "you would now ADD or RETRACT, and why. Do not restate "
                        "agreement."),
        Phase("rank", isolated=True, participants=None,
              directive="Rank the collected items by expected impact. Rank "
                        "alone; do not discuss the ranking."),
    ),
    min_panel=3,
    best_for="the default wrapper for any panel: cheapest reliable defence "
             "against premature convergence, since it changes ordering only",
)

DOUBLE_DIAMOND = Protocol(
    id="double_diamond",
    display="Double Diamond (Discover / Define / Develop / Deliver)",
    lenses=SCAMPER,
    phases=(
        Phase("discover", isolated=True, participants=None,
              directive="Diverge on the PROBLEM. Do not propose solutions. "
                        "Surface constraints, actors, and failure histories."),
        Phase("define", isolated=False, participants=1,
              directive="Converge to ONE problem statement, in one sentence, "
                        "that the discovered evidence actually supports."),
        Phase("develop", isolated=True, participants=None,
              directive="Diverge on SOLUTIONS to the defined problem only. "
                        "Apply your assigned lens; ignore the other lenses."),
        Phase("deliver", isolated=False, participants=1,
              directive="Converge to one implementable plan, naming what was "
                        "rejected and why."),
    ),
    min_panel=3,
    best_for="open-ended design where the problem statement is itself unsettled "
             "— the two-diamond shape stops solutioning a mis-framed problem",
)

SIX_HATS_PROTOCOL = Protocol(
    id="six_hats",
    display="Six Thinking Hats",
    lenses=SIX_HATS,
    phases=(
        Phase("parallel_hats", isolated=True, participants=None,
              directive="Answer strictly in your assigned mode. Staying in mode "
                        "matters more than being balanced — balance is the "
                        "panel's job, not yours."),
        Phase("blue_synthesis", isolated=False, participants=1,
              directive="Wearing the blue hat, integrate all modes into a "
                        "decision, and state which mode drove it."),
    ),
    min_panel=4,
    best_for="evaluating one concrete proposal, where a single agent would blend "
             "optimism and risk into unfalsifiable mush",
)

DIALECTIC_PROTOCOL = Protocol(
    id="dialectic",
    display="Dialectic (thesis / antithesis / synthesis)",
    lenses=DIALECTIC,
    phases=(
        Phase("positions", isolated=True, participants=2,
              directive="Argue your assigned side at full strength, alone."),
        Phase("synthesis", isolated=False, participants=1,
              directive="Judge both arguments on evidence, not on tone or "
                        "confidence. Name what remains untested."),
    ),
    min_panel=3,
    best_for="binary or near-binary decisions, and verifying a single contested "
             "claim at the lowest cost that still yields real opposition",
)

ADVERSARIAL = Protocol(
    id="adversarial",
    display="Perspective-diverse adversarial verification",
    lenses=CRITIC_LENSES,
    phases=(
        Phase("refute", isolated=True, participants=None,
              directive="Try to REFUTE the claim through your assigned failure "
                        "mode. Default to 'refuted' when you cannot construct a "
                        "concrete case that it holds."),
        Phase("adjudicate", isolated=False, participants=1,
              directive="Given the refutation attempts, decide whether the claim "
                        "survives. A claim survives only with a concrete "
                        "supporting case, never by absence of objection."),
    ),
    min_panel=3,
    best_for="checking a finding before acting on it; the assigned failure modes "
             "are what stop three skeptics from finding the same bug three times",
)

PROTOCOLS: dict[str, Protocol] = {
    p.id: p for p in (NGT, DOUBLE_DIAMOND, SIX_HATS_PROTOCOL,
                      DIALECTIC_PROTOCOL, ADVERSARIAL)
}


def get(protocol_id: str) -> Protocol:
    try:
        return PROTOCOLS[protocol_id]
    except KeyError:
        raise KeyError(f"unknown protocol {protocol_id!r}; "
                       f"known: {sorted(PROTOCOLS)}") from None


def recommend(task: str, panel_size: int) -> str:
    """Pick a protocol from the task's shape and the panel actually available.

    Keyword-based and intentionally simple: this is a DEFAULT, overridable at
    every call site. The panel-size clamp matters more than the keywords — a
    protocol below its `min_panel` is theatre, so a small panel is routed to
    dialectic (which works at 3) or to plain NGT rather than to a six-lens
    process that would silently reuse lenses.
    """
    text = task.lower()
    if panel_size < 3:
        return "ngt"

    verify = ("verify", "check", "review", "audit", "confirm", "validate",
              "is it true", "bug", "vulnerab", "regress")
    if any(k in text for k in verify):
        return "adversarial"

    decide = ("should we", "choose", "decide", "which", "versus", " vs ",
              "trade-off", "tradeoff", "worth it")
    if any(k in text for k in decide):
        return "dialectic" if panel_size < 4 else "six_hats"

    design = ("design", "architect", "redesign", "improve", "rethink",
              "brainstorm", "idea", "explore", "options", "approach")
    if any(k in text for k in design):
        return "double_diamond" if panel_size >= 3 else "ngt"

    evaluate = ("evaluate", "assess", "critique", "pros and cons", "risk")
    if any(k in text for k in evaluate):
        return "six_hats" if panel_size >= 4 else "dialectic"

    return "ngt"


def build_prompts(protocol: Protocol, task: str, panel_size: int,
                  shared_context: str = "") -> list[dict]:
    """Concrete per-participant prompts for the protocol's FIRST (isolated) phase.

    Only the first phase is materialized here because later phases need the
    earlier phase's outputs, which the orchestrator in `swarm/search.py` holds.
    Returning plain dicts keeps this module free of any provider or subprocess
    dependency, so protocols stay testable without spending a token.
    """
    first = protocol.phases[0]
    limit = first.participants or panel_size
    assignments = protocol.assign(limit)
    prompts = []
    for idx, (lens_name, lens_text) in enumerate(assignments):
        parts = [f"TASK: {task}"]
        if shared_context:
            parts.append(f"CONTEXT:\n{shared_context}")
        parts.append(f"PROTOCOL: {protocol.display} — phase '{first.name}'")
        parts.append(f"YOUR INSTRUCTION: {first.directive}")
        if lens_text:
            parts.append(f"YOUR ASSIGNED LENS ({lens_name}): {lens_text}")
        if first.isolated:
            parts.append(
                "You are working in isolation. Other participants are answering "
                "the same task under different lenses; you cannot see them and "
                "must not try to guess or accommodate their answers.")
        prompts.append({
            "index": idx,
            "lens": lens_name,
            "phase": first.name,
            "isolated": first.isolated,
            "prompt": "\n\n".join(parts),
        })
    return prompts
