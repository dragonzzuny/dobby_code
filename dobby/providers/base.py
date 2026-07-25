"""Provider abstraction: one uniform way to ask any coding agent for work.

A *provider* is an external model runner this harness can drive as a child
process (or, for API kinds, an HTTP endpoint). The point of the abstraction is
that the orchestrator picks providers by ROLE and COST, never by name — so a
project that only has one CLI installed still works, and a project with six gets
genuine parallelism without any orchestration code changing.

Three properties are load-bearing and deliberately explicit in the spec:

- **`availability` is measured, never assumed.** A spec declares how to probe
  for itself; `detect.py` runs the probe. Nothing in this package infers that a
  provider works because it is listed here. A declared-but-absent provider is
  reported as absent and skipped, which is why `ProviderSpec.verified_on`
  records where an invocation was actually observed to work rather than claiming
  universal support.

- **Cost tier drives routing, not capability worship.** Roles that need
  breadth (draft, scout, lint) route to cheap tiers; roles that decide
  (synthesize, adjudicate) route to expensive ones. Without this, a fan-out of
  eight agents on a frontier tier costs more than the work is worth.

- **Isolation is declared per provider.** A provider that edits files needs a
  worktree when run in parallel with another that does; a read-only provider
  does not. Encoding this on the spec keeps `fanout.py` from having to special-
  case individual tools.

`ProviderResult` is intentionally a plain record with no exception paths: a
provider failing (missing auth, timeout, non-zero exit) is an ordinary outcome
in a fan-out of six, not an error that should abort the other five. Callers
filter on `.ok`.
"""

from __future__ import annotations

import dataclasses
import shutil
from typing import Callable, Iterable, Sequence

#: Cost tiers, cheapest first. Used for role routing and for reporting the
#: expected relative spend of a fan-out before it runs.
COST_TIERS = ("local", "cheap", "standard", "premium")

#: Capabilities a provider may have. `files` = can read/write the working tree;
#: `shell` = can execute commands; `web` = can reach the network. The triple
#: matters for the "lethal trifecta" (untrusted input + secrets + exfiltration
#: path): a provider with all three, pointed at untrusted content, is the
#: dangerous configuration, so the set is recorded rather than implied.
CAPABILITIES = ("files", "shell", "web", "vision", "long_context")


class ProviderError(RuntimeError):
    """Raised only for programmer errors (unknown id, malformed spec) — never
    for a provider that merely failed to produce output."""


@dataclasses.dataclass(frozen=True)
class ProviderSpec:
    """How to detect, invoke, and budget one external agent runner."""

    id: str
    kind: str                      # "cli" | "api"
    display: str
    #: Executable to look for on PATH (cli kinds). None for api kinds.
    binary: str | None
    #: Builds the argv for a one-shot, non-interactive run of `prompt`.
    #: Signature: (prompt, model, extra) -> list[str]
    argv: Callable[[str, str | None, Sequence[str]], list[str]] | None
    cost_tier: str = "standard"
    capabilities: tuple[str, ...] = ()
    #: Env vars that must be present for an `api` provider to be usable. An
    #: empty tuple means the provider carries its own auth (CLI login state).
    required_env: tuple[str, ...] = ()
    #: Default per-call wall clock ceiling. Fan-outs multiply this by nothing —
    #: each child gets its own timer — so a hung provider cannot stall a round.
    timeout_s: int = 600
    #: True when running two of these concurrently in one working tree can
    #: corrupt it (both write files). Drives worktree isolation in fanout.
    mutates_worktree: bool = False
    #: Platforms where an invocation of this exact argv was OBSERVED to work.
    #: Empty means "not yet observed anywhere" — the honest default.
    verified_on: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("cli", "api"):
            raise ProviderError(f"{self.id}: kind must be 'cli' or 'api'")
        if self.cost_tier not in COST_TIERS:
            raise ProviderError(f"{self.id}: unknown cost tier {self.cost_tier!r}")
        for cap in self.capabilities:
            if cap not in CAPABILITIES:
                raise ProviderError(f"{self.id}: unknown capability {cap!r}")
        if self.kind == "cli" and (self.binary is None or self.argv is None):
            raise ProviderError(f"{self.id}: cli providers need binary and argv")

    # -- availability -------------------------------------------------------
    def which(self) -> str | None:
        """Absolute path of the provider binary, or None. CLI kinds only."""
        return shutil.which(self.binary) if self.binary else None

    def missing_env(self) -> list[str]:
        """Required env vars that are absent. Cheap, and never logs values."""
        import os
        return [v for v in self.required_env if not os.environ.get(v)]

    def build_argv(self, prompt: str, model: str | None = None,
                   extra: Sequence[str] = ()) -> list[str]:
        if self.argv is None:
            raise ProviderError(f"{self.id}: not a cli provider")
        return self.argv(prompt, model, tuple(extra))


@dataclasses.dataclass
class ProviderResult:
    """Outcome of one provider call. Failure is data, not an exception."""

    provider: str
    ok: bool
    text: str = ""
    exit_code: int | None = None
    duration_s: float = 0.0
    #: Set when output hit the size cap; the text is a prefix, not the whole
    #: answer, and downstream synthesis must be told so it does not treat a
    #: truncated answer as a complete one.
    truncated: bool = False
    error: str | None = None
    #: Free-form provenance for the ledger (argv shape, model, cwd).
    meta: dict = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        return d

    def short(self, limit: int = 160) -> str:
        """One-line form for progress logs and ledger rows."""
        if not self.ok:
            return f"{self.provider}: FAILED ({self.error})"
        body = " ".join(self.text.split())
        tail = "…" if len(body) > limit else ""
        flag = " [truncated]" if self.truncated else ""
        return f"{self.provider}: {body[:limit]}{tail}{flag}"


class ProviderRegistry:
    """Lookup over the declared provider catalog."""

    def __init__(self, specs: Iterable[ProviderSpec]):
        self._specs: dict[str, ProviderSpec] = {}
        for spec in specs:
            if spec.id in self._specs:
                raise ProviderError(f"duplicate provider id {spec.id!r}")
            self._specs[spec.id] = spec

    def __contains__(self, pid: object) -> bool:
        return pid in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def ids(self) -> list[str]:
        return sorted(self._specs)

    def get(self, pid: str) -> ProviderSpec:
        try:
            return self._specs[pid]
        except KeyError:
            raise ProviderError(
                f"unknown provider {pid!r}; known: {self.ids()}") from None

    def all(self) -> list[ProviderSpec]:
        return [self._specs[i] for i in self.ids()]

    def by_tier(self, *tiers: str) -> list[ProviderSpec]:
        want = set(tiers)
        return [s for s in self.all() if s.cost_tier in want]

    def with_capability(self, *caps: str) -> list[ProviderSpec]:
        want = set(caps)
        return [s for s in self.all() if want.issubset(set(s.capabilities))]
