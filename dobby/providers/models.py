"""Which MODEL a role gets, and whether the one that answered is the one asked for.

Two facts, both measured on this machine on 2026-08-29, decide the shape of
this module.

The first is that model choice is worth making. The same prompt to the same
provider, `claude`, differing only in `--model`:

    (unset)   claude-opus-5[1m]   249 out, 227 thinking   $0.2997   22.3s
    sonnet    claude-sonnet-5      47 out,   0 thinking   $0.1203   16.0s

Sixty percent of the cost for a lookup that needed no deep reasoning. The
plumbing to ask for that already ran end to end -- `node.config["model"]` ->
`run_provider(model=)` -> `spec.build_argv` -> `--model` -- and nothing in the
runtime ever set it. A capability with no producer, which is the same shape as
the quota ledger nothing imported.

The second is that asking is not getting:

    requested haiku                       answered claude-sonnet-5
    requested haiku                       answered claude-sonnet-5
    requested claude-haiku-4-5-20251001   answered claude-haiku-4-5-20251001
    requested sonnet                      answered claude-sonnet-5

The `haiku` alias is not resolved by that CLI and it falls back SILENTLY. A run
that pinned haiku to save money would be billed for sonnet and never learn. So
this module does not only choose a model; it says afterwards whether the choice
took, because a pin nobody checks is a preference and not a pin.

Nothing is inferred. The tier for a role is declared here, the model for a
(provider, tier) is declared by the operator in `.dobby/config.json`, and with
no declaration nothing is passed and the provider's own default applies -- which
is exactly what every run did before this existed.
"""

from __future__ import annotations

import json
import os

#: Role -> tier. The roles are `providers/policy.py`'s; the tiers are names an
#: operator maps to real model ids for their own providers.
#:
#: The split is about how much of the answer has to be REASONED. A scout is
#: reading a tree and reporting paths; a mechanical step is applying a
#: transformation somebody already specified. Neither improves with a larger
#: model, and both are the bulk of the calls in a decomposed run. An architect
#: is choosing what to do and a judge is deciding whether it was done, and those
#: are where a weaker model costs more than it saves.
#:
#: Declared rather than derived from the task text. Deriving it would put a
#: model's guess about difficulty in front of a spend decision, and a guess that
#: flatters the cheap option is the one nobody notices.
ROLE_TIER: dict[str, str] = {
    "scout": "cheap",
    "mechanical": "cheap",
    "draft": "cheap",
    "implement": "standard",
    "synthesize": "standard",
    "critic": "standard",
    "architect": "strong",
    "adjudicate": "strong",
}

TIERS = ("cheap", "standard", "strong")


def tier_for(role: str) -> str:
    """The declared tier for `role`, or `standard` for one nobody classified."""
    return ROLE_TIER.get(role or "", "standard")


def load_table(data_dir: str) -> dict:
    """`{provider: {tier: model}}` from `.dobby/config.json`, or empty.

    Empty is the default and means: pass no `--model`, and let the provider's
    own default stand. Model ids are provider-specific strings this repository
    cannot validate without spending a call, so they are the operator's to
    declare and not this module's to guess.
    """
    path = os.path.join(data_dir, "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        # A damaged config is `doctor`'s to report. Choosing a model is not the
        # place to raise about it, and silently pretending it said something is
        # worse than falling back to the provider default.
        return {}
    table = ((blob.get("providers") or {}).get("models") or {})
    if not isinstance(table, dict):
        return {}
    return {str(pid): {str(t): str(m) for t, m in (tiers or {}).items()
                       if isinstance(m, str) and m}
            for pid, tiers in table.items() if isinstance(tiers, dict)}


def model_for(provider: str, role: str, *, table: dict) -> str:
    """The declared model for this provider and role, or "" for none.

    Falls back down the tiers rather than across providers: an operator who
    declared only `strong` for a provider gets that provider's strong model for
    a cheap role, which is what they configured, instead of a model belonging to
    somebody else's provider.
    """
    per_provider = table.get(provider) or {}
    if not per_provider:
        return ""
    wanted = tier_for(role)
    order = list(TIERS[TIERS.index(wanted):]) + list(TIERS[:TIERS.index(wanted)])
    for tier in order:
        if per_provider.get(tier):
            return per_provider[tier]
    return ""


def honoured(pinned: str, reported: str) -> bool | None:
    """Did the model that answered match the one asked for?

    `True` they agree, `False` they do not, `None` unknowable -- nothing was
    pinned, or the provider named no model. `None` is not `True`: a CLI that
    reports nothing has not confirmed anything, and flattening the two would
    turn "we cannot tell" into "it was fine".

    Aliases count as agreement. `sonnet` is answered by `claude-sonnet-5` and
    that is the pin taking effect, not a substitution -- so a pin is honoured
    when either name contains the other, compared without case or separators.
    That is deliberately generous: this exists to catch `haiku` coming back as
    `claude-sonnet-5`, which no amount of generosity makes look like a match.
    """
    if not pinned or not reported:
        return None
    left, right = _normalise(pinned), _normalise(reported)
    return left in right or right in left


def _normalise(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())
