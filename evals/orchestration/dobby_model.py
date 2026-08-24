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

from contract import (DEFAULT_RETRY_CHAIN, carries_workflow,  # noqa: E402
                      retry_chain, summarise)


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
        from dobby.providers.run import run_by_id
        from dobby.swebench import write_extra_for

        spec = registry().get(provider_id)
        extra = tuple(write_extra_for(provider_id)) + spec.workspace(os.getcwd())
        model = self.cli_model if provider_id == self.provider else None
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
        full = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        chain = ((self.provider,) if self.arm == "solo"
                 else retry_chain(self.provider, self.retry_chain))

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
            # A solo arm returns whatever came back, right or wrong. That IS the
            # arm: adding a check to it would make every arm the dobby arm.
            if self.arm == "solo" or passed:
                return text
        return last_text

    async def generate_chat_response(self, messages: List[Dict[str, str]],
                                     **kwargs) -> str:
        system = "\n\n".join(m.get("content", "") for m in messages
                             if m.get("role") == "system")
        body = "\n\n".join(f"{m.get('role')}: {m.get('content', '')}"
                           for m in messages if m.get("role") != "system")
        return await self.generate_response(body, system_prompt=system or None,
                                            **kwargs)

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
