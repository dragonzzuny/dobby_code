"""The output contract the dobby arm enforces, kept away from the benchmark.

`dobby_model.py` subclasses OrchestrationBench's `BaseModel` and therefore only
imports inside a checkout of it. Everything decidable without a provider or a
benchmark lives here instead, so it can be tested in this repository on its own.

The contract is the benchmark's, not ours. `evaluate_workflow_as_DAG.
extract_workflow_from_content` accepts an answer only if it can find a JSON
object carrying at least one `workflow_*` key; an answer without one scores zero
whether or not dobby is in the loop. Checking the same thing at call time turns
that zero into a retry, and checking anything STRICTER would manufacture retries
and inflate the very count the arm is measured on.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

#: A cheap pre-filter. The scorer accepts JSON *and* YAML, so the quoted-key
#: form is not the test — `workflow_` appearing at all is the weakest thing that
#: must be true, and the scorer's own function decides the rest.
#:
#: A first version matched `"workflow_\d+"` with the quotes, i.e. JSON only. The
#: benchmark's gold answers are YAML (`workflow_1:\n  status: pending`), so that
#: version would have rejected correct replies and manufactured retries — and
#: the retry count is one of the numbers this arm reports. Caught by reading
#: `data/EN/scenario_data/1.yaml` before spending a call on it.
WORKFLOW_HINT = re.compile(r"workflow_\d+")

#: Tried in order when an answer misses the contract. A DIFFERENT provider, never
#: the same one twice: `dobby/swarm/diversity.py` is this repository's argument
#: that a model's second pass is correlated with its first, and a retry that asks
#: the same model again buys a correlated sample at full price.
DEFAULT_RETRY_CHAIN = ("claude", "codex", "agy")


def load_extractor():
    """The benchmark's OWN extractor. Importable only inside its checkout.

    Delegating rather than reimplementing is the whole design of this contract.
    The scorer accepts a workflow as bare JSON, JSON in a fence, YAML in a
    fence, whole-content YAML, and a re-indented repair of malformed YAML — five
    paths, each with its own fallbacks. A second implementation of that would
    drift from it, and every point of drift is either a retry nobody needed or
    an answer accepted that the scorer will score zero.
    """
    from src.utils.evaluation.evaluate_workflow_as_DAG import (
        extract_workflow_from_content)

    return extract_workflow_from_content


def carries_workflow(text: str, extract=None) -> bool:
    """Whether the SCORER would find a workflow in `text`.

    `extract` is injected so this is testable outside a benchmark checkout; in
    the adapter it is `load_extractor()`, which is the function that will
    actually grade the answer.
    """
    if not isinstance(text, str) or not WORKFLOW_HINT.search(text):
        return False
    if extract is None:
        extract = load_extractor()
    try:
        parsed = extract(text)
    except Exception:                              # noqa: BLE001
        # The scorer swallows its own parse errors and returns {}. An adapter
        # that raised where the scorer shrugs would turn a zero into a crash.
        return False
    return bool(isinstance(parsed, dict)
                and any(str(key).startswith("workflow_") for key in parsed))


def retry_chain(primary: str, chain=DEFAULT_RETRY_CHAIN) -> tuple:
    """`primary` first, then the others once each, never `primary` again."""
    return (primary,) + tuple(p for p in chain if p != primary)


def summarise(attempts: List[Dict[str, Any]], *, arm: str, provider: str,
              cli_model: str | None = None) -> Dict[str, Any]:
    """What the arm did, in the shape the comparison table needs.

    `contract_violation_rate` is over ANSWERED calls, not all calls: a provider
    that failed to return at all did not violate a contract, it never reached
    one, and folding the two together would let an outage read as a formatting
    problem.
    """
    answered = [a for a in attempts if a.get("ok")]
    violations = [a for a in answered if not a.get("carries_workflow")]
    switched = [a for a in attempts if a.get("index", 0) > 0]
    return {
        "arm": arm,
        "provider": provider,
        "cli_model": cli_model,
        "calls": len(attempts),
        "answered": len(answered),
        "contract_violations": len(violations),
        "contract_violation_rate": (round(len(violations) / len(answered), 3)
                                    if answered else None),
        "retries_on_another_provider": len(switched),
        "recovered_by_retry": bool(switched and attempts[-1].get("carries_workflow")),
        "providers_used": sorted({a.get("provider") for a in attempts
                                  if a.get("provider")}),
    }
