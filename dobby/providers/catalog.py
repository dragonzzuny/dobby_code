"""The provider catalog: how to invoke each supported agent runner, one-shot.

Every CLI invocation below was read off that tool's own `--help` on a real
install and, where marked in `verified_on`, executed. The distinction matters:
a wrong flag does not fail loudly, it makes the CLI drop into INTERACTIVE mode
and hang until the timeout — a fan-out of six then costs six timeouts and
returns nothing. So the non-interactive flag for each tool is treated as the
critical fact of its spec:

| provider | non-interactive flag        | machine-readable output      |
|----------|-----------------------------|------------------------------|
| claude   | `-p/--print`                | `--output-format json`       |
| codex    | `exec` subcommand           | `--json` (JSONL events)      |
| gemini   | `-p/--prompt`               | `-o json`                    |
| agy      | `--print`                   | (text only)                  |
| qwen     | `-p/--prompt`               | (text only)                  |
| ollama   | `run <model> <prompt>`      | (text only)                  |

`--output-format json` is deliberately NOT the default here. The JSON envelopes
differ per tool and several wrap the answer in a session record whose shape
changes between versions; parsing them is a maintenance liability for no gain
when the caller wants prose. `run.py` therefore asks for text and treats the
whole stdout as the answer. Structured mode stays available through `extra`.

## Network providers are opt-in

The MCP gateway in this kit ships with no network tool at all, which structurally
removes the exfiltration leg of the "lethal trifecta" (untrusted input + secret
access + a way out). API-kind providers below reintroduce that leg: they send
prompt text to a third party. They are therefore gated behind an explicit config
flag (`providers.allow_network`) and are absent from every default routing table.
Turning them on is a real threat-model change, documented in docs/THREAT_MODEL.md.
"""

from __future__ import annotations

from typing import Sequence

from .base import ProviderRegistry, ProviderSpec

# --------------------------------------------------------------------------
# CLI argv builders.
#
# Each returns a full argv list. `extra` is appended verbatim so a caller can
# reach any flag the builder does not model, without the builder having to
# grow a parameter per tool.
# --------------------------------------------------------------------------

