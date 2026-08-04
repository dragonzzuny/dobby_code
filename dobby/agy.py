"""Delegating work to Antigravity (`agy`): when it pays, and how to phrase it.

This is the harness-side port of the `claude-code-agy-CLI-skill` project
(github.com/SafeMantella/claude-code-agy-CLI-skill). That repository is five
markdown files — a delegation policy, twelve prompt templates, eight orchestration
patterns and a capability matrix — written for one person's laptop: it hardcodes
`/Users/pedroarfux/.local/bin/agy`, and its flag list predates `--effort`,
`--output-format`, `--json-schema` and `--mode`. Copying those files into this kit
would have shipped a macOS path and a stale flag surface as if they were facts.

What survives the port is the part that is actually knowledge:

1. **A delegation decision** — most tasks should NOT be delegated, and the
   deciding quantity is how many tool calls doing it yourself would cost.
2. **A prompt shape** — the delegate starts with ZERO context, so a prompt that
   omits absolute paths produces a confident answer about the wrong tree.
3. **A capability list** — the things `agy` can do that this process cannot, which
   is the only case where delegation wins regardless of size.

Everything about HOW to launch it comes from `agy --help` on the machine that runs
this file, recorded verbatim in `reports/AGY_FLAG_SURFACE.md`, never from the
upstream README.

## Three failure modes this module exists to prevent

**The timeout that lies.** `agy --print-timeout` takes a Go duration and defaults
to 5m0s. `run_provider` imposes its own process ceiling and, when that ceiling is
the shorter of the two, kills a healthy call and reports "the tool may have fallen
back to interactive mode" — a diagnosis pointing at the wrong subsystem entirely.
`delegate()` therefore derives BOTH ceilings from one number and keeps the process
ceiling strictly above the print ceiling, so a slow answer is reported by the tool
that knows why it was slow.

**The mode that does not mode.** `--mode plan` is documented as the read-only
execution mode and the catalog treated it as one. It is not, on 1.1.8. Measured
four ways — plan and accept-edits, each with and without
`--dangerously-skip-permissions`, each in a fresh temp directory, prompt "create
hello.txt containing DOBBY_WRITE_OK" — the file was created **four times out of
four**. So a delegation cannot be made read-only with a flag here.

Two consequences run through this module. The `Do NOT modify` line that
`build_prompt` writes into every read-only template is not belt-and-braces, it is
the ONLY instruction-level control that exists, so it is unconditional and stated
before anything else. And `cwd` is passed explicitly on every call, because the
working directory a delegate is launched in is a real boundary where its execution
mode is not. Exactly one `--mode` still reaches the process — `agy_extra` never
emits a duplicate and `catalog._agy` drops its default when the caller supplies
one — because an argv that contradicts itself is unreadable evidence, not because
the flag protects anything.

**The prompt that assumes shared context.** Delegated calls do not inherit this
conversation. `build_prompt` refuses to imply context that is not in its own text:
paths are absolute, the output contract is stated, and the read-only constraint is
written into the prompt as well as the flags — because a model that has been told
in prose not to edit is a second, independent layer over a flag nobody re-read.

## The headless permission trap, measured

The upstream skill passes `--dangerously-skip-permissions` in most of its examples
and never says why. It reads like carelessness. It is not — it is load-bearing, and
this is what happens without it (agy 1.1.8, win32, 2026-08-04, prompt = "state in
one sentence what dobby/agy.py is for"):

    rc=0   stdout=0 chars   duration=18.6s
    stderr: no output produced — a tool required the "command" permission that
            headless mode cannot prompt for, so it was auto-denied. ... re-run
            with --dangerously-skip-permissions to auto-approve all tools.

The same prompt with the flag: rc=0, 334 characters, a correct answer about the
file. So in `--print` mode ANY delegation that must touch the tree — which is
every template here except pure reasoning — returns a SUCCESSFUL EXIT AND NOTHING
ELSE until tool permission is granted, either by that flag or by allow-rules in
agy's own `settings.json`.

This is the single most expensive fact about delegating to this tool, because the
signature (exit 0, empty output) looks like a harness bug and not a policy. Both
halves are now handled: `run.py` reports the child's stderr instead of guessing
about output formats, and `delegate()` says up front, before spending anything,
that a permissionless call will come back empty.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from typing import Sequence

from .core.platform import child_env, shim_safe_argv
from .core.security import cap_output, redact_secrets
from .providers.base import ProviderResult
from .providers.catalog import registry
from .providers.run import DEFAULT_OUTPUT_CAP, run_provider

#: Version whose `--help` was read to build this module. Recorded so a future
#: reader can tell a stale flag from a wrong one: if `agy --version` disagrees,
#: re-measure before trusting anything below.
OBSERVED_VERSION = "1.1.8"

#: Flags this module emits, each present in that `--help`. Anything not here is
#: passed through by the caller at their own risk.
VERIFIED_FLAGS = (
    "--print", "--print-timeout", "--mode", "--model", "--effort", "--add-dir",
    "--continue", "--conversation", "--output-format", "--json-schema",
    "--sandbox", "--dangerously-skip-permissions", "--agent",
)

#: `--mode` values, verbatim from `--help`: "(accept-edits, plan)". NOT the
#: `acceptEdits` spelling claude uses — a plausible guess here costs a hang.
MODES = ("plan", "accept-edits")

#: `--effort` values, verbatim: "(low|medium|high)".
EFFORTS = ("low", "medium", "high")

#: `--output-format` values, verbatim: "(text, json, stream-json)". The upstream
#: skill and this kit's own catalog docstring both said agy was text-only; that
#: was true of an older build and is not true of 1.1.8.
OUTPUT_FORMATS = ("text", "json", "stream-json")

#: Seconds added to the print timeout to get the process ceiling. Large enough to
#: cover interpreter start and the tool's own shutdown, small enough that a truly
#: wedged child is still reaped in the same minute.
PROCESS_MARGIN_S = 45

#: Default wall clock for one delegation. The upstream patterns use 2m–5m; 5m is
#: also agy's own default, so an unspecified call behaves the way its docs say.
DEFAULT_TIMEOUT_S = 300

#: Substrings of the stderr agy emits when headless mode auto-denies a tool. Used
#: to turn "exit 0, no output" — which reads as a harness fault — into the one
#: sentence that actually resolves it. Matching on either half survives a reworded
#: message better than pinning the whole string.
PERMISSION_DENIED_MARKERS = ("permission", "auto-denied")

#: What to tell the caller when that happens, or before it does.
PERMISSION_REMEDY = (
    "agy's headless (--print) mode cannot prompt for tool permission, so it "
    "auto-denies and exits 0 with no output. Grant it either way: pass "
    "--skip-permissions (--dangerously-skip-permissions, auto-approves every "
    "tool this delegate calls), or add allow-rules under permissions.allow in "
    "agy's settings.json, which is narrower and survives the next run. Note that "
    "this gates the 'command' class specifically: creating files needs no such "
    "grant and happens regardless of --mode, so neither flag is what keeps a "
    "delegate out of your tree — cwd, worktree isolation and the prompt are."
)


class AgyError(ValueError):
    """A delegation this module refuses to launch.

    Raised for bad flag VALUES rather than for a call that ran and failed: an
    invalid `--mode` does not fail loudly at the CLI, it can drop the tool into
    interactive mode where it waits on a stdin that `run_provider` has closed.
    Refusing before launch converts a silent five-minute hang into one line.
    """


# ---------------------------------------------------------------------------
# Capability triggers: what `agy` can do that this process cannot.
# ---------------------------------------------------------------------------
#
# For these the delegation arithmetic does not apply. A task needing a live web
# search is not "cheaper" to delegate — it is impossible not to.
#
# HONESTY BOUNDARY: which of these are real is a claim about somebody else's
# tool. `google_web_search`, `generate_image`, `codebase_investigator`, the
# Chrome DevTools bridge and the science-database skills are DECLARED by the
# upstream skill's capability matrix and are NOT verified from this repository —
# `capabilities()` reports them under that label and never as measurements.

@dataclasses.dataclass(frozen=True)
class Trigger:
    """A capability that makes delegation the only option, and how to spot it."""

    capability: str
    template: str
    #: Lowercased substrings. Korean is first-class here: this harness is used in
    #: Korean and an English-only trigger table simply never fires.
    words: tuple[str, ...]
    #: "declared" — asserted by the upstream skill, unverified here.
    #: "measured"  — observed from this repository, with the evidence named.
    evidence: str
    note: str = ""


TRIGGERS: tuple[Trigger, ...] = (
    Trigger("web_search", "websearch",
            ("search the web", "google", "latest version", "current version",
             "cve", "release notes", "최신", "검색", "웹 검색", "구글"),
            "declared",
            "google_web_search — grounded, live results. This process has no "
            "web tool at all, so any 'what is current' question is delegate-only."),
    Trigger("image_generation", "image",
            ("generate an image", "mockup", "wireframe", "이미지 생성", "목업",
             "시안"),
            "declared",
            "generate_image — the harness has no image generator of any kind."),
    Trigger("codebase_investigator", "investigate",
            ("architecture of", "how does this codebase", "map the codebase",
             "아키텍처", "구조 파악", "코드베이스 분석"),
            "declared",
            "codebase_investigator does in one pass what grep/read do in fifty."),
    Trigger("browser", "investigate",
            ("chrome devtools", "browser automation", "lighthouse",
             "core web vitals", "브라우저 자동화"),
            "declared",
            "Chrome DevTools Protocol access."),
    Trigger("science_databases", "science",
            ("pubmed", "uniprot", "gnomad", "alphafold", "chembl", "clinvar",
             "ensembl", "pdb structure", "논문 검색"),
            "declared",
            "40+ database skills. Absent here, and hand-rolling one HTTP client "
            "per database is how a two-hour task becomes a two-day one."),
)

#: Tasks the upstream skill names as anti-patterns: delegation costs more than
#: the work. Doing these yourself is not laziness avoidance, it is arithmetic.
TRIVIAL_MARKERS = (
    "typo", "rename this", "add a comment", "one-liner", "one line",
    "오타", "주석 추가", "한 줄", "이름만 바꿔",
)

#: Below this, the round trip (write the prompt, wait, read the answer, verify it)
#: costs more than the calls it saves.
SELF_CEILING_CALLS = 5
#: Above this, delegation wins on volume alone even with no exclusive capability.
DELEGATE_FLOOR_CALLS = 15


# ---------------------------------------------------------------------------
# Templates: the eight upstream patterns, as data.
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Template:
    """One delegation shape: what it is for, and what must come back."""

    id: str
    purpose: str
    output_contract: str
    constraints: tuple[str, ...]
    #: True when the template's whole point is to change files. Everything else
    #: is read-only and says so in the prompt as well as in `--mode`.
    writes: bool = False
    #: True when the delegate must call a permissioned tool — read the tree,
    #: search, run a command — to answer at all. Every template shipped here is
    #: such a case, which is exactly why the default is True and why a delegation
    #: without granted permission comes back empty (see PERMISSION_REMEDY). A
    #: future pure-reasoning template would set it False and mean it.
    needs_tools: bool = True
    timeout_s: int = DEFAULT_TIMEOUT_S
    effort: str | None = None


_READ_ONLY = ("Do NOT modify, create, or delete any file.",)

TEMPLATES: dict[str, Template] = {
    t.id: t for t in (
        Template(
            "research",
            "Map an unfamiliar area of a tree and report what is where.",
            "A structured summary: every claim carries an absolute file path and "
            "a line number. No recommendations, no code.",
            _READ_ONLY + ("If a path cannot be found, say so — do not infer it.",),
            timeout_s=300),
        Template(
            "investigate",
            "Whole-codebase architecture pass (the codebase_investigator case).",
            "An architecture map: entry points, module boundaries, cross-file "
            "dependencies, and the patterns actually in use — each with paths.",
            _READ_ONLY,
            timeout_s=420, effort="high"),
        Template(
            "review",
            "Second-opinion review from a model that did not write the code.",
            "A prioritised finding list. Each finding: severity "
            "(critical/high/medium/low), file:line, the concrete failure it "
            "causes, and the smallest fix. No praise, no summary paragraph.",
            _READ_ONLY + (
                "A finding without a file:line and a failure scenario is not a "
                "finding — drop it rather than padding the list.",),
            timeout_s=240, effort="high"),
        Template(
            "generate",
            "Bulk generation (tests, boilerplate, docs) kept out of this context.",
            "ONLY the file content requested, in one fenced block per file, each "
            "preceded by its absolute target path. No explanation.",
            _READ_ONLY + (
                "Write nothing to disk; the caller reviews before applying.",),
            timeout_s=300),
        Template(
            "refactor",
            "A mechanical multi-file change, applied in place.",
            "The list of files changed and a one-line description per file.",
            ("Preserve every existing comment and docstring.",
             "Do not change public signatures unless the task says to.",
             "Run the project's existing tests afterwards and report the result "
             "verbatim, including failures."),
            writes=True, timeout_s=600),
        Template(
            "websearch",
            "Live web research (grounded search — this process has none).",
            "A markdown table, one row per source, with the URL and the date "
            "the source itself carries.",
            _READ_ONLY + (
                "Report only what a fetched source states. Mark anything you "
                "could not confirm as UNVERIFIED rather than filling it in.",),
            timeout_s=240),
        Template(
            "image",
            "Generate a visual asset.",
            "The absolute path of each file written, and its dimensions.",
            ("Write only into the directory named in the task.",),
            writes=True, timeout_s=300),
        Template(
            "science",
            "Query a scientific database through agy's database skills.",
            "A markdown table with the database's own identifiers (PMID, "
            "accession, DOI) so every row is independently checkable.",
            _READ_ONLY + (
                "Never synthesise a record that the database did not return.",),
            timeout_s=300),
    )
}

DEFAULT_TEMPLATE = "research"


# ---------------------------------------------------------------------------
# The decision.
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Verdict:
    """Whether to delegate `task`, and the reasoning, as data."""

    delegate: bool
    basis: str                     # "capability" | "volume" | "trivial" | "unknown"
    reason: str
    template: str
    triggers: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    estimated_tool_calls: int | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def assess(task: str, *, estimated_tool_calls: int | None = None) -> Verdict:
    """Decide whether `task` is worth delegating, before spending anything.

    Order matters. An exclusive capability outranks the volume arithmetic —
    "search the web" is not cheaper to delegate, it is the only way to do it —
    and both outrank the trivial-marker check, because a one-line change that
    needs a live CVE lookup is still a delegation.
    """
    lowered = " ".join(task.lower().split())
    warnings: list[str] = []

    if len(lowered.split()) < 4:
        warnings.append(
            "the task is too short to delegate as written: the delegate has no "
            "shared context, so a vague prompt returns a confident answer about "
            "something else (upstream anti-pattern 2)")

    fired = [t for t in TRIGGERS if any(w in lowered for w in t.words)]
    if fired:
        return Verdict(
            delegate=True, basis="capability",
            reason="needs a capability this process does not have: "
                   + ", ".join(f"{t.capability} ({t.evidence})" for t in fired),
            template=fired[0].template,
            triggers=[t.capability for t in fired],
            warnings=warnings, estimated_tool_calls=estimated_tool_calls)

    if any(marker in lowered for marker in TRIVIAL_MARKERS):
        return Verdict(
            delegate=False, basis="trivial",
            reason="matches an upstream anti-pattern: the round trip costs more "
                   "than the edit",
            template=DEFAULT_TEMPLATE, warnings=warnings,
            estimated_tool_calls=estimated_tool_calls)

    if estimated_tool_calls is None:
        return Verdict(
            delegate=False, basis="unknown",
            reason=f"no capability trigger fired and no tool-call estimate was "
                   f"given. Estimate it: under {SELF_CEILING_CALLS} calls, do it "
                   f"here; over {DELEGATE_FLOOR_CALLS}, delegate. An unestimated "
                   f"delegation is a guess wearing a decision's clothes.",
            template=DEFAULT_TEMPLATE, warnings=warnings)

    if estimated_tool_calls < SELF_CEILING_CALLS:
        return Verdict(
            delegate=False, basis="volume",
            reason=f"{estimated_tool_calls} tool calls is below the "
                   f"{SELF_CEILING_CALLS}-call floor; doing it here is cheaper "
                   f"than writing a self-contained prompt for it",
            template=DEFAULT_TEMPLATE, warnings=warnings,
            estimated_tool_calls=estimated_tool_calls)

    if estimated_tool_calls > DELEGATE_FLOOR_CALLS:
        return Verdict(
            delegate=True, basis="volume",
            reason=f"{estimated_tool_calls} tool calls exceeds the "
                   f"{DELEGATE_FLOOR_CALLS}-call ceiling; the intermediate steps "
                   f"would fill this context with material nobody reads again",
            template=DEFAULT_TEMPLATE, warnings=warnings,
            estimated_tool_calls=estimated_tool_calls)

    return Verdict(
        delegate=False, basis="volume",
        reason=f"{estimated_tool_calls} calls is in the judgment band "
               f"({SELF_CEILING_CALLS}-{DELEGATE_FLOOR_CALLS}). Delegate only if "
               f"the intermediate output is bulky and disposable; the default is "
               f"to keep it here, where the context is already loaded.",
        template=DEFAULT_TEMPLATE, warnings=warnings,
        estimated_tool_calls=estimated_tool_calls)


# ---------------------------------------------------------------------------
# The prompt.
# ---------------------------------------------------------------------------

def build_prompt(task: str, *, template: str = DEFAULT_TEMPLATE,
                 project_root: str | None = None,
                 files: Sequence[str] = (),
                 stack: str = "",
                 requirements: Sequence[str] = (),
                 output_contract: str | None = None,
                 constraints: Sequence[str] = (),
                 allow_writes: bool = False) -> str:
    """Assemble a delegation prompt that stands on its own.

    Paths are made ABSOLUTE here rather than trusted as given. A relative path in
    a delegated prompt resolves against the delegate's working directory, which is
    not necessarily this one — the resulting answer is about a file that may not
    exist, and nothing in the output says so.

    The read-only constraint is written in prose and is NOT redundant with
    `--mode plan`. That flag was measured not to prevent writes at all on agy
    1.1.8 (four configurations, four files created), so this sentence is the only
    read-only instruction the delegate actually receives. It is added
    unconditionally unless the caller asked for writes.
    """
    tpl = get_template(template)
    if tpl.writes and not allow_writes:
        raise AgyError(
            f"template {template!r} exists to modify files; say so explicitly "
            f"(allow_writes=True, or --write on the command line). Silently "
            f"downgrading it to read-only would produce a report that reads like "
            f"work that was never done.")

    lines = [task.strip(), "", "Context:"]
    root = os.path.abspath(project_root) if project_root else os.getcwd()
    lines.append(f"- Project root: {root}")
    if files:
        lines.append("- Relevant files:")
        lines += [f"  - {os.path.abspath(f)}" for f in files]
    if stack:
        lines.append(f"- Tech stack: {stack}")
    lines.append("- You have NO context from the caller beyond this message. "
                 "Do not assume any file, decision, or convention not stated here.")

    if requirements:
        lines += ["", "Requirements:"]
        lines += [f"{i}. {r}" for i, r in enumerate(requirements, 1)]

    lines += ["", "Output format:", output_contract or tpl.output_contract]

    every = list(tpl.constraints) + list(constraints)
    if not allow_writes and not any("do not modify" in c.lower() for c in every):
        every.insert(0, "Do NOT modify, create, or delete any file.")
    lines += ["", "Constraints:"] + [f"- {c}" for c in every]
    return "\n".join(lines)


def looks_permission_denied(result: ProviderResult) -> bool:
    """Is this the headless auto-deny rather than a genuinely empty answer?

    The signature is exit 0 with no captured text — a SUCCESSFUL process that
    produced nothing — plus the tool's own word for it on stderr, which
    `run_provider` now carries into `error`. Requiring both halves keeps a
    prompt that legitimately returned nothing from being explained away as a
    permission problem.
    """
    if result.ok or result.exit_code != 0 or result.text.strip():
        return False
    haystack = (result.error or "").lower()
    return any(marker in haystack for marker in PERMISSION_DENIED_MARKERS)


def get_template(name: str) -> Template:
    try:
        return TEMPLATES[name]
    except KeyError:
        raise AgyError(
            f"unknown template {name!r}; known: {sorted(TEMPLATES)}") from None


# ---------------------------------------------------------------------------
# The launch.
# ---------------------------------------------------------------------------

def go_duration(seconds: int) -> str:
    """Seconds as the Go duration string `--print-timeout` parses.

    `--print-timeout 300` is not a Go duration and the flag rejects it; the
    default shown in `--help` is `5m0s`. Emitting `{m}m{s}s` always — including
    `0m45s` — keeps one code path instead of a special case that gets tested less.
    """
    if seconds <= 0:
        raise AgyError(f"print timeout must be positive, got {seconds}")
    return f"{seconds // 60}m{seconds % 60}s"


def agy_extra(*, timeout_s: int = DEFAULT_TIMEOUT_S,
              allow_writes: bool = False,
              effort: str | None = None,
              add_dirs: Sequence[str] = (),
              continue_conversation: bool = False,
              conversation: str | None = None,
              output_format: str = "text",
              json_schema: str | None = None,
              sandbox: bool = False,
              skip_permissions: bool = False,
              agent: str | None = None) -> list[str]:
    """Build the `extra` argv for `run_provider`, validating every value.

    Nothing here is a guess. Each flag appears in `agy --help` on the machine that
    recorded `reports/AGY_FLAG_SURFACE.md`, and each enumerated value is quoted
    from that text — because a rejected flag does not raise, it can leave the tool
    waiting on a closed stdin until the ceiling fires.

    `--mode` is emitted at most once, and only when writes are requested: the
    catalog's own builder drops its `--mode plan` default the moment the caller
    supplies one, so a duplicate can never reach the process and the read-only
    default can never be reversed by accident.
    """
    if effort is not None and effort not in EFFORTS:
        raise AgyError(f"--effort must be one of {EFFORTS}, got {effort!r}")
    if output_format not in OUTPUT_FORMATS:
        raise AgyError(
            f"--output-format must be one of {OUTPUT_FORMATS}, got "
            f"{output_format!r}")
    if json_schema and output_format == "text":
        raise AgyError(
            "--json-schema shapes structured output; it does nothing under "
            "--output-format text. Pass output_format='json'.")
    if continue_conversation and conversation:
        raise AgyError(
            "--continue and --conversation are two different resumptions; "
            "passing both leaves which one wins up to the CLI's flag order")

    extra: list[str] = ["--print-timeout", go_duration(timeout_s)]
    if allow_writes:
        extra += ["--mode", "accept-edits"]
    if effort:
        extra += ["--effort", effort]
    for directory in add_dirs:
        # A workspace directory that does not exist is accepted by the flag and
        # then quietly contributes nothing, so the answer is about a smaller tree
        # than the caller asked for and reads exactly like a complete one.
        resolved = os.path.abspath(directory)
        if not os.path.isdir(resolved):
            raise AgyError(f"--add-dir {resolved} is not a directory")
        extra += ["--add-dir", resolved]
    if continue_conversation:
        extra.append("--continue")
    if conversation:
        extra += ["--conversation", conversation]
    if output_format != "text":
        extra += ["--output-format", output_format]
    if json_schema:
        extra += ["--json-schema", json_schema]
    if sandbox:
        extra.append("--sandbox")
    if skip_permissions:
        extra.append("--dangerously-skip-permissions")
    if agent:
        extra += ["--agent", agent]
    return extra


def delegate(task: str, *, template: str = DEFAULT_TEMPLATE,
             project_root: str | None = None,
             files: Sequence[str] = (),
             stack: str = "",
             requirements: Sequence[str] = (),
             output_contract: str | None = None,
             constraints: Sequence[str] = (),
             model: str | None = None,
             effort: str | None = None,
             add_dirs: Sequence[str] = (),
             timeout_s: int | None = None,
             allow_writes: bool = False,
             skip_permissions: bool = False,
             continue_conversation: bool = False,
             conversation: str | None = None,
             output_format: str = "text",
             json_schema: str | None = None,
             sandbox: bool = False,
             agent: str | None = None,
             cwd: str | None = None,
             output_cap: int = DEFAULT_OUTPUT_CAP,
             dry_run: bool = False) -> dict:
    """Run one delegation and return the result plus everything needed to audit it.

    The returned dict always carries the exact prompt and the argv shape, whether
    or not the call ran. A delegation whose prompt is not recoverable cannot be
    reproduced, and an unreproducible provider answer is a rumour.

    `dry_run=True` builds and validates everything and spends nothing — the only
    honest way to review a delegation before paying for it.
    """
    tpl = get_template(template)
    seconds = timeout_s or tpl.timeout_s
    prompt = build_prompt(
        task, template=template, project_root=project_root, files=files,
        stack=stack, requirements=requirements, output_contract=output_contract,
        constraints=constraints, allow_writes=allow_writes)
    extra = agy_extra(
        timeout_s=seconds, allow_writes=allow_writes,
        effort=effort or tpl.effort, add_dirs=add_dirs,
        continue_conversation=continue_conversation, conversation=conversation,
        output_format=output_format, json_schema=json_schema, sandbox=sandbox,
        skip_permissions=skip_permissions, agent=agent)

    spec = registry().get("agy")
    argv = spec.build_argv(prompt, model, extra)
    envelope = {
        "provider": "agy",
        "template": template,
        "writes_requested": bool(allow_writes),
        "prompt": prompt,
        "prompt_chars": len(prompt),
        # The prompt is one argv element and can be enormous; showing it in the
        # argv preview twice helps nobody.
        "argv_preview": [a if a is not prompt else "<PROMPT>" for a in argv],
        "print_timeout": go_duration(seconds),
        "process_timeout_s": seconds + PROCESS_MARGIN_S,
        "observed_version": OBSERVED_VERSION,
    }
    if skip_permissions:
        envelope["warning"] = (
            "--dangerously-skip-permissions auto-approves every tool call this "
            "delegate makes, including shell. The upstream skill uses it in most "
            "of its examples; that is a choice about somebody else's machine, not "
            "a default.")
    elif tpl.needs_tools:
        # Said BEFORE the call, not after it: the failure costs a full timeout's
        # worth of waiting and then looks like a harness fault.
        envelope["permission_note"] = PERMISSION_REMEDY
    if dry_run:
        envelope["dry_run"] = True
        return envelope

    result = run_provider(
        spec, prompt, model=model, extra=extra, cwd=cwd,
        # Strictly above the print ceiling: agy's own timeout must fire first, or
        # the harness reports an interactive-fallback hang for a call that was
        # merely slow, and the next hour is spent on the wrong subsystem.
        timeout_s=seconds + PROCESS_MARGIN_S,
        output_cap=output_cap)
    envelope["result"] = result.to_dict()
    envelope["ok"] = result.ok
    if not result.ok and looks_permission_denied(result):
        envelope["diagnosis"] = "headless tool-permission auto-deny"
        envelope["remedy"] = PERMISSION_REMEDY
    if result.ok and result.truncated:
        envelope["truncation_warning"] = (
            f"output hit the {output_cap}-character cap; what follows is a "
            f"PREFIX, not the whole answer — do not summarise it as complete")
    return envelope


# ---------------------------------------------------------------------------
# Local introspection: what this install actually offers.
# ---------------------------------------------------------------------------

def _run_subcommand(name: str, timeout_s: int = 60) -> ProviderResult:
    """Run a local `agy` subcommand (`models`, `agents`) with the launch guards.

    Same three guards as `run_provider` and for the same reasons: the RESOLVED
    path (a bare name cannot start a PATHEXT shim on Windows), stdin closed (so a
    tool that wants a human fails instead of waiting), and redaction before the
    text is stored anywhere.
    """
    spec = registry().get("agy")
    resolved = spec.which()
    if resolved is None:
        return ProviderResult(provider="agy", ok=False,
                              error="binary 'agy' not on PATH")
    argv, note = shim_safe_argv(resolved, [name])
    if argv is None:
        return ProviderResult(provider="agy", ok=False, error=note)
    try:
        proc = subprocess.run(
            argv, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=child_env(), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return ProviderResult(provider="agy", ok=False,
                              error=f"`agy {name}` timed out after {timeout_s}s")
    except OSError as exc:
        return ProviderResult(provider="agy", ok=False,
                              error=f"cannot launch `agy {name}`: {exc}")
    text = cap_output(redact_secrets(proc.stdout or ""), 8_000)
    if proc.returncode != 0:
        return ProviderResult(
            provider="agy", ok=False, text=text, exit_code=proc.returncode,
            error=f"exit {proc.returncode}: "
                  f"{cap_output(redact_secrets(proc.stderr or ''), 400).strip()}")
    return ProviderResult(provider="agy", ok=True, text=text, exit_code=0,
                          meta={"resolved_binary": resolved, "launch_note": note})


def models(timeout_s: int = 60) -> dict:
    """The model list this install offers, measured — never a remembered list.

    Model names move faster than any document about them. `--model` takes one of
    these verbatim, and a stale name is a failed call at the far end of a
    five-minute timeout.
    """
    result = _run_subcommand("models", timeout_s)
    names = [line.strip() for line in result.text.splitlines() if line.strip()]
    return {"ok": result.ok, "error": result.error, "models": names,
            "count": len(names)}


def agents(timeout_s: int = 60) -> dict:
    """Named agents configured in this install (`agy agents`)."""
    result = _run_subcommand("agents", timeout_s)
    lines = [line.strip() for line in result.text.splitlines() if line.strip()]
    # The header line is prose, not an agent; an empty list under it means none
    # are configured, which is a different fact from the command failing.
    names = [line for line in lines if not line.lower().startswith("available")]
    return {"ok": result.ok, "error": result.error, "agents": names,
            "count": len(names)}


def capabilities() -> dict:
    """What delegation buys, split by whether anyone here actually measured it.

    The upstream capability matrix is a good map and an untested one. Merging its
    claims with this repository's own observations under a single "capabilities"
    heading would launder somebody's README into a measurement — the exact move
    `.claude/rules/evidence-and-numbers.md` exists to forbid.
    """
    spec = registry().get("agy")
    return {
        "measured_here": {
            "binary_on_path": spec.which(),
            "version_when_this_module_was_written": OBSERVED_VERSION,
            "flag_surface_evidence": "reports/AGY_FLAG_SURFACE.md",
            "non_interactive": "--print (verified: dobby fleet --probe, 2026-07-26)",
            "structured_output": list(OUTPUT_FORMATS),
            "modes": list(MODES),
            "efforts": list(EFFORTS),
        },
        "declared_upstream_not_verified_here": [
            {"capability": t.capability, "template": t.template, "note": t.note}
            for t in TRIGGERS
        ],
        "source": "github.com/SafeMantella/claude-code-agy-CLI-skill "
                  "(capability matrix), ported 2026-08-04",
        "caveat": "everything under declared_* is a claim about another tool "
                  "read from its skill documentation. Verify with one real call "
                  "before reporting any of it as a property of this machine.",
    }


def templates() -> list[dict]:
    """The delegation shapes, for `dobby agy templates`."""
    return [{"id": t.id, "purpose": t.purpose, "writes": t.writes,
             "timeout_s": t.timeout_s, "effort": t.effort,
             "output_contract": t.output_contract,
             "constraints": list(t.constraints)}
            for t in TEMPLATES.values()]


def triggers() -> list[dict]:
    """The capability trigger table, including the words that fire it."""
    return [{"capability": t.capability, "template": t.template,
             "evidence": t.evidence, "words": list(t.words), "note": t.note}
            for t in TRIGGERS]
