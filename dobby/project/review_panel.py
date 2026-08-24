"""Three critics on one change, and a refusal to call them three if they are one.

Why this exists
---------------
The loop's gate is deterministic — declared write set, then the project's own
smoke checks — and that is the right default: a check that can be run twice and
give the same answer is worth more than an opinion. But a deterministic gate
answers "did the declared checks pass", never "is this the change the item asked
for". Nothing in the loop asked that question, and the fan-out machinery that
could ask it (`providers/fanout.run_round`) had exactly one caller in the whole
repository: the hand-typed `dobby panel` subcommand.

What it does NOT do
-------------------
It does not decide. `ReviewVerdict` is data the caller reads, the same contract
`architecture.PlanSpec` documents for the architect: a critic that says REJECT
has not blocked a merge, it has recorded a reason the caller may act on. Wiring
a model into the position of gatekeeper would replace a check that is repeatable
with one that is not.

Independence is measured, not assumed
-------------------------------------
`resolve_panel` already refuses to fill a panel by repeating a provider. That
stops the crudest version of the failure. It does not stop three DIFFERENT
models producing one answer three times, which reads identically in a report and
is worth one opinion. So the round is scored with `swarm.diversity.analyze` and
the verdict carries `independent`. A caller that reports agreement as
corroboration without reading that field is making exactly the claim this module
exists to make checkable.

Read-only, and how the tree is watched
--------------------------------------
Critics may not write. `architecture.propose_via_provider` enforces the same
rule per call via `readonly.run_read_only`, which fingerprints the tree either
side of ONE invocation. That does not survive concurrency: three fingerprints
interleaved around three overlapping calls attribute nothing. Rather than give
up the parallelism the fan-out exists for, the fingerprint is taken around the
WHOLE round. The cost is attribution — a violation names the round, not the
critic — and the response is therefore to discard every answer in it. Losing
three opinions is the cheap half of that trade; keeping one from a process that
edited the repository is not.
"""

from __future__ import annotations

import dataclasses

from .readonly import ReadOnlyViolation, fingerprint

#: The catalog already routes this role: codex, claude, gemini, agy.
CRITIC_ROLE = "critic"

DEFAULT_PANEL_SIZE = 3

APPROVE = "APPROVE"
REJECT = "REJECT"
UNREADABLE = "UNREADABLE"

#: Below this, `swarm.diversity` says the answers collapsed toward one voice.
MIN_EFFECTIVE_N = 1.5


@dataclasses.dataclass(frozen=True)
class Vote:
    """One critic's answer. `verdict` is UNREADABLE when it did not answer."""

    provider: str
    verdict: str
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ReviewVerdict:
    """What the panel found. Data, never an instruction to obey."""

    approved: bool
    panel: tuple
    votes: tuple
    independent: bool
    diversity: dict | None
    note: str

    @property
    def rejections(self) -> list:
        return [v for v in self.votes if v.verdict == REJECT]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "panel": list(self.panel),
            "votes": [v.to_dict() for v in self.votes],
            "independent": self.independent,
            "diversity": self.diversity,
            "note": self.note,
        }


def build_review_prompt(diff_text: str, *, outcome: str,
                        acceptance_checks=()) -> str:
    """The question every critic is asked, verbatim and identically.

    Identical prompts are deliberate. The panel measures whether independent
    models converge; varying the prompt per critic would make disagreement
    uninterpretable — a difference of opinion and a difference of question would
    be indistinguishable in the result.
    """
    checks = "\n".join(f"  - {c}" for c in acceptance_checks)
    return "\n".join([
        "You are reviewing a change that has ALREADY passed its declared",
        "acceptance checks. Do not re-run them and do not report that they",
        "pass; that is known. Answer only the question they cannot answer:",
        "does this diff do what the work item asked for?",
        "",
        f"WORK ITEM: {outcome}",
        "",
        "DECLARED ACCEPTANCE CHECKS (already passing):",
        checks or "  (none declared)",
        "",
        "DIFF:",
        diff_text,
        "",
        "You may not edit any file. This is a read-only review and the tree is",
        "fingerprinted; a review that also changed the repository is discarded.",
        "",
        "Answer with JSON and nothing else:",
        '  {"verdict": "APPROVE" or "REJECT", "reason": "<one sentence>"}',
        "",
        "REJECT means the diff does not deliver the item as written — wrong",
        "behaviour, a requirement silently dropped, or a change that passes the",
        "checks by narrowing what they test. Passing checks is not a reason to",
        "APPROVE on its own.",
    ])


def _parse(text: str, provider: str) -> Vote:
    from ..runtime.workers import extract_json

    payload = extract_json(text or "")
    if not isinstance(payload, dict):
        return Vote(provider=provider, verdict=UNREADABLE,
                    error="answered in prose where JSON was required")
    verdict = str(payload.get("verdict", "")).strip().upper()
    reason = str(payload.get("reason", "")).strip()
    if verdict not in (APPROVE, REJECT):
        return Vote(provider=provider, verdict=UNREADABLE, reason=reason,
                    error=f"unknown verdict {payload.get('verdict')!r}")
    return Vote(provider=provider, verdict=verdict, reason=reason)