def _claude(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # `-p` is print/non-interactive. Permission mode is pinned to `plan`
    # (read-only) by DEFAULT so a scout cannot silently edit the tree; callers
    # that want edits pass `--permission-mode acceptEdits` through `extra`,
    # which appears later in argv and therefore wins.
    argv = ["claude", "-p", prompt, "--permission-mode", "plan"]
    if model:
        argv += ["--model", model]
    return argv + list(extra)


def _codex(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # `exec` is the documented non-interactive entry point ("Run Codex
    # non-interactively"). The prompt is positional.
    argv = ["codex", "exec", prompt]
    if model:
        argv += ["--model", model]
    return argv + list(extra)


def _gemini(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # `--approval-mode plan` is the read-only mode; it is the safe default for
    # the same reason as claude's `plan`.
    argv = ["gemini", "-p", prompt, "--approval-mode", "plan"]
    if model:
        argv += ["--model", model]
    return argv + list(extra)


def _agy(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # Antigravity CLI: `--print` runs one prompt non-interactively. It also has
    # `--mode plan`, used here for the same read-only default.
    argv = ["agy", "--print", prompt, "--mode", "plan"]
    if model:
        argv += ["--model", model]
    return argv + list(extra)


def _qwen(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # qwen-code is a Gemini-CLI derivative and keeps `-p` for headless runs.
    # NOT verified on this machine (binary absent) — availability is probed.
    argv = ["qwen", "-p", prompt]
    if model:
        argv += ["--model", model]
    return argv + list(extra)


def _ollama(prompt: str, model: str | None, extra: Sequence[str]) -> list[str]:
    # Local weights (llama / qwen / mistral / gpt-oss ...). Model is REQUIRED by
    # the CLI's grammar: `ollama run <model> <prompt>`. A default is supplied so
    # a caller that only says "give me a local model" still produces valid argv.
    argv = ["ollama", "run", model or "llama3.1", prompt]
    return argv + list(extra)


# --------------------------------------------------------------------------
# The catalog.
# --------------------------------------------------------------------------

#: Platform tag used in `verified_on` for observations made on native Windows.
WIN = "win32"

# WHEN AND HOW `verified_on=(WIN,)` WAS ACTUALLY EARNED
#
# This field asserts that the invocation was EXECUTED, not merely that the tool
# was installed. It carried that assertion for all four CLIs long before it was
# true: `run_provider` launched the bare binary name with shell=False, and on
# Windows `shutil.which` consults PATHEXT while CreateProcess does not, so any
# provider shipped as an npm `.CMD` shim could never start. The claim was
# unfalsifiable in practice because nothing had ever run a live call.
#
# First real `dobby fleet --probe` run, 2026-07-26, native Windows 11, after
# argv[0] was changed to the resolved path:
#
#   claude   ok, replied DOBBY_OK exactly           29.1s
#   agy      ok, replied DOBBY_OK exactly           14.6s
#   codex    ok, replied DOBBY_OK exactly           31.0s   (0.14s failure before)
#   gemini   launched, reached the service, refused 22.2s
#
# gemini keeps WIN because what `verified_on` describes IS verified: the CLI
# accepted the argv, ran non-interactively, and reached authentication. It then
# returned IneligibleTierError - "This client is no longer supported for Gemini
# Code Assist for individuals" - which is a condition of the account, not of the
# invocation. Detection reports it usable and a live probe reports why it is not;
# collapsing those two into one flag is what hid the .CMD defect for so long.
#
# Anything below with `verified_on=()` has never been executed here. That empty
# tuple is the honest state and must not be filled in from a plausible reading of
# a tool's documentation.

CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="claude", kind="cli", display="Claude Code", binary="claude",
        argv=_claude, cost_tier="premium",
        capabilities=("files", "shell", "web", "vision", "long_context"),
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Deepest tool use and longest context of the CLI set; default "
              "choice for synthesis and adjudication roles.",
    ),
    ProviderSpec(
        id="codex", kind="cli", display="OpenAI Codex CLI", binary="codex",
        argv=_codex, cost_tier="premium",
        capabilities=("files", "shell", "long_context"),
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Strong at focused code edits and repo-scoped review "
              "(`codex exec review`). Sandboxed by default.",
    ),
    ProviderSpec(
        id="gemini", kind="cli", display="Gemini CLI", binary="gemini",
        argv=_gemini, cost_tier="standard",
        capabilities=("files", "shell", "web", "vision", "long_context"),
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Large context and a real web tool; the default scout for "
              "breadth-first exploration.",
    ),
    ProviderSpec(
        id="agy", kind="cli", display="Antigravity CLI", binary="agy",
        argv=_agy, cost_tier="standard",
        capabilities=("files", "shell", "long_context"),
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Exposes several model families behind one CLI (`agy models`), "
              "including a reasoning-effort dial (--effort).",
    ),
    ProviderSpec(
        id="qwen", kind="cli", display="Qwen Code CLI", binary="qwen",
        argv=_qwen, cost_tier="cheap",
        capabilities=("files", "shell", "long_context"),
        timeout_s=600, mutates_worktree=True, verified_on=(),
        notes="Declared, NOT verified here (binary absent on the authoring "
              "machine). Install: npm i -g @qwen-code/qwen-code.",
    ),
    ProviderSpec(
        id="ollama", kind="cli", display="Ollama (local weights)",
        binary="ollama", argv=_ollama, cost_tier="local",
        capabilities=("long_context",),
        timeout_s=1200, mutates_worktree=False, verified_on=(),
        notes="Declared, NOT verified here. Runs llama/qwen/mistral locally: "
              "no network egress, no per-token cost, weaker tool use. Ideal "
              "for high-volume mechanical roles (dedupe, classify, rank).",
    ),
    # -- api kinds: network egress, opt-in only ------------------------------
    ProviderSpec(
        id="kimi", kind="api", display="Moonshot Kimi (OpenAI-compatible)",
        binary=None, argv=None, cost_tier="cheap",
        capabilities=("long_context",),
        required_env=("MOONSHOT_API_KEY",),
        timeout_s=300, mutates_worktree=False, verified_on=(),
        notes="Declared, NOT verified here. OpenAI-compatible endpoint; needs "
              "providers.allow_network=true AND MOONSHOT_API_KEY.",
    ),
    ProviderSpec(
        id="dashscope", kind="api",
        display="Qwen via DashScope (OpenAI-compatible)",
        binary=None, argv=None, cost_tier="cheap",
        capabilities=("long_context", "vision"),
        required_env=("DASHSCOPE_API_KEY",),
        timeout_s=300, mutates_worktree=False, verified_on=(),
        notes="Declared, NOT verified here. Hosted Qwen; same gating as kimi.",
    ),
)


def registry() -> ProviderRegistry:
    """The full declared catalog. Availability is NOT consulted here — call
    `detect.survey()` for what actually exists on this machine."""
    return ProviderRegistry(CATALOG)


# --------------------------------------------------------------------------
# Role routing.
#
# Roles are what the orchestrator asks for; providers are what it gets. The
# ordering in each list is a PREFERENCE, and `detect.resolve_role` walks it
# until it finds something available — so a machine with only `gemini` still
# fills every role, just with less diversity.
#
# The routing principle: breadth roles go cheap and MANY, decision roles go
# expensive and FEW. A fan-out whose every member is premium-tier costs more
# than the disagreement it surfaces is worth.
# --------------------------------------------------------------------------

ROLE_ROUTING: dict[str, tuple[str, ...]] = {
    # Breadth-first exploration; wrong answers are cheap because a later stage
    # verifies. Web capability matters most here.
    "scout": ("gemini", "qwen", "agy", "ollama", "claude"),
    # Independent drafting for the NGT phase of a decorrelated fan-out. Deliberately
    # ordered to put DIFFERENT model families adjacent, so the first three picks
    # are maximally uncorrelated rather than three flavours of one vendor.
    "draft": ("claude", "gemini", "codex", "qwen", "agy", "ollama"),
    # Focused code edits inside a worktree.
    "implement": ("codex", "claude", "agy", "gemini", "qwen"),
    # Adversarial checking. Must NOT be the same provider that drafted — the
    # orchestrator enforces that; this list only states preference.
    "critic": ("codex", "claude", "gemini", "agy", "qwen"),
    # High-volume mechanical work: dedupe, classify, extract, rank. Cheapest
    # first, because volume is the cost driver and the task is not subtle.
    "mechanical": ("ollama", "qwen", "kimi", "dashscope", "gemini", "agy"),
    # Final judgment. Deepest available, and only ONE runs.
    "synthesize": ("claude", "codex", "gemini", "agy"),
    "adjudicate": ("claude", "codex", "gemini", "agy"),
}

#: Roles that must never be filled by an api-kind provider even when network is
#: allowed: they see the whole aggregated context, which is the highest-value
#: payload in the system and the worst thing to ship to a third party.
LOCAL_ONLY_ROLES = frozenset({"synthesize", "adjudicate"})


def role_preference(role: str) -> tuple[str, ...]:
    """Provider ids for `role`, most-preferred first."""
    try:
        return ROLE_ROUTING[role]
    except KeyError:
        raise KeyError(
            f"unknown role {role!r}; known: {sorted(ROLE_ROUTING)}") from None
