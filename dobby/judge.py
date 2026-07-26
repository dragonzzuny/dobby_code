"""Model-based judgment for criteria no command can check — advisory only.

WHY THIS IS NOT A CHECK

`.dobby/ontology.json` states the rule this module has to obey:

    "model_assertion provenance is NEVER confidence=verified; verification
     requires a command/scan/eval_result source."

So a model verdict must never be able to turn an evaluation green. Before this
module existed, `model_judgment` criteria returned `passed=None` and the evaluator
put every non-None record into the set that decides PASS/FAIL — meaning the moment
a judge started answering True, its opinion would have counted exactly as much as
a test exit code. Wiring the adapter without splitting that bucket would have
quietly converted the strongest claim in the repository ("deterministic-first")
into a false one.

Judgments are therefore returned with `advisory=True`, and `Evaluator.evaluate`
keeps them out of the deterministic verdict. They appear in their own section,
with the provider that said it and the reply it gave. That is useful — the
criterion "the final message states the real verdict including anything NOT
verified" genuinely cannot be checked by a command — but it is evidence of a
different kind, and it is labelled as such.

THREE THINGS THIS REFUSES TO DO

1. **Run implicitly.** Judging costs money and reaches an external service.
   Nothing calls it unless a caller passes `judge=True`, the same stance
   `providers/run.py::probe` takes about the only other paid path.
2. **Judge its own author.** `exclude` is threaded to `resolve_role` so the
   provider that produced an artifact cannot grade it. `skills.py` already
   enforces proposer != approver for skills; the same reason applies here, and
   a second opinion from the same model is correlated with the first.
3. **Guess at an unparseable reply.** The reply format is fixed and checked
   exactly. A model that answers in prose gets `passed=None` and its raw text
   recorded, not a verdict inferred from whether the word "pass" appears
   somewhere. Lenient parsing is how a judge starts agreeing with everything.
"""

from __future__ import annotations

import re
import time

from .core.security import cap_output, redact_secrets

#: The reply contract. Deliberately rigid: the whole value of a judge is that its
#: answer is unambiguous, and a format that tolerates variation tolerates
#: hallucinated agreement.
VERDICT_LINE = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL|UNCLEAR)\s*$",
                          re.MULTILINE)

#: Model judgments never reach this. Kept as a named constant so the ceiling is
#: visible next to the ontology rule it implements.
MAX_MODEL_CONFIDENCE = 0.6

#: An artifact is evidence, not a payload. Anything longer is truncated with the
#: truncation stated in the prompt, so the judge knows it saw a fragment.
MAX_ARTIFACT_CHARS = 12_000

PROMPT = """You are grading one criterion. You did not write the work.

CRITERION: {description}

Answer in exactly this format and nothing else:

VERDICT: PASS or FAIL or UNCLEAR
WHY: one or two sentences quoting the specific text that decided it

Rules:
- PASS only if the artifact plainly satisfies the criterion.
- FAIL if it plainly does not.
- UNCLEAR if the artifact does not contain enough to tell. UNCLEAR is a correct
  answer and is preferred over a guess.
- Judge only the criterion above. Do not comment on anything else.
{truncation_note}
--- ARTIFACT BEGINS ---
{artifact}
--- ARTIFACT ENDS ---
"""


def build_prompt(criterion: dict, artifact: str) -> str:
    """Assemble the grading prompt, redacted and bounded."""
    safe = redact_secrets(artifact or "")
    truncated = len(safe) > MAX_ARTIFACT_CHARS
    body = cap_output(safe, MAX_ARTIFACT_CHARS)
    note = ("- The artifact below is TRUNCATED. If the answer depends on the "
            "missing part, reply UNCLEAR.\n" if truncated else "")
    return PROMPT.format(description=criterion.get("description", ""),
                         truncation_note=note, artifact=body)


def parse_verdict(text: str) -> tuple[bool | None, str, str]:
    """Return (passed, verdict_token, why).

    `passed` is None for UNCLEAR and for any reply that does not match the
    contract. The distinction between "the judge said it could not tell" and
    "the judge did not answer the question" is kept in the token.
    """
    if not text:
        return None, "NO_REPLY", ""
    match = VERDICT_LINE.search(text)
    if not match:
        return None, "UNPARSEABLE", ""
    token = match.group(1)
    why = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("WHY:"):
            why = stripped[4:].strip()
            break
    if token == "PASS":
        return True, token, why
    if token == "FAIL":
        return False, token, why
    return None, token, why


def judge_criterion(criterion: dict, artifact: str, *,
                    provider_id: str | None = None,
                    exclude: set[str] | None = None,
                    timeout_s: int = 180,
                    cwd: str | None = None) -> dict:
    """Ask one provider to grade one criterion. Never raises.

    Returns a record with `advisory=True` always, so a caller cannot mistake it
    for a measurement even if it ignores the rest.
    """
    from .providers import resolve_role, run_by_id

    started = time.monotonic()
    record = {
        "advisory": True,
        "kind": "model_judgment",
        "criterion": criterion.get("id"),
        "passed": None,
        "confidence": 0.0,
        "verdict_token": None,
        "judge_provider": None,
        "why": "",
        "evidence": None,
        "duration_s": 0.0,
    }

    chosen = provider_id or resolve_role("critic", exclude=exclude or set())
    if not chosen:
        record["evidence"] = (
            "NOT RUN: no provider available for the critic role"
            + (f" after excluding {sorted(exclude)}" if exclude else "")
            + ". A judge that is the author is not a second opinion.")
        return record
    record["judge_provider"] = chosen

    prompt = build_prompt(criterion, artifact)
    result = run_by_id(chosen, prompt, timeout_s=timeout_s, cwd=cwd)
    record["duration_s"] = round(time.monotonic() - started, 2)

    if not result.ok:
        record["evidence"] = f"NOT RUN: {chosen} failed: {result.error}"
        return record

    passed, token, why = parse_verdict(result.text)
    record.update(passed=passed, verdict_token=token, why=why)
    # Confidence is capped whatever the model says, and is zero when it did not
    # answer the question. There is no path here to 1.0.
    record["confidence"] = MAX_MODEL_CONFIDENCE if passed is not None else 0.0
    record["evidence"] = (
        f"{chosen} said {token}"
        + (f": {why}" if why else "")
        + f" [advisory: a model judgment is never verification"
          f" (.dobby/ontology.json)]"
        + ("" if token not in ("UNPARSEABLE", "NO_REPLY")
           else f"\nraw reply: {cap_output(result.text, 600)}"))
    return record
