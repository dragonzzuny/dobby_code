"""What a run cost, and the two reasons that is not one number.

MODEL IDENTITY
--------------
A token count is uninterpretable without knowing which model produced it.
Measured 2026-08-24, from the CLIs' own JSON:

    claude   reports `model` per call — "claude-opus-5[1m]", "claude-fable-5"
    codex    reports NO model field. `codex doctor` says `model <default>`
    agy      reports NO model field, and `agy models` lists gemini flash/pro,
             `claude-sonnet-4-6`, `claude-opus-4-6-thinking` and gpt-oss

So an unpinned agy run may have been Gemini Flash or it may have been Claude
Opus, and nothing in the record can say which. The C_agy row of the first pilot
is exactly that: 1,601,155 tokens attributable to no model. `require_pinned`
exists so that does not happen twice — the arm names its model, the row records
it, and the number means something.

BILLING MODE
------------
`codex doctor` reports `auth mode: chatgpt`. Codex on this machine bills against
a ChatGPT subscription, not per token, which is why its `cost_usd` is None. That
is not a gap in the measurement; it is a different billing model, and putting a
subscription CLI's dollars in the same column as a metered API's would be a
category error dressed as a comparison.

So: TOKENS are the comparable axis across arms, and DOLLARS are reported only
where the provider itself returned a figure. Rates are not hardcoded here.
Inventing a per-token price for a subscription seat, or pasting a published rate
that may have changed, would produce a number this repository's own rules forbid
— it did not come from a command run in the session that reports it.
"""

from __future__ import annotations

#: How each provider bills on THIS machine, as observed rather than assumed.
#: Re-derive with `codex doctor`, `claude --version`, `agy models`.
BILLING_MODE = {
    "claude": "metered-and-reported",   # returns total_cost_usd per call
    "codex": "subscription",            # codex doctor: auth mode chatgpt
    "agy": "subscription",              # antigravity seat; reports no cost
    "gemini": "unknown",
}

#: Providers whose CLI names the model it used. The rest must be pinned by the
#: caller or the row cannot say what produced its tokens.
REPORTS_MODEL = {"claude"}


class ModelNotPinned(ValueError):
    """An arm whose provider does not name its model, and which named none."""


def require_pinned(provider_id: str, model) -> str:
    """The model this arm will run, or an error naming what is missing.

    Refuses rather than defaulting. A default here would be a guess recorded as
    a fact, and the whole point of the function is that the first pilot already
    produced a row nobody can interpret.
    """
    if model:
        return model
    if provider_id in REPORTS_MODEL:
        return "(reported by the provider)"
    raise ModelNotPinned(
        f"{provider_id} does not report which model it used and none was "
        f"pinned, so its token counts would belong to no model. Pass a model "
        f"for this arm — `agy models` and `codex --model` list what is "
        f"available")


def cost_of(usage: dict | None) -> float | None:
    """The provider's OWN cost figure, or None. Never derived from a rate."""
    if not usage:
        return None
    value = usage.get("cost_usd")
    return None if value is None else float(value)


def summarise(record: dict) -> dict:
    """Per-provider tokens and cost, with the billing mode attached to each.

    `dollars_comparable` is false as soon as any provider in the run bills by
    subscription: the run's dollar total is then a partial sum and adding it up
    would understate it by however much the subscription covered.
    """
    providers = (record or {}).get("providers") or {}
    rows, reported, subscription = {}, 0.0, []
    for pid, entry in providers.items():
        usage = entry.get("usage") or {}
        cost = cost_of(usage)
        mode = BILLING_MODE.get(pid, "unknown")
        rows[pid] = {
            "calls": entry.get("calls_total"),
            "calls_measured": usage.get("calls_measured"),
            "models": sorted({(a.get("usage") or {}).get("model")
                              for a in entry.get("attempts", [])
                              if (a.get("usage") or {}).get("model")}),
            "billing_mode": mode,
            "cost_usd_reported": cost,
        }
        if cost is not None:
            reported += cost
        if mode == "subscription" and (entry.get("calls_total") or 0):
            subscription.append(pid)
    return {
        "per_provider": rows,
        "cost_usd_reported_total": round(reported, 4) if rows else None,
        "subscription_providers": sorted(subscription),
        "dollars_comparable": not subscription,
        "note": ("" if not subscription else
                 f"{', '.join(sorted(subscription))} bill against a "
                 f"subscription and report no per-call cost, so the dollar "
                 f"total covers only the metered providers and must not be "
                 f"compared with an arm that has none"),
    }
