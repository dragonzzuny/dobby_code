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

ALL THREE CLIs REPORT USAGE, AND NONE OF THEM AGREES ON ANYTHING

Probed 2026-08-23. The paragraph above used to end by saying codex "may well
carry usage; nobody here has run it and looked" — somebody has now looked, and
so has agy:

    claude   result envelope   input / output / thinking / cache read+create
                              AND `total_cost_usd`, the vendor's own figure
    agy      response envelope input / output / thinking / cache read
                              no cost, no cache-creation counter
    codex    JSONL events      input / output / reasoning / cached / cache-write
                              no cost, and no envelope at all

The names are mapped into one shape rather than passed through, because
`reasoning_output_tokens`, `thinking_tokens` and
`output_tokens_details.thinking_tokens` are the same quantity under three
spellings, and three fields meaning one thing is a comparison nobody can make.

WHAT IS STILL NOT MEASURED

**Money, for two of the three.** Only claude reports a cost. `cost_usd` stays
None for codex and agy rather than becoming zero, so no routing decision can
treat "unpriced" as "free" — which would send work to whichever provider is
least instrumented. Token counts are comparable across all three; prices are not.
"""

from __future__ import annotations

import dataclasses
import json

#: The envelope key that holds the actual answer when a CLI is asked for JSON.
#: Per provider, because there is no convention and guessing produces a caller
#: that silently treats a whole envelope as the model's reply.
#: Probed 2026-08-23: claude uses `result`, agy uses `response`, and codex emits
#: JSONL events with no single envelope at all — see `unwrap_codex`.
RESULT_KEY = {"claude": "result", "agy": "response"}


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


def parse_agy(envelope: dict) -> Usage:
    """agy `--output-format json`. Probed 2026-08-23:

        {"conversation_id": ..., "status": "SUCCESS", "response": "...",
         "duration_seconds": 8.8, "num_turns": 1,
         "usage": {"input_tokens": 18328, "output_tokens": 1624,
                   "thinking_tokens": 1306, "cache_read_tokens": 12201,
                   "total_tokens": 19952}}

    No cost figure and no cache-CREATION counter. Both stay None rather than
    zero: agy may not cache, or may simply not report it, and this module cannot
    tell those apart from the outside.
    """
    usage = envelope.get("usage") or {}
    seconds = envelope.get("duration_seconds")
    return Usage(
        provider="agy",
        model=None,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        thinking_tokens=_int(usage.get("thinking_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_tokens")),
        cache_creation_tokens=None,
        cost_usd=None,
        api_ms=(int(seconds * 1000)
                if isinstance(seconds, (int, float)) else None),
        turns=_int(envelope.get("num_turns")),
        source="agy --output-format json")


def parse_codex(events: list):
    """codex `--json`. Probed 2026-08-23, the terminal event:

        {"type": "turn.completed",
         "usage": {"input_tokens": 14957, "cached_input_tokens": 11008,
                   "cache_write_input_tokens": 0, "output_tokens": 6,
                   "reasoning_output_tokens": 0}}

    The names differ from every other provider here and are MAPPED rather than
    passed through: `reasoning_output_tokens` is what the other two call
    thinking, `cached_input_tokens` is a cache read, `cache_write_input_tokens`
    is a cache creation. Keeping the vendor's spelling would leave three fields
    meaning one thing and a comparison nobody could make.

    Turns are SUMMED across `turn.completed` events. Taking only the last would
    report the cost of the final turn as the cost of the whole call.
    """
    totals = {"input": 0, "output": 0, "thinking": 0, "read": 0, "write": 0}
    turns = 0
    seen = False
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage") or {}
        seen = True
        turns += 1
        totals["input"] += _int(usage.get("input_tokens")) or 0
        totals["output"] += _int(usage.get("output_tokens")) or 0
        totals["thinking"] += _int(usage.get("reasoning_output_tokens")) or 0
        totals["read"] += _int(usage.get("cached_input_tokens")) or 0
        totals["write"] += _int(usage.get("cache_write_input_tokens")) or 0
    if not seen:
        return None
    return Usage(
        provider="codex", model=None,
        input_tokens=totals["input"], output_tokens=totals["output"],
        thinking_tokens=totals["thinking"],
        cache_read_tokens=totals["read"],
        cache_creation_tokens=totals["write"],
        cost_usd=None, api_ms=None, turns=turns,
        source="codex exec --json")


def unwrap_codex(text: str):
    """`(answer, usage, signals)` from codex's JSONL stream.

    Not an envelope, so it does not go through `unwrap`: the answer is the last
    `agent_message` item and the usage is summed over `turn.completed`. Lines
    that are not JSON are skipped rather than failing the parse, because codex
    prints human notices such as "Reading additional input from stdin" alongside
    its events.
    """
    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    if not events:
        return text, None, {}

    answer = None
    for event in events:
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            answer = item.get("text") or answer
    usage = parse_codex(events)
    return ((answer if isinstance(answer, str) else text), usage,
            {"turns": usage.turns} if usage else {})


PARSERS = {"claude": parse_claude, "agy": parse_agy}


#: Envelope fields that are not usage but ARE evidence. `permission_denials` is
#: the provider saying it was refused a tool, which is the difference between "it
#: chose not to" and "it could not" — and the second must never be reported as a
#: successful call that simply did nothing.
SIGNAL_KEYS = ("permission_denials", "is_error", "stop_reason",
               "terminal_reason", "num_turns")


def unwrap(provider_id: str, text: str) -> tuple[str, Usage | None, dict]:
    """`(answer, usage, signals)` from a provider's structured envelope.

    A text that is not the expected envelope comes back UNCHANGED with no usage.
    That is the important half: a CLI that ignored the flag, a version that
    changed its output, or a crash that printed a traceback must degrade to "the
    answer, unmeasured" rather than to an exception or to an empty answer.
    """
    if provider_id == "codex":
        # JSONL rather than one envelope, so it has its own reader.
        return unwrap_codex(text)
    parser = PARSERS.get(provider_id)
    if parser is None or not text.strip():
        return text, None, {}
    try:
        envelope = json.loads(text)
    except (ValueError, TypeError):
        return text, None, {}
    if not isinstance(envelope, dict):
        return text, None, {}
    key = RESULT_KEY.get(provider_id)
    if key is None or key not in envelope:
        return text, None, {}
    answer = envelope.get(key)
    signals = {k: envelope[k] for k in SIGNAL_KEYS if k in envelope}
    return ((answer if isinstance(answer, str) else text), parser(envelope),
            signals)


def roll_up(results) -> dict:
    """One run record from a sequence of `ProviderResult`, per provider.

    Three counts, kept apart on purpose. `calls_total` is what was launched,
    `calls_succeeded` is what came back usable, and `calls_failed` is the
    difference — a provider that fails half its calls and a provider that makes
    half as many are the same number to a single counter and completely
    different to an operator.

    Attempts are kept individually as well as summed. A retried node's cost is
    the sum, and which attempt cost what is how a retry policy gets evaluated.
    """
    by_provider: dict = {}
    for index, result in enumerate(results):
        pid = getattr(result, "provider", "unknown")
        row = by_provider.setdefault(pid, {
            "provider": pid, "calls_total": 0, "calls_succeeded": 0,
            "calls_failed": 0, "wall_s": 0.0, "attempts": []})
        ok = bool(getattr(result, "ok", False))
        usage = getattr(result, "usage", None)
        row["calls_total"] += 1
        row["calls_succeeded"] += 1 if ok else 0
        row["calls_failed"] += 0 if ok else 1
        row["wall_s"] = round(row["wall_s"]
                              + float(getattr(result, "duration_s", 0.0)), 2)
        row["attempts"].append({
            "index": index, "ok": ok,
            "duration_s": getattr(result, "duration_s", None),
            "error": (getattr(result, "error", None) or "")[:200] or None,
            "usage": usage,
            # A failed call still LAUNCHED. Recording it with usage None keeps
            # the launch visible without inventing a token count for it.
            "usage_measured": usage is not None})

    for row in by_provider.values():
        measured = [Usage(**{k: v for k, v in a["usage"].items()
                             if k in Usage.__dataclass_fields__})
                    for a in row["attempts"] if a["usage"]]
        summed = total(measured + [None] * (row["calls_total"]
                                            - len(measured)))
        row["usage"] = summed
    return {"providers": by_provider,
            "calls_total": sum(r["calls_total"] for r in by_provider.values()),
            "calls_succeeded": sum(r["calls_succeeded"]
                                   for r in by_provider.values()),
            "calls_failed": sum(r["calls_failed"]
                                for r in by_provider.values()),
            "note": ("usage null on an attempt means the provider reported "
                     "none, not that it spent none")}


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
