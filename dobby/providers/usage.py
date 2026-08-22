"""Token and cost accounting, for the providers that will actually report it.

WHAT THIS CORRECTS

`runtime/metrics.py` returns `cost_per_verified_task` as null with the note "this
engine cannot see money: CLI providers do not report token usage and no price
table is configured". That was true when it was written and it is now false for
one provider. Measured this session:

    claude -p "Reply with exactly: OK" --output-format json --permission-mode plan

returns an envelope carrying `total_cost_usd`, `usage.input_tokens`,
`usage.output_tokens`, `usage.output_tokens_details.thinking_tokens`, and both
cache counters. The cost is the VENDOR's own figure, not a price table this
repository maintains, which is the difference between reporting a measurement and
reporting an estimate.

WHY IT IS OPT-IN AND OFF BY DEFAULT

`--output-format json` changes what the CLI writes to stdout. Every existing
caller of `run_provider` reads `result.text` as the answer, and switching the
whole repository to a JSON envelope by default would turn every one of those into
a parser bug at once. So usage collection is requested per call, the envelope is
unwrapped back into `text`, and a caller that does not ask sees exactly what it
saw before.

WHY `usage_extra` LIVES ON THE SPEC

Beside `write_extra`, and for the same reason: it is the argv that puts one CLI
into a state, it differs per vendor, and an empty tuple means NOBODY HAS VERIFIED
HOW rather than "no flag needed". `write_extra` documents that lesson at length
after `swebench` hardcoded one CLI's flag and appended it to every provider. This
does not repeat it.

WHAT IS NOT MEASURED

Every provider whose `usage_extra` is empty. `codex exec --json` prints JSONL
events and may well carry usage; nobody here has run it and looked, so it reports
`None` and the caller says "not measured" rather than "zero". A zero would enter
a mean and move it.
"""

from __future__ import annotations

import dataclasses
import json

#: The envelope key that holds the actual answer when a CLI is asked for JSON.
#: Per provider, because there is no convention and guessing produces a caller
#: that silently treats a whole envelope as the model's reply.
RESULT_KEY = {"claude": "result"}


@dataclasses.dataclass(frozen=True)
class Usage:
    """What one provider call consumed, as the provider itself reported it.

    Every field is optional because providers report different subsets, and a
    missing field stays None. Filling it with 0 would make an unreported quantity
    indistinguishable from a measured absence — the distinction this whole
    harness is built on.
    """

    provider: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    #: The VENDOR's figure. This repository maintains no price table, and a cost
    #: computed from one would be an estimate wearing a measurement's clothes.
    cost_usd: float | None = None
    api_ms: int | None = None
    turns: int | None = None
    source: str = ""

    @property
    def billable_input(self) -> int | None:
        """Input plus cache creation — what a call actually costs on the way in.

        Cache READS are deliberately excluded: they are the cheap path and
        counting them as input would make a well-cached call look expensive.
        Cache CREATION is included because it is paid, and on a short prompt it
        dominates: a measured "reply OK" call reported 2 input tokens and 29,613
        cache-creation tokens.
        """
        parts = [t for t in (self.input_tokens, self.cache_creation_tokens)
                 if t is not None]
        return sum(parts) if parts else None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _int(value):
    return int(value) if isinstance(value, (int, float)) else None


def parse_claude(envelope: dict) -> Usage:
    usage = envelope.get("usage") or {}
    details = usage.get("output_tokens_details") or {}
    models = envelope.get("modelUsage") or {}
    model = next(iter(models), None)
    return Usage(
        provider="claude",
        model=model,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        thinking_tokens=_int(details.get("thinking_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
        cost_usd=(float(envelope["total_cost_usd"])
                  if isinstance(envelope.get("total_cost_usd"), (int, float))
                  else None),
        api_ms=_int(envelope.get("duration_api_ms")),
        turns=_int(envelope.get("num_turns")),
        source="claude --output-format json")


PARSERS = {"claude": parse_claude}


def unwrap(provider_id: str, text: str) -> tuple[str, Usage | None]:
    """`(answer, usage)` from a provider's structured envelope.

    A text that is not the expected envelope comes back UNCHANGED with no usage.
    That is the important half: a CLI that ignored the flag, a version that
    changed its output, or a crash that printed a traceback must degrade to "the
    answer, unmeasured" rather than to an exception or to an empty answer.
    """
    parser = PARSERS.get(provider_id)
    if parser is None or not text.strip():
        return text, None
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError):
        return text, None
    if not isinstance(envelope, dict):
        return text, None
    key = RESULT_KEY.get(provider_id)
    if key is None or key not in envelope:
        return text, None
    answer = envelope.get(key)
    return (answer if isinstance(answer, str) else text), parser(envelope)


def total(usages) -> dict:
    """Sum a sequence of `Usage`, keeping unmeasured separate from zero.

    `calls_measured` is reported beside every total so a reader can see the
    denominator. Three calls of which one reported usage produce a total that is
    a third of the truth, and a total without its denominator hides that.
    """
    rows = [u for u in usages if u is not None]
    fields = ("input_tokens", "output_tokens", "thinking_tokens",
              "cache_read_tokens", "cache_creation_tokens")
    out: dict = {"calls": len(list(usages)), "calls_measured": len(rows)}
    for name in fields:
        values = [getattr(u, name) for u in rows if getattr(u, name) is not None]
        out[name] = sum(values) if values else None
    costs = [u.cost_usd for u in rows if u.cost_usd is not None]
    out["cost_usd"] = round(sum(costs), 6) if costs else None
    out["note"] = ("null means no call reported that field; it does not mean "
                   "zero" if len(rows) < out["calls"] else "")
    return out
