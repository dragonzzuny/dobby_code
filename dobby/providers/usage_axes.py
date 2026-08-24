"""Where a run's tokens actually went, split four ways.

`usage.py` answers "how many tokens". This answers "how many of them were the
work", which is the question a decomposing harness lives or dies on. A harness
that splits one task into five calls pays every per-call constant five times, so
a total that does not separate the constants from the content cannot say whether
splitting helped.

The four axes
-------------
``prefix_write``   `cache_creation_tokens` — what the call had to establish
                   before it could start: system prompt, tool schemas, and the
                   first pass of context.
``prefix_reread``  `cache_read_tokens` — the same prefix read again on later
                   turns. In an agentic loop this is the exploration tax, and it
                   is the axis a plan is supposed to shrink: a call that is told
                   where to look does not re-read the tree finding out.
``fresh_input``    `input_tokens` — input that was neither cached nor re-read.
                   On these CLIs it is startlingly small; two digits is normal.
``generated``      `output_tokens` + `thinking_tokens` — what the model produced.

What this CANNOT separate
-------------------------
Tool definitions are inside `prefix_write` and no CLI reports them apart. They
are separable only by DIFFERENTIAL — run the same prompt under different
`--tools` sets and subtract. `TOOL_SURCHARGE` records that experiment rather than
pretending the split is available per call, because a number attributed to tools
without varying the tools is a guess wearing a field name.

Provider comparability
----------------------
`usage.py` already maps each vendor's names onto one set (codex's
`reasoning_output_tokens` is thinking, its `cached_input_tokens` is a cache
read). It cannot make the vendors agree on what they COUNT: claude reports an
`input_tokens` of 10 beside a 210,506-token cache read, while codex folds most of
its prefix into `input_tokens` and reports no cache write at all. So
`prefix_write` is not comparable across providers, and `total()` is. Both are
returned, and `comparable` says which is which.
"""

from __future__ import annotations

import dataclasses

#: Measured on this machine 2026-08-24 with `scratchpad/tool_tax.py`: the same
#: trivial prompt to `claude -p` under four tool sets, cold cache. The value is
#: `cache_creation_tokens` above the tools-disabled floor, so it isolates the
#: schemas from the system prompt that is there either way.
TOOL_SURCHARGE = {
    "": 0,                       # 22,565 tokens is the floor, and it is not tools
    "Read": 509,
    "Read,Edit,Bash": 2050,
    "default": 7603,
}

#: The floor the surcharges sit on: what one claude call costs before tools and
#: before any task. Recorded because it is the number that decides whether
#: splitting a task can ever pay for itself.
CALL_FLOOR_TOKENS = 22565

AXES = ("prefix_write", "prefix_reread", "fresh_input", "generated")


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_mapping(usage) -> dict:
    if usage is None:
        return {}
    if dataclasses.is_dataclass(usage):
        return dataclasses.asdict(usage)
    if hasattr(usage, "to_dict"):
        return usage.to_dict()
    return dict(usage)


def axes(usage) -> dict:
    """One usage envelope split into the four axes, plus its total."""
    u = _as_mapping(usage)
    split = {
        "prefix_write": _int(u.get("cache_creation_tokens")),
        "prefix_reread": _int(u.get("cache_read_tokens")),
        "fresh_input": _int(u.get("input_tokens")),
        "generated": _int(u.get("output_tokens")) + _int(u.get("thinking_tokens")),
    }
    split["total"] = sum(split[a] for a in AXES)
    return split


def axes_for_record(record: dict) -> dict:
    """The four axes for a `usage.roll_up` record, per provider and summed.

    `measured` is carried through rather than assumed: a provider that made
    calls it did not report usage for makes the totals a FLOOR, and a floor
    reported as a total is the arithmetic version of the claim this module
    exists to make checkable.
    """
    providers = (record or {}).get("providers") or {}
    per_provider, totals = {}, dict.fromkeys(AXES + ("total",), 0)
    calls = measured = 0
    for pid, row in providers.items():
        split = axes(row.get("usage"))
        split["calls"] = _int(row.get("calls_total"))
        split["calls_measured"] = _int((row.get("usage") or {}).get(
            "calls_measured"))
        per_provider[pid] = split
        for key in AXES + ("total",):
            totals[key] += split[key]
        calls += split["calls"]
        measured += split["calls_measured"]
    return {
        "per_provider": per_provider,
        "totals": totals,
        "calls": calls,
        "calls_measured": measured,
        "complete": calls == measured,
        "note": ("" if calls == measured else
                 f"{calls - measured} of {calls} call(s) reported no usage; "
                 f"every total here is a FLOOR, not a measurement"),
    }


def per_unit(totals: dict, units: int) -> dict:
    """Axis totals divided by whatever the run is being priced per.

    `units` is the count of tasks that actually SUCCEEDED, never the count
    attempted: dividing by attempts credits an arm for the tasks it failed, which
    is how a harness that gives up early wins a token-efficiency table.
    """
    if not units:
        return {axis: None for axis in AXES + ("total",)}
    return {axis: round(totals.get(axis, 0) / units, 1)
            for axis in AXES + ("total",)}


def compare(by_arm: dict, *, baseline: str) -> dict:
    """Each arm's axes against a baseline arm, as ratios.

    `by_arm` maps an arm name to `{"totals": ..., "units": ...}`. A ratio above
    1.0 means the arm spent MORE than the baseline on that axis. Ratios are
    reported per successful unit, so an arm that solved fewer tasks does not look
    cheap for having done less.
    """
    if baseline not in by_arm:
        raise KeyError(f"baseline arm {baseline!r} not among {sorted(by_arm)}")
    base = per_unit(by_arm[baseline]["totals"], by_arm[baseline]["units"])
    out = {}
    for arm, row in by_arm.items():
        unit = per_unit(row["totals"], row["units"])
        out[arm] = {
            "per_unit": unit,
            "units": row["units"],
            "ratio_to_baseline": {
                axis: (None if not base.get(axis) or unit.get(axis) is None
                       else round(unit[axis] / base[axis], 2))
                for axis in AXES + ("total",)
            },
        }
    return {"baseline": baseline, "arms": out}
