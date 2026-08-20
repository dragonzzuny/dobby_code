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
| agy      | `--print`                   | `--output-format json`       |
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

from .base import (RO_CLAIMED, RO_DENIED, RO_UNKNOWN, RO_VERIFIED,
                   ProviderRegistry, ProviderSpec)

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
    # Antigravity CLI: `--print` runs one prompt non-interactively.
    #
    # `--mode plan` IS NOT A READ-ONLY GUARANTEE HERE, and this comment used to
    # say it was. Measured 2026-08-04 on agy 1.1.8 / win32, prompt = "create a
    # file named hello.txt whose content is DOBBY_WRITE_OK", each run in a fresh
    # temp directory:
    #
    #     --mode plan          --dangerously-skip-permissions   FILE CREATED
    #     --mode accept-edits  --dangerously-skip-permissions   FILE CREATED
    #     --mode plan          (no permission flag)             FILE CREATED
    #     --mode accept-edits  (no permission flag)             FILE CREATED
    #
    # Four for four. The flag is still sent, because it states the caller's
    # intent and costs nothing, but NOTHING may be built on top of it as a
    # containment control. What actually contains a delegate on this build is the
    # working directory it is launched in (`cwd`), the worktree isolation in
    # fanout.py, and the instruction in the prompt itself — which is why
    # dobby/agy.py writes "Do NOT modify" into the prompt text rather than
    # trusting the mode.
    #
    # Note the asymmetry that hid this: a prompt asking agy to READ a named file
    # was auto-denied headlessly for wanting the "command" permission, so the
    # first evidence pointed the other way — it looked more locked down than
    # claude, not less. Writing a new file needs no such permission.
    #
    # The default is DROPPED when the caller supplies its own `--mode`, instead of
    # being emitted and overridden. Every other builder here relies on extras
    # coming last and winning, which is verified for claude; for agy it would have
    # meant staking the difference between a read-only scout and one that rewrites
    # the tree on an unverified property of somebody else's flag parser. One
    # `--mode` reaches the process, always, and which one is decided here.
    #
    # Verbatim from `agy --help` 1.1.8 on win32: "--mode  Set the agent execution
    # mode for this session (accept-edits, plan)". Note `accept-edits`, not
    # claude's `acceptEdits` — see dobby/agy.py MODES.
    extra = list(extra)
    argv = ["agy", "--print", prompt]
    if "--mode" not in extra:
        argv += ["--mode", "plan"]
    if model:
        argv += ["--model", model]
    return argv + extra


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
        # The catalog's own argv ends with `--permission-mode plan`, which is
        # read-only. Extras are appended last and therefore override it.
        write_extra=("--permission-mode", "acceptEdits"),
        capabilities=("files", "shell", "web", "vision", "long_context"),
        # `--permission-mode plan` is documented by the vendor as read-only and the
        # default argv pins it. CLAIMED, not VERIFIED: no write probe has been run
        # against it here, and the one provider that WAS probed wrote anyway.
        read_only_default=RO_CLAIMED,
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Deepest tool use and longest context of the CLI set; default "
              "choice for synthesis and adjudication roles.",
    ),
    ProviderSpec(
        id="codex", kind="cli", display="OpenAI Codex CLI", binary="codex",
        argv=_codex, cost_tier="premium",
        # Verified 2026-07-26: `codex exec -s workspace-write` edits inside the
        # working directory and refuses outside it. `danger-full-access` also
        # exists and is deliberately not used - an agent that can write anywhere
        # is a liability, not a measurement.
        write_extra=("-s", "workspace-write"),
        capabilities=("files", "shell", "long_context"),
        # Sandboxed by default per the note below; `-s workspace-write` is the
        # opt-in. CLAIMED for the same reason as claude — documented, not probed.
        read_only_default=RO_CLAIMED,
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Strong at focused code edits and repo-scoped review "
              "(`codex exec review`). Sandboxed by default.",
    ),
    ProviderSpec(
        id="gemini", kind="cli", display="Gemini CLI", binary="gemini",
        argv=_gemini, cost_tier="standard",
        capabilities=("files", "shell", "web", "vision", "long_context"),
        # `--approval-mode plan` is the vendor's read-only mode and the default
        # argv pins it. Documented, not probed here.
        read_only_default=RO_CLAIMED,
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Large context and a real web tool; the default scout for "
              "breadth-first exploration.",
    ),
    ProviderSpec(
        id="agy", kind="cli", display="Antigravity CLI", binary="agy",
        argv=_agy, cost_tier="standard",
        # ROLE_ROUTING has listed agy under "implement" since this table was
        # written while the spec carried no write_extra at all, and `write_extra`
        # documents itself as a refusal when empty — so `swebench` refused to run
        # agy as an implementer at all.
        #
        # This value is EXECUTED, not read off `--help`: see `_agy` above for the
        # four-configuration probe. It is the documented write mode and it is the
        # right thing to send, but the probe also showed agy writes files without
        # it, so unlike codex's `-s workspace-write` this flag is a declaration of
        # intent rather than the switch that unlocks editing. An implementer role
        # filled by agy will edit; a scout role filled by agy CAN ALSO edit, and
        # only cwd/worktree isolation and the prompt stand between it and the tree.
        write_extra=("--mode", "accept-edits"),
        capabilities=("files", "shell", "long_context"),
        # MEASURED to write under the default argv: the four-configuration probe
        # in `_agy` above created a file in all four mode/permission combinations.
        # This is the whole reason this field exists. `write_extra=()` says what
        # this harness did not send; it says nothing about what agy does anyway,
        # and a read-only role that resolved here would be one in name only.
        read_only_default=RO_DENIED,
        timeout_s=900, mutates_worktree=True, verified_on=(WIN,),
        notes="Exposes several model families behind one CLI (`agy models`), "
              "including a reasoning-effort dial (--effort). `dobby agy` is the "
              "delegation lane for it: templates, a cost-benefit gate, and a "
              "print-timeout that outlives the process ceiling. Its `--mode plan` "
              "is NOT a containment control — measured to write files in all four "
              "mode/permission combinations.",
    ),
    ProviderSpec(
        id="qwen", kind="cli", display="Qwen Code CLI", binary="qwen",
        argv=_qwen, cost_tier="cheap",
        capabilities=("files", "shell", "long_context"),
        # Nobody has looked, and `verified_on=()` says the argv itself is
        # unobserved. Unknown is refused for read-only roles rather than assumed.
        read_only_default=RO_UNKNOWN,
        timeout_s=600, mutates_worktree=True, verified_on=(),
        notes="Declared, NOT verified here (binary absent on the authoring "
              "machine). Install: npm i -g @qwen-code/qwen-code.",
    ),
    ProviderSpec(
        id="ollama", kind="cli", display="Ollama (local weights)",
        binary="ollama", argv=_ollama, cost_tier="local",
        capabilities=("long_context",),
        # Structural: no `files` capability, so there is no mechanism to write
        # with. This is a fact about the interface, not a claim about a flag.
        read_only_default=RO_VERIFIED,
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
        # Structural: an api provider that returns text.
        read_only_default=RO_VERIFIED,
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
        # Structural: an api provider that returns text.
        read_only_default=RO_VERIFIED,
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
    # Plans one work item and returns a document, never an edit. Deepest
    # available and only ONE runs, for the same reason `synthesize` is: this is
    # the decision the rest of the portfolio is built on, and a cheap wrong
    # answer here is paid for by every worker that follows it.
    #
    # Invoked WITHOUT `write_extra`. That absence is NOT the read-only profile —
    # it only says what this harness declined to send. `agy` stays listed here
    # because it is a capable planner, and `READ_ONLY_ROLES` below is what
    # actually keeps it out: it is RO_DENIED, measured writing under exactly the
    # argv this role uses.
    "architect": ("claude", "codex", "gemini", "agy"),
    # Final judgment. Deepest available, and only ONE runs.
    "synthesize": ("claude", "codex", "gemini", "agy"),
    "adjudicate": ("claude", "codex", "gemini", "agy"),
}

#: Roles that must never be filled by an api-kind provider even when network is
#: allowed: they see the whole aggregated context, which is the highest-value
#: payload in the system and the worst thing to ship to a third party.
LOCAL_ONLY_ROLES = frozenset({"synthesize", "adjudicate"})

#: Roles whose whole contract is that they return a document and touch nothing.
#: A provider measured to write under the default argv may not fill one, however
#: high it sits in `ROLE_ROUTING` — the preference table states what would be
#: BEST, and this states what is ALLOWED, which is a different question and was
#: previously answered only by the absence of `write_extra`.
#:
#: Membership here is not a guarantee on its own. It removes the provider that is
#: known to write; the callers additionally fingerprint the tree either side of
#: the call, because a routing table is a claim and the tree is the fact.
READ_ONLY_ROLES = frozenset({"architect"})


def role_preference(role: str) -> tuple[str, ...]:
    """Provider ids for `role`, most-preferred first."""
    try:
        return ROLE_ROUTING[role]
    except KeyError:
        raise KeyError(
            f"unknown role {role!r}; known: {sorted(ROLE_ROUTING)}") from None
