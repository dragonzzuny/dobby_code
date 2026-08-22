"""Worker adapters — the only place the runtime touches the outside world.

An agent is not the unit of work here. A NODE is, and a worker is whatever
executes one: a shell command, a provider CLI, or a function. That inversion is
the point. It means the retry policy, the budget, the contract and the verifier
are written once against nodes, and adding a provider adds an adapter rather
than a second orchestration path.

Every adapter returns the same `WorkerResult`, and every failure comes back
CLASSIFIED. An adapter that returns a bare exception makes the scheduler guess,
and the scheduler guessing is how a permanently broken call gets retried three
times.

Nothing here reaches the network by itself: `ProviderWorker` drives the CLIs the
existing `providers/` package already detects, under the same rules — timeout
mandatory, stdin closed, output capped, secrets redacted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

from ..core.platform import child_env, resolve_command
from . import contextpack, effects
from .contracts import (EXTERNAL_IRREVERSIBLE, EXTERNAL_REVERSIBLE,
                        LOCAL_WRITE, SCHEMAS)
from .failures import (EFFECT_NOT_OBSERVED, Failure, PERMISSION_DENIED,
                       classify_command_failure, classify_provider_error,
                       CONTRACT_VIOLATION)

#: Side-effect classes that mean this node is expected to CHANGE something, and
#: therefore both needs a permission and owes an observable trace.
WRITING_CLASSES = frozenset({LOCAL_WRITE, EXTERNAL_REVERSIBLE,
                             EXTERNAL_IRREVERSIBLE})

#: A node's own wall clock. Separate from the provider timeout because a node
#: may be a test suite rather than a model call.
DEFAULT_NODE_TIMEOUT_S = 900


@dataclass
class WorkerResult:
    """What one attempt produced, or why it did not."""

    ok: bool
    #: The structured output, once parsed. `None` when the attempt failed.
    payload: object = None
    #: What the worker actually emitted, capped. Kept even on success, because a
    #: schema-clean payload extracted from prose is worth being able to audit.
    raw: str = ""
    failure: Failure | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "payload": self.payload,
                "raw_chars": len(self.raw),
                "failure": self.failure.to_dict() if self.failure else None,
                "meta": self.meta}


class WorkerAdapter:
    """Run one node attempt.

    `run` must not raise for an ordinary failure — a timeout, a non-zero exit, a
    refused prompt are all results. It may raise only for a programming error,
    which the runner records as a PERMANENT_FAILURE with the traceback text.
    """

    name = "base"

    def run(self, node, context: dict) -> WorkerResult:  # pragma: no cover
        raise NotImplementedError


# -- extracting structure from prose ----------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def extract_json(text: str):
    """Find the JSON object in a model's answer, or return None.

    Models put JSON inside fences, after a sentence of preamble, or with a
    trailing apology. Refusing anything that is not a bare document would fail
    on outputs that are, in substance, exactly right — and accepting prose as a
    payload is the failure this whole module exists to prevent. So: try the
    document, then fenced blocks, then the outermost brace-balanced span.
    """
    if not text:
        return None
    candidates = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(text))

    start = text.find("{")
    if start != -1:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(text)):
            char = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


# -- adapters ----------------------------------------------------------------

class CommandWorker(WorkerAdapter):
    """Run a shell command. The deterministic worker.

    Nodes that can be a command should be: a test suite, a linter, a build, a
    generator. They cost nothing, they are reproducible, and their failure text
    is the repair instruction. `config`:

        command   the command template; `{python}` is substituted
        parse     "json" to parse stdout as the payload, otherwise the payload
                  is a test_report (command, exit_code, stdout_tail)
        cwd       working directory, relative to the repo
        timeout_s wall clock
    """

    name = "command"

    def run(self, node, context: dict) -> WorkerResult:
        command = node.config.get("command") or node.instruction
        if not command:
            return WorkerResult(
                False, failure=Failure(
                    "NON_RETRYABLE",
                    f"node {node.node_id!r} uses the command worker and defines "
                    f"no command"))
        command = resolve_command(command)
        repo = context.get("repo") or os.getcwd()
        cwd = os.path.join(repo, node.config.get("cwd", "")) if node.config.get(
            "cwd") else repo
        timeout = node.config.get("timeout_s", DEFAULT_NODE_TIMEOUT_S)
        meta = {"command": command, "cwd": cwd, "timeout_s": timeout}
        try:
            proc = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=child_env(),
                timeout=timeout)
        except subprocess.TimeoutExpired:
            return WorkerResult(
                False, failure=classify_command_failure(
                    -1, f"no output before the {timeout}s wall clock",
                    timed_out=True), meta=meta)
        except OSError as exc:
            return WorkerResult(
                False, failure=Failure("NON_RETRYABLE",
                                       f"cannot run {command!r}: {exc}"),
                meta=meta)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        meta["exit_code"] = proc.returncode
        if proc.returncode != 0:
            return WorkerResult(
                False, raw=stdout[-4000:],
                failure=classify_command_failure(proc.returncode, stderr),
                meta=meta)

        if node.config.get("parse") == "json":
            parsed = extract_json(stdout)
            if parsed is None:
                return WorkerResult(
                    False, raw=stdout[-4000:],
                    failure=Failure(
                        CONTRACT_VIOLATION,
                        "the command succeeded and its stdout is not JSON, "
                        "which this node declared it would be",
                        {"stdout_tail": stdout[-400:]}),
                    meta=meta)
            return WorkerResult(True, payload=parsed, raw=stdout[-4000:],
                                meta=meta)

        return WorkerResult(
            True,
            payload={"command": command, "exit_code": proc.returncode,
                     "stdout_tail": stdout[-2000:]},
            raw=stdout[-4000:], meta=meta)


class ProviderWorker(WorkerAdapter):
    """Drive one agent CLI through the existing provider layer.

    `config`:

        provider   provider id (`claude`, `codex`, `gemini`, `agy`, ...)
        model      optional model override
        schema     name in `contracts.SCHEMAS`, or the node's own output_schema
        timeout_s  wall clock

    The prompt is assembled here rather than by the caller so every provider
    node states its output contract in the same words. A model told "return
    JSON" and a model shown the schema produce different rates of the
    CONTRACT_VIOLATION this runtime would otherwise have to repair.
    """

    name = "provider"

    def run(self, node, context: dict) -> WorkerResult:
        from ..providers.catalog import registry
        from ..providers.run import run_provider

        provider_id = node.config.get("provider")
        if not provider_id:
            return WorkerResult(False, failure=Failure(
                "NON_RETRYABLE",
                f"node {node.node_id!r} uses the provider worker and names no "
                f"provider"))

        # RETRY_ELSEWHERE means elsewhere. The failure handler records who just
        # failed; this is where that record has to change the call, or the
        # policy is a note nobody acts on and a rate-limited provider gets asked
        # three more times.
        avoid = set(node.config.get("avoid_providers") or ())
        substituted = ""
        if provider_id in avoid:
            alternative = self._alternative(provider_id, avoid, node)
            if alternative is None:
                return WorkerResult(False, failure=Failure(
                    "NON_RETRYABLE",
                    f"{provider_id} must be avoided after its last failure and "
                    f"this machine has no other usable provider; install a "
                    f"second one or wait out the window"))
            substituted = provider_id
            provider_id = alternative

        try:
            spec = registry().get(provider_id)
        except Exception as exc:  # noqa: BLE001 - catalog raises its own types
            return WorkerResult(False, failure=Failure(
                "NON_RETRYABLE", f"unknown provider {provider_id!r}: {exc}"))

        prompt = self._prompt(node, context)
        root = context.get("cwd") or context.get("repo")

        # THE DECLARED SIDE EFFECT HAS TO REACH THE CLI AS A PERMISSION.
        # It did not, and the measured consequence was a node that returned ok
        # having created nothing: `write_extra` is the catalog's opt-in for a CLI
        # that may edit files and this call never passed it, so claude ran under
        # its default `--permission-mode plan`. The grant is the MINIMUM: only a
        # writing class gets it, a read-only node is unchanged, and a provider
        # whose write flag nobody verified has an empty tuple and gets nothing.
        writes = node.contract.side_effect_class in WRITING_CLASSES
        grant = tuple(spec.write_extra) if writes else ()
        # Isolation is what licenses the headless grant, so the scheduler's view
        # of the workspace decides it, not the node.
        if context.get("isolated") and spec.isolated_extra:
            grant = grant + tuple(spec.isolated_extra)
        if writes and not grant:
            return WorkerResult(False, failure=Failure(
                PERMISSION_DENIED,
                f"node {node.node_id!r} declares "
                f"{node.contract.side_effect_class} and nobody has verified how "
                f"to grant {provider_id} edit rights (its write_extra is "
                f"empty). Running it read-only would produce a call that "
                f"succeeds and changes nothing",
                evidence={"provider": provider_id,
                          "side_effect_class": node.contract.side_effect_class}))

        before = (effects.snapshot(root, node.contract.expected_paths)
                  if writes and root else None)
        result = run_provider(
            spec, prompt, model=node.config.get("model"),
            cwd=root, extra=grant,
            timeout_s=node.config.get("timeout_s"))
        meta = {"provider": provider_id, "model": node.config.get("model"),
                "duration_s": result.duration_s, "prompt_chars": len(prompt)}
        if substituted:
            # Recorded, never silent. A run whose answer came from a different
            # model than the one it was asked for has to say so.
            meta["substituted_for"] = substituted

        if not result.ok:
            return WorkerResult(
                False, raw=result.text or "",
                failure=classify_provider_error(
                    result.error or "", exit_code=result.exit_code,
                    empty_output=(result.exit_code == 0
                                  and not (result.text or "").strip())),
                meta=meta)

        # FAIL-CLOSED ON THE DECLARED EFFECT, AND ON THAT ALONE.
        #
        # The first version of this checked `permission_denials` FIRST and failed
        # any writing node that saw one. The A/B pilot caught it within nine
        # runs: claude fixed the file correctly and was refused five unrelated
        # tools along the way, so a node that had done exactly what it was asked
        # was reported as a permission failure. That is the same defect this
        # module was written to fix, pointing the other way — a verdict with no
        # basis, this time a negative one.
        #
        # So the EFFECT decides. Denials are recorded either way, and they only
        # change the diagnosis when the effect is missing: then "it could not"
        # is a better answer than "it did not", and it names a fix the operator
        # can make.
        denials = result.meta.get("permission_denials") or []
        if before is not None:
            happened, detail = effects.observed(
                before, effects.snapshot(root, node.contract.expected_paths))
            meta["effect_observed"] = happened
            meta["effect_detail"] = detail
            if denials:
                # Kept even on success: a run that got its work done while being
                # refused tools is worth seeing before the day it does not.
                meta["permission_denials"] = len(denials)
            if not happened and denials:
                return WorkerResult(
                    False, raw=result.text, meta=meta,
                    failure=Failure(
                        PERMISSION_DENIED,
                        f"{provider_id} was refused {len(denials)} tool "
                        f"permission(s) and {detail}. The call returned; the "
                        f"work was not permitted",
                        evidence={"provider": provider_id,
                                  "denials": denials[:5]}))
            if not happened:
                return WorkerResult(
                    False, raw=result.text, meta=meta,
                    failure=Failure(
                        EFFECT_NOT_OBSERVED,
                        f"node {node.node_id!r} declares "
                        f"{node.contract.side_effect_class} and the call "
                        f"reported success, but {detail}. A worker that changed "
                        f"nothing has not done the work its output describes",
                        evidence={"provider": provider_id,
                                  "expected_paths":
                                      list(node.contract.expected_paths)}))

        schema = self._schema(node)
        if not schema:
            return WorkerResult(True, payload={"summary": result.text},
                                raw=result.text, meta=meta)
        parsed = extract_json(result.text)
        if parsed is None:
            return WorkerResult(
                False, raw=result.text,
                failure=Failure(
                    CONTRACT_VIOLATION,
                    "the provider answered in prose where the node declared a "
                    "JSON output schema",
                    {"text_tail": result.text[-400:]}),
                meta=meta)
        return WorkerResult(True, payload=parsed, raw=result.text, meta=meta)

    @staticmethod
    def _alternative(failed: str, avoid: set, node) -> str | None:
        """Another usable provider, or None.

        `allow_network` stays False: a substitution must not quietly turn a
        local-CLI run into one that leaves the machine. A run that wants api
        providers says so in its config, and this is not the place it gets to
        decide that.
        """
        from ..providers.detect import available_ids
        candidates = [pid for pid in available_ids(allow_network=False)
                      if pid not in avoid and pid != failed]
        preferred = node.config.get("fallback_providers") or []
        for pid in preferred:
            if pid in candidates:
                return pid
        return candidates[0] if candidates else None

    @staticmethod
    def _schema(node) -> dict:
        named = node.config.get("schema")
        if named and named in SCHEMAS:
            return SCHEMAS[named]
        return node.contract.output_schema or {}

    def _prompt(self, node, context: dict) -> str:
        """Delegated to `runtime/contextpack.py`.

        This used to inline every promoted dependency as
        `json.dumps(inputs, indent=1)[:8000]`. A character cap is not a
        relevance budget, and three things followed from that: it truncated by
        SERIALISATION ORDER, so the evidence a worker most needed was the
        evidence most likely to be missing; it re-injected a whole plan payload
        into an implementer that already had the task; and it moved the stable
        prefix of every call, which is the part a prompt cache wants held still.

        The pack ranks first, budgets second, and lists by id what it left out —
        so a worker that did not use evidence it never received is diagnosable
        from the record rather than mistaken for one that ignored it.
        """
        return contextpack.build(node, context,
                                 schema=self._schema(node)).render()


class AdvisoryJudgeWorker(WorkerAdapter):
    """The semantic verifier layer — as an ordinary node, and always advisory.

    Model judgment is not moved inside the gate. It runs as a node, which means
    it costs a visible provider call, appears in the trace, and is subject to
    the same budget as everything else. A semantic verdict that arrived free and
    unbudgeted inside `Verifier.verify` would be consulted on every artifact and
    nobody would see the bill.

    It is ADVISORY without exception. `dobby/judge.py` stamps `advisory=True` on
    every record it returns precisely so a caller cannot mistake it for a
    measurement, and `.dobby/ontology.json` forbids a model assertion from
    counting as verification. This adapter therefore never fails a node on the
    judge's opinion: a FAIL verdict is recorded as data, not raised as an error.
    Where a deterministic check exists it outranks this; where none exists, this
    is a second opinion, which is a different thing from a result.

    `config`:

        criterion    {"id": ..., "description": "what to grade"}
        judge_of     node id whose promoted payload is the artifact
        provider     optional; otherwise the catalog's `critic` role
        exclude      providers that must not judge — the author, above all
    """

    name = "judge"

    def run(self, node, context: dict) -> WorkerResult:
        from ..judge import judge_criterion

        criterion = node.config.get("criterion") or {
            "id": node.node_id,
            "description": node.instruction or "does this satisfy the request?"}
        source = node.config.get("judge_of")
        inputs = context.get("inputs") or {}
        artifact = inputs.get(source) if source else inputs
        if artifact is None:
            return WorkerResult(False, failure=Failure(
                "NON_RETRYABLE",
                f"node {node.node_id!r} judges {source!r}, which produced no "
                f"promoted artifact — there is nothing to grade"))

        exclude = set(node.config.get("exclude") or ())
        record = judge_criterion(
            criterion,
            json.dumps(artifact, ensure_ascii=False, indent=1, default=str),
            provider_id=node.config.get("provider"),
            exclude=exclude,
            cwd=context.get("repo"))

        # A judge that could not run is NOT a pass and NOT a fail. It is an
        # unrun check, and `judge.py` already says so in `evidence`. Reporting
        # it as a verdict is the failure this whole module is built against.
        if record.get("verdict_token") is None and record.get("evidence"):
            return WorkerResult(
                False, raw=str(record.get("evidence")),
                failure=Failure("POLICY_BLOCKED", str(record["evidence"]),
                                {"criterion": criterion.get("id")}),
                meta={"advisory": True})

        return WorkerResult(True, payload=record,
                            raw=json.dumps(record, ensure_ascii=False),
                            meta={"advisory": True,
                                  "judge_provider": record.get("judge_provider")})


class StaticWorker(WorkerAdapter):
    """Return a payload from the node's own config.

    Its purpose is testing the kernel — leases, resume, promotion, retry classes
    — without spending a provider call or depending on a machine having one
    installed. `config`:

        payload    returned as-is
        fail_with  a failure class; makes the attempt fail deterministically
        fail_times attempts to fail before succeeding, so a retry policy can be
                   exercised end to end
    """

    name = "static"

    def run(self, node, context: dict) -> WorkerResult:
        fail_with = node.config.get("fail_with")
        fail_times = node.config.get("fail_times", 0)
        attempt = context.get("attempt", 1)
        if fail_with and (not fail_times or attempt <= fail_times):
            return WorkerResult(
                False,
                failure=Failure(fail_with,
                                node.config.get("fail_detail",
                                                "deterministic test failure")),
                meta={"attempt": attempt})
        return WorkerResult(True, payload=node.config.get("payload", {}),
                            raw=json.dumps(node.config.get("payload", {}),
                                           ensure_ascii=False),
                            meta={"attempt": attempt})


class WorkerRegistry:
    """Name -> adapter. Callers may add their own without touching the runner."""

    def __init__(self, adapters: dict | None = None):
        self.adapters: dict[str, WorkerAdapter] = dict(adapters or {})
        for adapter in (CommandWorker(), ProviderWorker(), StaticWorker(),
                        AdvisoryJudgeWorker()):
            self.adapters.setdefault(adapter.name, adapter)

    def get(self, name: str) -> WorkerAdapter:
        if name not in self.adapters:
            raise KeyError(
                f"no worker adapter {name!r}; registered: "
                f"{sorted(self.adapters)}")
        return self.adapters[name]

    def add(self, adapter: WorkerAdapter) -> None:
        self.adapters[adapter.name] = adapter
