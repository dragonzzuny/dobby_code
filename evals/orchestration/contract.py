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

import json
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


def render_tools(tools) -> str:
    """OpenAI-style tool definitions as text, because a CLI has no tools channel.

    `llm_agent._complete_general` passes the agent card's tools as a KWARG:

        kwargs = {"tools": self.tools}
        resp = await self.model.generate_chat_response(msgs_, **kwargs)

    An API model forwards that to its `tools=` parameter. A CLI has nowhere to
    put it, so an adapter that ignores the kwarg leaves the model with no idea
    the tools exist — and it says so. Measured 2026-08-24, transport_agent on
    scenario 1 answering where `tool: callTaxi` was expected:

        <status>TOOL_CONSTRAINT_VIOLATION</status>
        <violation_message>No taxi, rideshare, or ground transportation
        booking tool is available in this environment.

    The model was right. Twenty scenarios scored FC 0.0 with an underaction rate
    of 0.97 on that, and the number measured the adapter rather than the model.
    """
    if not tools:
        return ""
    lines = ["## Tools available to you", ""]
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # Both the bare shape and OpenAI's {"type": "function", "function": {…}}
        # wrapper appear in the wild; take whichever carries the name.
        body = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        name = body.get("name")
        if not name:
            continue
        lines.append(f"### {name}")
        if body.get("description"):
            lines.append(str(body["description"]))
        params = body.get("parameters")
        if params:
            lines.append("parameters (JSON schema):")
            lines.append(json.dumps(params, ensure_ascii=False))
        lines.append("")
    return "\n".join(lines).strip()


#: Prepended to every call, on every arm, identically.
#:
#: OrchestrationBench is written for a chat-completion API: the caller passes
#: `tools=[…]` and the model RETURNS a tool-call object, executing nothing.
#: These providers are agentic CLIs. Handed the same description they try to
#: INVOKE the tool, fail, and report the failure as their answer. Measured
#: 2026-08-24, transport_agent with the tools rendered into the prompt and
#: `--permission-mode acceptEdits`:
#:
#:   without this   "I can't complete this — `callTaxi` is listed in the
#:                  conversation's tool descriptions but isn't actually
#:                  available in my runtime, so the call failed with
#:                  'No such tool available.'"
#:   with this      {"name": "callTaxi", "arguments": {"refinedQuery": …}}
#:
#: Same prompt, same cost (57,471 against 57,709 tokens), opposite outcome. This
#: is the API contract restated in the idiom a CLI understands, not a change to
#: what is being asked — and applying it to every arm equally is what keeps the
#: comparison about the harness.
EXECUTION_FRAMING = (
    "You are answering as a TEXT COMPLETION, not as an agent with live tools.\n"
    "Any tools described below are a fiction of this exercise: none of them\n"
    "exists in your runtime and none can be invoked. Do not attempt to call\n"
    "anything, and do not report that a call failed. WRITE the call or the\n"
    "workflow you would produce, as text, and nothing else.\n"
)

#: The CLI's OWN tools, disabled. The benchmark supplies the tools that matter
#: in the prompt; the CLI's real ones (file reads, shell) are not merely unused
#: here, they are what the model goes exploring with. Measured on one
#: transport_agent turn: 176,372 tokens with them against 57,154 without, the
#: difference being `cache_read` — an agentic loop re-reading its own context.
NO_CLI_TOOLS = ("--tools", "")


#: A turn is subject to the workflow contract only if a workflow is what it was
#: ASKED for. The benchmark uses four system prompts and exactly one of them
#: wants a workflow:
#:
#:     general                8,268 chars,  "workflow" x75   the orchestrator
#:     select_tools           2,196 chars,  x0               a sub-agent
#:     summarize              1,734 chars,  x0
#:     history_summarization  3,131 chars,  x0
#:
#: Applying the contract everywhere was a real and expensive mistake. A
#: sub-agent answering `{"name": "callTaxi", …}` — which is CORRECT — failed a
#: check for `workflow_` and was retried on every provider in the chain.
#: Measured on scenario 1: three logical turns became eleven calls and
#: 3,312,817 tokens, against 82,656 for the same scenario without the loop, and
#: agy alone burned 3,166,630 of them answering a question nobody should have
#: asked it twice.
WORKFLOW_EXPECTED = re.compile(r"workflow", re.I)

#: Below this many mentions the prompt is talking ABOUT workflows in passing
#: rather than demanding one. `general` has 75; the others have none, so any
#: small threshold separates them and this one does not have to be delicate.
WORKFLOW_MENTIONS_REQUIRED = 5


def expects_workflow(system_prompt: str | None) -> bool:
    """Whether this turn is one the workflow contract applies to."""
    if not system_prompt:
        return False
    return len(WORKFLOW_EXPECTED.findall(system_prompt)) >= WORKFLOW_MENTIONS_REQUIRED


#: Key names models actually use for the two halves of a tool call. The
#: benchmark's own prompt says only "return ONLY the tool call JSON format" and
#: never names the keys — it does not have to, because an API model is handed
#: `tools=` and returns a `tool_calls` object whose shape comes from the schema.
#: Driving a CLI there is no such object, so the answer arrives as text and the
#: keys are whatever the model chose.
_NAME_KEYS = ("function_name", "name", "tool", "tool_name")
_ARG_KEYS = ("arguments", "parameters", "args", "params")


def parse_tool_call(text: str) -> list:
    """The model's textual tool call as OpenAI `tool_calls`, or [].

    This is the same accommodation `render_tools` is, at the other end of the
    round trip: the benchmark gives an API model its tools through a parameter
    and reads its answer out of a structured field. A CLI has neither, so the
    tools go in as text and the call comes back as text, and something has to
    put it back into the shape `eval_utils` reads.

    Without it FC scored 0.0 on a scenario where the model named the right tool
    with the right arguments — `{"tool": "callTaxi", "parameters": {…}}` against
    a reader looking for `function_name`. The failure was the adapter's.

    XML answers are left alone. `AWAITING_USER_INPUT` and
    `TOOL_CONSTRAINT_VIOLATION` are the benchmark's rejection paths and turning
    one into a tool call would invent an action the model declined to take.
    """
    if not isinstance(text, str) or "<response>" in text:
        return []
    from json import JSONDecodeError, dumps, loads

    body = text.strip().split("</think>")[-1]
    match = re.search(r"\{.*\}", body, re.DOTALL | re.MULTILINE)
    if not match:
        return []
    try:
        parsed = loads(match.group(0))
    except (JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []

    name = next((parsed[k] for k in _NAME_KEYS
                 if isinstance(parsed.get(k), str) and parsed[k]), None)
    if not name:
        return []
    args = next((parsed[k] for k in _ARG_KEYS
                 if isinstance(parsed.get(k), dict)), {})
    return [{"id": f"call_{name}", "type": "function",
             # A STRING, because that is what `eval_utils` json.loads. Handing
             # it a dict would raise inside the scorer.
             "function": {"name": name, "arguments": dumps(args,
                                                           ensure_ascii=False)}}]


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
