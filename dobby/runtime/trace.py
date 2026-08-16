"""One trace per run, so every question about it has one place to look.

The problem this fixes
----------------------
The kit already records valuable things and records them in four incompatible
shapes: the spend ledger knows agent seconds per provider, the trajectory knows
decisions, `ProviderResult.meta` knows the argv and the duration, and the run
store knows attempts and artifacts. None of them share a correlation id, so a
question that crosses two of them — *which model made this run expensive*, *did
the wrong answer start at retrieval* — cannot be answered by reading; it has to
be reconstructed by a human matching timestamps.

A span model fixes that by construction. Every span carries the ids that let it
be joined to every other: `trace_id` (the run), `parent_span_id` (the caller),
`run_id`/`node_id`/`attempt`, and the artifact it produced. The field names
follow OpenTelemetry so the data survives a change of backend — this stores to
SQLite because the kit has no infrastructure, and nothing about the model
assumes that.

Required attributes are enforced
--------------------------------
Each span kind declares the attributes without which it cannot answer its own
question. A `agent.generation` span with no `provider` cannot answer "which
model", and recording it anyway produces a dashboard that is complete-looking
and useless. `Tracer.span` raises instead. That is a deliberate choice to fail
at write time, where the fix is one line, rather than at read time three weeks
later.

What this is NOT
----------------
It is not sampling, not distributed, and not real-time. Every span is written,
because a run here is minutes of provider calls and not a million requests a
second; the write cost is irrelevant next to what it observes.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field

#: Span kinds, and the question each one exists to answer.
ORCHESTRATOR_PLAN = "orchestrator.plan"      # why did this task go to N steps?
SCHEDULER_DECISION = "scheduler.decision"    # why was this provider chosen?
AGENT_GENERATION = "agent.generation"        # which model, at what cost?
TOOL_CALL = "tool.call"                      # which tool failed or looped?
RETRIEVAL = "retrieval"                      # did the wrong answer start here?
VERIFIER = "verifier"                        # which criterion breaks most?
NODE = "node"                                # how long did this step take?
RUN = "run"                                  # the root

SPAN_KINDS = (ORCHESTRATOR_PLAN, SCHEDULER_DECISION, AGENT_GENERATION,
              TOOL_CALL, RETRIEVAL, VERIFIER, NODE, RUN)

#: Attributes a span of each kind MUST carry. Enforced at write time.
REQUIRED_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    ORCHESTRATOR_PLAN: ("level", "model_tier"),
    SCHEDULER_DECISION: ("candidates", "chosen", "reason"),
    AGENT_GENERATION: ("provider",),
    TOOL_CALL: ("tool", "effect_class"),
    RETRIEVAL: ("query", "k"),
    VERIFIER: ("checks", "passed"),
    NODE: ("node_kind", "worker"),
    RUN: ("task",),
}

OK = "OK"
ERROR = "ERROR"
UNSET = "UNSET"
STATUSES = (OK, ERROR, UNSET)

#: Wall clock anchored once, then advanced by a monotonic counter.
#:
#: `time.time()` alone is not precise enough to order spans on Windows, where
#: its resolution is about 15.6ms — longer than most of the spans in a run.
#: Measured on the first traced run here: the root `run` span and the first
#: `admit` event inside it received the SAME timestamp, so the store's
#: `ORDER BY started_ms` put a child before its own parent and the rendered
#: timeline showed the run starting after the work it contained.
#:
#: `perf_counter` has sub-microsecond resolution but an arbitrary origin, so it
#: cannot be correlated across processes. Anchoring one to the other keeps both
#: properties: the absolute value is real wall clock, and two spans a
#: microsecond apart are still distinguishable.
_WALL_ANCHOR = time.time()
_PERF_ANCHOR = time.perf_counter()


def now_ms() -> float:
    """Wall-clock milliseconds, with monotonic resolution."""
    return (_WALL_ANCHOR + (time.perf_counter() - _PERF_ANCHOR)) * 1000.0


class TraceError(ValueError):
    """A span that cannot answer the question its kind exists for."""


def new_span_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Span:
    """One unit of work in a trace, OpenTelemetry-shaped."""

    span_id: str
    trace_id: str
    kind: str
    name: str
    parent_span_id: str | None = None
    run_id: str = ""
    node_id: str | None = None
    attempt: int | None = None
    started_ms: float = 0.0
    ended_ms: float | None = None
    status: str = UNSET
    attributes: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in SPAN_KINDS:
            raise TraceError(f"unknown span kind {self.kind!r}; "
                             f"expected one of {SPAN_KINDS}")
        if self.status not in STATUSES:
            raise TraceError(f"unknown span status {self.status!r}")
        if not self.started_ms:
            self.started_ms = now_ms()

    @property
    def duration_ms(self) -> float | None:
        if self.ended_ms is None:
            return None
        return round(self.ended_ms - self.started_ms, 3)

    def end(self, status: str = OK, **attributes) -> "Span":
        self.ended_ms = now_ms()
        self.status = status
        self.attributes.update(attributes)
        return self

    def check_required(self) -> None:
        missing = [key for key in REQUIRED_ATTRIBUTES.get(self.kind, ())
                   if key not in self.attributes]
        if missing:
            raise TraceError(
                f"a {self.kind} span is missing {missing}; without them it "
                f"cannot answer the question its kind exists for, and a "
                f"complete-looking record that answers nothing is worse than a "
                f"gap")

    def to_dict(self) -> dict:
        return {"span_id": self.span_id, "trace_id": self.trace_id,
                "parent_span_id": self.parent_span_id, "kind": self.kind,
                "name": self.name, "run_id": self.run_id,
                "node_id": self.node_id, "attempt": self.attempt,
                "started_ms": self.started_ms, "ended_ms": self.ended_ms,
                "duration_ms": self.duration_ms, "status": self.status,
                "attributes": dict(self.attributes)}


class Tracer:
    """Opens spans, nests them, and writes them to the store.

    The parent is taken from a stack rather than passed by every caller. Passing
    it explicitly is how a tree quietly becomes a list: one function forgets, and
    from then on every child is a sibling of the root, which reads as a flat run
    that never nested anything.
    """

    def __init__(self, store, run_id: str, *,
                 policy_version: str = "1", prompt_version: str = "1"):
        import threading
        self.store = store
        self.run_id = run_id
        #: One trace per run. Runs are the unit anybody asks about.
        self.trace_id = run_id
        self.policy_version = policy_version
        self.prompt_version = prompt_version
        #: THREAD-LOCAL. Nodes run concurrently, and a shared stack would make
        #: one thread's span the parent of another thread's — producing a tree
        #: that is not merely wrong but plausible, which is the worst kind.
        #: A thread that starts inside a span still needs that span as its
        #: parent, so the root is passed down explicitly via `child_of`.
        self._local = threading.local()

    @property
    def _stack(self) -> list[str]:
        if not hasattr(self._local, "stack"):
            self._local.stack = list(getattr(self, "_root_stack", []))
        return self._local.stack

    def child_of(self, span_id: str | None) -> "Tracer":
        """A view of this tracer whose new spans hang under `span_id`.

        Returned rather than mutated because the caller is usually handing it to
        a thread, and mutating the shared tracer would set the parent for
        everybody.
        """
        clone = Tracer(self.store, self.run_id,
                       policy_version=self.policy_version,
                       prompt_version=self.prompt_version)
        clone._root_stack = [span_id] if span_id else []
        return clone

    @contextlib.contextmanager
    def span(self, kind: str, name: str, *, node_id: str | None = None,
             attempt: int | None = None, **attributes):
        """Open a span, yield it, and write it however the block exits.

        An exception marks the span ERROR and re-raises. A span that vanishes
        when the code inside it fails is a trace that only records success,
        which is the opposite of what one is for.
        """
        span = Span(span_id=new_span_id(), trace_id=self.trace_id, kind=kind,
                    name=name, parent_span_id=self._stack[-1] if self._stack
                    else None,
                    run_id=self.run_id, node_id=node_id, attempt=attempt,
                    attributes={"policy_version": self.policy_version,
                                "prompt_version": self.prompt_version,
                                **attributes})
        self._stack.append(span.span_id)
        try:
            yield span
        except BaseException as exc:
            span.end(ERROR, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._stack.pop()
            if span.ended_ms is None:
                span.end(OK)
            try:
                span.check_required()
            except TraceError as exc:
                # The span is still written, with the violation recorded in it.
                # Losing the observation to enforce a rule about observations
                # would be its own defect; the loud version is the message.
                span.attributes["trace_violation"] = str(exc)
                span.status = ERROR
            self.store.record_span(span)

    def event(self, kind: str, name: str, **attributes) -> Span:
        """A point in time rather than an interval. Written immediately."""
        span = Span(span_id=new_span_id(), trace_id=self.trace_id, kind=kind,
                    name=name,
                    parent_span_id=self._stack[-1] if self._stack else None,
                    run_id=self.run_id,
                    attributes={"policy_version": self.policy_version,
                                "prompt_version": self.prompt_version,
                                **attributes})
        span.end(OK)
        try:
            span.check_required()
        except TraceError as exc:
            span.attributes["trace_violation"] = str(exc)
            span.status = ERROR
        self.store.record_span(span)
        return span


class NullTracer:
    """A tracer that records nothing, for callers that have no store.

    Exists so the runtime never has to write `if self.tracer is not None`. A
    conditional around every observation is how observations get skipped in the
    branch that mattered.
    """

    trace_id = ""

    @contextlib.contextmanager
    def span(self, kind: str, name: str, **kwargs):
        yield Span(span_id="", trace_id="", kind=kind, name=name)

    def event(self, kind: str, name: str, **attributes) -> None:
        return None

    def child_of(self, span_id: str | None) -> "NullTracer":
        return self


def to_otlp(spans: list[dict]) -> dict:
    """Render spans in OpenTelemetry's JSON shape.

    Provided so the claim "OTel-compatible" is a thing that can be checked
    rather than a word in a docstring. Nothing in this kit sends it anywhere —
    the engine makes no network calls — but an operator who wants this in their
    own collector should not have to write the mapping.
    """
    def _ns(ms: float | None) -> int:
        return int((ms or 0) * 1_000_000)

    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "dobby"}}]},
            "scopeSpans": [{
                "scope": {"name": "dobby.runtime"},
                "spans": [{
                    "traceId": s["trace_id"],
                    "spanId": s["span_id"],
                    "parentSpanId": s["parent_span_id"] or "",
                    "name": s["name"],
                    "startTimeUnixNano": _ns(s["started_ms"]),
                    "endTimeUnixNano": _ns(s["ended_ms"]),
                    "status": {"code": 1 if s["status"] == OK else 2},
                    "attributes": [
                        {"key": k, "value": {"stringValue": json.dumps(
                            v, ensure_ascii=False, default=str)
                            if not isinstance(v, str) else v}}
                        for k, v in ({"dobby.kind": s["kind"],
                                      "dobby.run_id": s["run_id"],
                                      "dobby.node_id": s["node_id"] or "",
                                      **s["attributes"]}).items()],
                } for s in spans],
            }],
        }],
    }


def render_timeline(spans: list[dict], *, width: int = 48) -> list[str]:
    """A one-line-per-span waterfall, for a human reading a run.

    JSON answers the machine's questions. The question a person actually has —
    *where did the time go* — is a shape, and a shape needs a picture.
    """
    if not spans:
        return ["(no spans recorded)"]
    starts = [s["started_ms"] for s in spans]
    ends = [s["ended_ms"] or s["started_ms"] for s in spans]
    t0, t1 = min(starts), max(ends)
    span_ms = max(1.0, t1 - t0)
    depth = _depths(spans)
    lines = []
    for s in spans:
        start = int((s["started_ms"] - t0) / span_ms * width)
        length = max(1, int(((s["ended_ms"] or s["started_ms"])
                             - s["started_ms"]) / span_ms * width))
        bar = " " * start + ("=" if s["status"] != ERROR else "!") * length
        label = "  " * depth.get(s["span_id"], 0) + s["name"]
        duration = s["duration_ms"]
        lines.append(f"{label[:34]:<34} |{bar[:width]:<{width}}| "
                     f"{'' if duration is None else f'{duration / 1000:.2f}s'}")
    return lines


def _depths(spans: list[dict]) -> dict:
    by_id = {s["span_id"]: s for s in spans}
    out: dict[str, int] = {}
    for span in spans:
        depth, cursor = 0, span
        seen = set()
        while cursor.get("parent_span_id") and cursor["span_id"] not in seen:
            seen.add(cursor["span_id"])
            parent = by_id.get(cursor["parent_span_id"])
            if parent is None:
                break
            depth += 1
            cursor = parent
        out[span["span_id"]] = depth
    return out
