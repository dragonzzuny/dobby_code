"""dobby as a model, so a third party's rubric can score its orchestration.

Why
---
Every methodology number this repository has produced came out of dobby's own
run store — contract-violation rate, evidence density, effect observation. Those
are real and they are also circular: the harness defines the fields, fills them,
and grades itself on them. `kakao/OrchestrationBench` (Apache 2.0) is a rubric
nobody here wrote: 222 scenarios per language over 17 domains, scoring a
DAG-shaped plan against a gold workflow with `networkx` graph edit distance, plus
function-name and argument-key F1.

What this adapter measures
--------------------------
The MODEL is held fixed and the HARNESS is the variable, which is the design
`docs/EVAL_DESIGN.md` argues for. Two arms share this class:

    arm="solo"    one CLI call, the answer returned as-is. This is what
                  claude / codex / agy / fable score on their own.
    arm="dobby"   the same call, then dobby's contract loop: the answer must
                  parse as the shape the benchmark itself requires, and a reply
                  that does not is RETRIED ON A DIFFERENT PROVIDER rather than
                  accepted or re-asked of the model that just missed.

The contract is not invented here and it is not tuned to flatter the harness. It
is exactly what `evaluate_workflow_as_DAG.extract_workflow_from_content` looks
for — a JSON object carrying at least one `workflow_*` key. An answer that fails
it scores zero with or without this adapter; the only question the dobby arm
asks is whether retrying elsewhere recovers it.

Measured precedent, 2026-08-24 on django__django-11532: dobby's scout node got a
`thread.started` envelope from codex where a `claims` document was required, the
contract rejected it, the retry went to claude, and the second answer carried
nine claims with thirty-five file:line citations. That is the behaviour this arm
exists to price against a scale somebody else drew.

Not measured here
-----------------
`arguments_value_f1` needs OrchestrationBench's LLM judge, which needs an API
key this machine does not have. The three judge-free metrics are the ones
reported. Saying which is missing is the point; a mean over three metrics
presented as the benchmark's four-metric average would be a different number
wearing the same name.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

# The benchmark imports this from inside its own tree, so its `src` package is
# importable; dobby is not, and its location is passed rather than guessed.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_DOBBY_ROOT = os.environ.get("DOBBY_ROOT")
if _DOBBY_ROOT and _DOBBY_ROOT not in sys.path:
    sys.path.insert(0, _DOBBY_ROOT)

from src.models.base_model import BaseModel  # noqa: E402

from contract import (DEFAULT_RETRY_CHAIN, EXECUTION_FRAMING,  # noqa: E402
                      carries_workflow, expects_workflow, parse_tool_call,
                      render_tools, retry_chain, summarise)


class DobbyModel(BaseModel):
    """One CLI call, or one CLI call inside dobby's contract loop."""

    def __init__(self, model_name: str, temperature: float = 0.2,
                 max_tokens: int = 4096,
                 token_callback: Optional[Callable] = None,
                 provider: str = "claude", arm: str = "solo",
                 retry_chain=DEFAULT_RETRY_CHAIN, cli_model: str | None = None,
                 timeout_s: int = 600, **kwargs):
        super().__init__(model_name, temperature, max_tokens, token_callback,
                         **kwargs)
        self.provider = provider
        self.arm = arm
        self.retry_chain = tuple(retry_chain)
        # The CLI's own model. codex and agy do not report theirs, so pinning it
        # is the only way the row can say what produced the tokens; see
        # evals/swebench/billing.py for the measurement that forced this.
        self.cli_model = cli_model
        self.timeout_s = timeout_s
        self.attempts: List[Dict[str, Any]] = []

    def _on_path(self) -> bool:
        from dobby.providers.catalog import registry

        return registry().get(self.provider).which() is not None

    async def initialize(self) -> None:
        if not self._on_path():
            raise RuntimeError(
                f"{self.provider} is not on PATH; this adapter drives CLIs and "
                f"cannot fall back to an API")
        self.initialized = True

    async def check_availability(self) -> bool:
        """Whether this arm could run. PATH only — no call is made.

        The benchmark asks this before a run and a truthful answer here is
        cheap; probing with a real prompt would spend money to answer a question
        `shutil.which` already settles. An installed CLI that turns out to be
        unauthenticated fails at the first scenario and says so there, which is
        where the error belongs.
        """
        return self._on_path()

    # -- the call ------------------------------------------------------------

    def _call_sync(self, provider_id: str, prompt: str) -> Any:
        from dobby.providers.catalog import registry
        from dobby.providers.run import run_by_id, spend_ledger
        from dobby.swebench import write_extra_for

        spec = registry().get(provider_id)
        extra = tuple(write_extra_for(provider_id)) + spec.workspace(os.getcwd())
        # Turn the CLI's own tools off where it supports that. `tool_scope`
        # returns () for a provider with no such flag, so this is a no-op for
        # codex and agy rather than an invented argument.
        extra += spec.tool_scope("")
        model = self.cli_model if provider_id == self.provider else None
        # Also record into dobby's own ledger when asked, because the
        # benchmark's cost figure cannot be trusted for these arms: `pricing.py`
        # carries "fallback prices for unknown models" and none of
        # claude-opus-5, claude-fable-5, gpt-5.6-sol or gemini-3.5-flash is in
        # its table. The first smoke reported total_cost 2.413185 for an arm
        # whose real per-call cost the provider had already stated. dobby's
        # ledger keeps the VENDOR's figure, the model that produced it, and a
        # null where a subscription provider reports none.
        sink = os.environ.get("DOBBY_SPEND_DIR")
        with spend_ledger(sink, skill=f"orchestration-bench:{self.arm}"):
            return run_by_id(provider_id, prompt, model=model, extra=extra,
                             timeout_s=self.timeout_s, collect_usage=True,
                             output_cap=400_000)

    def _account(self, result) -> None:
        usage = result.usage or {}
        # Input side is everything the provider had to read. Claude splits it
        # across cache fields and codex folds it into `input_tokens`; summing
        # them is the only form comparable across the two.
        inbound = sum(int(usage.get(k) or 0) for k in
                      ("input_tokens", "cache_read_tokens",
                       "cache_creation_tokens"))
        outbound = sum(int(usage.get(k) or 0) for k in
                       ("output_tokens", "thinking_tokens"))
        self._update_stats(inbound, outbound)

    async def _run(self, provider_id: str, prompt: str):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._call_sync, provider_id,
                                          prompt)

    async def generate_response(self, prompt: str,
                                system_prompt: Optional[str] = None,
                                **kwargs) -> str:
        # The framing goes FIRST and on every arm. See contract.EXECUTION_FRAMING
        # for the measurement that made it necessary: without it these CLIs try
        # to invoke the benchmark's fictional tools and answer with the failure.
        full = f"{EXECUTION_FRAMING}\n{system_prompt}\n\n{prompt}" \
            if system_prompt else f"{EXECUTION_FRAMING}\n{prompt}"

        # The contract applies only where a workflow was asked for. A sub-agent
        # is asked for a TOOL CALL, and checking its correct answer for
        # `workflow_` failed it and retried the whole chain — see
        # `contract.expects_workflow` for what that cost.
        governed = self.arm != "solo" and expects_workflow(system_prompt)
        chain = (retry_chain(self.provider, self.retry_chain) if governed
                 else (self.provider,))

        last_text = ""
        for index, provider_id in enumerate(chain):
            result = await self._run(provider_id, full)
            self._account(result)
            text = result.text or ""
            ok = bool(result.ok)
            passed = ok and carries_workflow(text)
            self.attempts.append({
                "index": index, "provider": provider_id, "ok": ok,
                "carries_workflow": passed,
                "error": (result.error or "")[:200] or None,
                "chars": len(text)})
            if ok:
                last_text = text or last_text
            # An ungoverned turn returns whatever came back. That is the solo
            # arm by definition, and it is also every sub-agent turn on the
            # dobby arm: those were asked for a tool call, not a workflow.
            if not governed or passed:
                return text
        return last_text

    async def generate_chat_response(self, messages: List[Dict[str, str]],
                                     **kwargs) -> Dict[str, Any]:
        """A MESSAGE DICT, not a string, whatever the base class annotates.

        `BaseModel.generate_chat_response` is typed `-> str`, and the caller
        passes the result straight to `type_utils.normalize_chat_response`,
        which accepts an SDK message object or a dict carrying `role` — and
        raises `ValueError: Unsupported chat response format` on a bare string.
        Measured by returning one. The annotation is the thing that is wrong
        here, so this follows the consumer.

        `tool_calls` is empty on purpose: these CLIs answer in prose carrying a
        workflow document, and the benchmark's scorer reads that document out of
        `content`. Synthesising tool-call objects the provider never emitted
        would be inventing structure to be graded on.
        """
        system = "\n\n".join(m.get("content", "") for m in messages
                             if m.get("role") == "system")
        body = "\n\n".join(f"{m.get('role')}: {m.get('content', '')}"
                           for m in messages if m.get("role") != "system")
        # The caller hands the agent card's tools over as a kwarg, for an API's
        # `tools=` parameter. A CLI has no such channel, so they go into the
        # prompt or they never reach the model — see `contract.render_tools` for
        # what dropping them measured.
        tools = render_tools(kwargs.pop("tools", None))
        if tools:
            system = f"{system}\n\n{tools}" if system else tools
        text = await self.generate_response(body, system_prompt=system or None,
                                            **kwargs)
        # An API model returns its call in a structured field; a CLI
        # returns text. `parse_tool_call` puts it back into the shape
        # `eval_utils` reads, and returns [] for the XML rejection
        # answers so a refusal is never rewritten as an action.
        return {"role": "assistant", "content": text,
                "tool_calls": parse_tool_call(text)}

    # -- what the arm did, for the report ------------------------------------

    def orchestration_stats(self) -> Dict[str, Any]:
        return summarise(self.attempts, arm=self.arm, provider=self.provider,
                         cli_model=self.cli_model)

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": self.orchestration_stats(),
                       "usage": self.get_usage_info(),
                       "attempts": self.attempts}, fh, ensure_ascii=False,
                      indent=1)