def _score(texts, labels):
    """Diversity of the answers, or None when it cannot be measured."""
    if len(texts) < 2:
        return None
    try:
        from ..swarm import analyze

        return analyze(texts, labels).to_dict()
    except Exception:                             # noqa: BLE001
        return None


def review_change(diff_text: str, *, root: str, outcome: str,
                  acceptance_checks=(), size: int = DEFAULT_PANEL_SIZE,
                  allow_network: bool = False, timeout_s: int | None = None,
                  panel=None, round_runner=None) -> ReviewVerdict:
    """Ask up to `size` DISTINCT providers whether the diff delivers the item.

    `round_runner` is the seam, mirroring `architecture.request_architecture`'s
    `propose` and `readonly.run_read_only`'s `runner`: it takes the AgentTask
    list and returns a FanoutRound, so every rule above is testable with no
    provider installed.

    Raises `ReadOnlyViolation` if the tree moved during the round. See the
    module docstring for why that discards all of it rather than one answer.
    """
    from ..providers import AgentTask
    from ..providers.detect import resolve_panel

    members = list(panel) if panel is not None else resolve_panel(
        CRITIC_ROLE, size, allow_network=allow_network)
    if not members:
        return ReviewVerdict(
            approved=False, panel=(), votes=(), independent=False,
            diversity=None,
            note=(f"no usable provider fills the {CRITIC_ROLE!r} role on this "
                  f"machine, so the change was not reviewed. This is not an "
                  f"approval and not a rejection: nobody looked"))

    prompt = build_review_prompt(diff_text, outcome=outcome,
                                 acceptance_checks=acceptance_checks)
    tasks = [AgentTask(provider_id=pid, prompt=prompt, timeout_s=timeout_s,
                       label=f"{pid}:{CRITIC_ROLE}") for pid in members]

    if round_runner is None:
        def round_runner(tasks):                  # noqa: ANN001
            from ..providers import run_round

            return run_round(tasks, cwd=root)

    before = fingerprint(root)
    round_ = round_runner(tasks)
    after = fingerprint(root)
    if before != after:
        raise ReadOnlyViolation(
            f"the {CRITIC_ROLE!r} panel ({', '.join(members)}) ran read-only "
            f"and {root} changed while it did ({before[:12]} -> {after[:12]}). "
            f"Every answer in the round is discarded: the round is "
            f"fingerprinted as a whole, so which critic wrote cannot be "
            f"established from here. Inspect the tree (`git status`) before "
            f"running anything else, and record the culprit against "
            f"`read_only_default` in providers/catalog.py once you know it")

    results = list(getattr(round_, "results", ()) or ())
    votes = []
    for index, pid in enumerate(members):
        result = results[index] if index < len(results) else None
        if result is None or not getattr(result, "ok", False):
            votes.append(Vote(provider=pid, verdict=UNREADABLE,
                              error=(getattr(result, "error", "")
                                     or "the provider produced no result")))
            continue
        votes.append(_parse(result.text, pid))

    answered = [v for v in votes if v.verdict in (APPROVE, REJECT)]
    rejects = [v for v in answered if v.verdict == REJECT]
    # Labels must be built from the SAME filtered list as the texts. Deriving
    # them from `members` instead put three labels against two texts the first
    # time a critic failed, `analyze` raised on the mismatch, and the verdict
    # reported `independent: true` off a measurement that never happened —
    # the exact assumption this module says it does not make.
    scored = [(r.provider, r.text) for r in results
              if r is not None and getattr(r, "ok", False)]
    diversity = _score([t for _, t in scored],
                       [f"{p}:{CRITIC_ROLE}" for p, _ in scored])
    effective = (diversity or {}).get("effective_n")
    if not answered:
        independent = False
    elif len(answered) < 2:
        # One opinion cannot be correlated with opinions nobody gave.
        independent = True
    elif effective is None:
        # Two or more answers whose diversity could not be measured. Reported
        # as not independent: unmeasured is not the same as fine, and this
        # module's whole claim is that independence is measured.
        independent = False
    else:
        independent = effective >= MIN_EFFECTIVE_N

    # A single REJECT blocks. The asymmetry is deliberate and it is about the
    # cost of being wrong, not about how many models were bought: a false
    # APPROVE ships a change that does not deliver the item, and a false REJECT
    # costs one more pass through a loop that was going to run anyway.
    approved = bool(answered) and not rejects

    if not answered:
        note = ("every critic failed or answered in prose; the change was not "
                "reviewed. Not an approval")
    elif rejects:
        note = (f"{len(rejects)} of {len(answered)} critic(s) rejected: "
                + "; ".join(f"{v.provider}: {v.reason}" for v in rejects))
    elif not independent and effective is None:
        note = (f"{len(answered)} critic(s) approved, but their diversity could "
                f"not be measured, so independence is unproven. Do not report "
                f"this as independent corroboration")
    elif not independent:
        note = (f"{len(answered)} critic(s) approved, but the answers scored "
                f"effective_n={effective} (< {MIN_EFFECTIVE_N}): they converged "
                f"on one answer and are worth roughly one opinion. Do not "
                f"report this as independent corroboration")
    else:
        note = f"{len(answered)} independent critic(s) approved"

    return ReviewVerdict(approved=approved, panel=tuple(members),
                         votes=tuple(votes), independent=independent,
                         diversity=diversity, note=note)
