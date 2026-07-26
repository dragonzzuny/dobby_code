"""Execute one provider call as a child process, safely and with a hard budget.

Every guarantee here exists because of a specific failure mode observed with
agent CLIs:

- **A timeout is mandatory, never optional.** All of these tools drop into an
  interactive REPL when their non-interactive flag is wrong or a login has
  expired. With stdin closed they then block forever on a read. A fan-out of six
  such calls hangs the whole orchestration with no output and no error. Every
  call therefore gets a wall clock, and a timeout is returned as an ordinary
  failed `ProviderResult`.

- **stdin is closed explicitly (`DEVNULL`).** Inheriting the parent's stdin lets
  a child consume the orchestrator's own input stream, which corrupts JSON-RPC
  when the harness runs as an MCP server. Closing it also makes the interactive-
  fallback case fail fast instead of waiting on a human.

- **Output is capped and secrets are redacted before anything is stored.**
  Provider stdout is untrusted text that lands in ledgers, trajectories, and
  later prompts. Uncapped, a runaway tool fills the context window; unredacted, a
  key echoed by a child gets committed to a report.

- **UTF-8 is pinned on both sides.** See dobby/core/platform.py: the parent
  decodes with the locale codec by default, which raises UnicodeDecodeError on a
  child's non-ASCII output on any non-UTF-8 Windows install.

The prompt is passed as an ARGV ELEMENT, never through a shell. `shell=False`
means no quoting rules, no interpolation, and no way for prompt text — which may
be attacker-influenced, e.g. an issue body — to become shell syntax.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Sequence

from ..core.platform import child_env, shim_safe_argv
from ..core.security import cap_output, redact_secrets
from .base import ProviderResult, ProviderSpec
from .catalog import registry

#: Default ceiling on captured provider output, in characters. Chosen to be
#: large enough for a full code review and small enough that six of them still
#: fit in a synthesis prompt alongside the task.
DEFAULT_OUTPUT_CAP = 24_000


def run_provider(spec: ProviderSpec, prompt: str, *,
                 model: str | None = None,
                 extra: Sequence[str] = (),
                 cwd: str | None = None,
                 timeout_s: int | None = None,
                 output_cap: int = DEFAULT_OUTPUT_CAP,
                 env_extra: dict | None = None) -> ProviderResult:
    """Run `spec` once on `prompt` and return its outcome as data."""
    if spec.kind != "cli":
        return ProviderResult(
            provider=spec.id, ok=False,
            error=f"{spec.id} is an api provider; run_provider drives cli "
                  f"providers only (see api_client for the api path)")
    resolved = spec.which()
    if resolved is None:
        return ProviderResult(
            provider=spec.id, ok=False,
            error=f"binary {spec.binary!r} not on PATH")

    argv = spec.build_argv(prompt, model, extra)

    # Launch the RESOLVED path, not the bare name. `shutil.which` and
    # CreateProcess disagree about what "on PATH" means on Windows, and the
    # disagreement silently broke most of the fleet:
    #
    #   which("codex")                     -> C:\...\npm\codex.CMD   (found)
    #   subprocess.run(["codex", ...])      -> WinError 2, not found
    #   subprocess.run([r"C:\...codex.CMD"]) -> rc 0, works
    #
    # `which` consults PATHEXT and so finds .CMD; CreateProcess appends only
    # .exe. npm installs its CLIs as .CMD shims on Windows, which is how codex
    # and gemini came to be reported `usable: true` while being unlaunchable —
    # measured by the first real `dobby fleet --probe` run: claude and agy
    # answered in 35s and 62s, codex and gemini failed in 0.14s without ever
    # starting a process.
    #
    # Detection already paid for this lookup and its answer was being discarded
    # one line later. Substituting it costs nothing and keeps shell=False, so
    # prompt text still cannot become shell syntax.
    argv, launch_note = shim_safe_argv(resolved, argv[1:])
    if argv is None:
        # Refusing beats delivering a truncated prompt. A provider that receives
        # only the first line answers that line and the reply looks like an
        # opinion about the whole task.
        return ProviderResult(
            provider=spec.id, ok=False,
            error=f"cannot deliver this prompt intact: {launch_note}")
    limit = timeout_s or spec.timeout_s
    started = time.monotonic()
    meta = {
        # The prompt is deliberately NOT stored here: it can be long and can
        # contain the very untrusted content the ledger should not echo. Its
        # length and a hash-free preview length are enough for provenance.
        "argv_head": argv[:2],
        "argv_len": len(argv),
        # Recorded because the gap between the name and the path was the bug.
        "resolved_binary": resolved,
        # Non-empty when the launch route had to change to keep the
        # prompt intact. Silent re-routing would be its own defect.
        "launch_note": launch_note,
        "prompt_chars": len(prompt),
        "model": model,
        "cwd": cwd or os.getcwd(),
        "timeout_s": limit,
    }
    try:
        proc = subprocess.run(
            argv,
            shell=False,                    # prompt text can never be shell syntax
            stdin=subprocess.DEVNULL,       # fail fast instead of waiting on a human
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=child_env(env_extra),
            cwd=cwd,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=f"timeout after {limit}s (the tool may have fallen back to "
                  f"interactive mode — check its non-interactive flag)",
            meta=meta)
    except FileNotFoundError:
        # State the contradiction rather than just the symptom: PATH lookup
        # succeeded and launching the very path it returned did not. The old
        # message said only "cannot execute 'codex'", which read like a missing
        # install and hid a resolvable path for as long as nobody probed.
        return ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=(f"{spec.binary!r} resolved to {resolved!r} but could not be "
                   f"launched — the file exists and the OS refused it, so this "
                   f"is a shim or extension the process loader cannot start "
                   f"directly, not a missing install"),
            meta=meta)
    except OSError as exc:
        return ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=f"OS error launching {spec.binary!r}: {exc}", meta=meta)

    duration = round(time.monotonic() - started, 2)
    raw = proc.stdout or ""
    stderr = proc.stderr or ""
    safe = redact_secrets(raw)
    capped = cap_output(safe, output_cap)
    truncated = len(safe) > len(capped)

    if proc.returncode != 0:
        # stderr is the diagnostic; cap it too, since a crashing tool can emit
        # megabytes of trace.
        return ProviderResult(
            provider=spec.id, ok=False, text=capped,
            exit_code=proc.returncode, duration_s=duration,
            truncated=truncated,
            error=f"exit {proc.returncode}: "
                  f"{cap_output(redact_secrets(stderr), 800).strip()}",
            meta=meta)

    if not capped.strip():
        # Exit 0 with empty stdout is a real and confusing case: it usually
        # means the tool printed its answer to a TTY-only surface or refused the
        # prompt silently. Reporting it as a failure is correct — an empty answer
        # must not be synthesized as if it were a considered "no comment".
        return ProviderResult(
            provider=spec.id, ok=False, text="", exit_code=0,
            duration_s=duration,
            error="exit 0 but no stdout (tool produced no machine-readable "
                  "answer; try its explicit output-format flag)",
            meta=meta)

    return ProviderResult(provider=spec.id, ok=True, text=capped,
                          exit_code=0, duration_s=duration,
                          truncated=truncated, meta=meta)


def run_by_id(pid: str, prompt: str, **kwargs) -> ProviderResult:
    """Convenience wrapper: resolve `pid` in the catalog, then run it."""
    return run_provider(registry().get(pid), prompt, **kwargs)


def probe(pid: str, cwd: str | None = None,
          timeout_s: int = 120) -> ProviderResult:
    """Verify a provider ACTUALLY works, with the cheapest possible real call.

    This is the only function in the package that spends money, so it is never
    called implicitly by routing or detection — the user invokes it via
    `dobby fleet probe`. The prompt asks for a fixed token so the check tests the
    whole path (launch, auth, non-interactive flag, output capture) rather than
    just process startup, and a wrong answer still proves the path works.
    """
    result = run_by_id(
        pid,
        "Reply with exactly this word and nothing else: DOBBY_OK",
        cwd=cwd, timeout_s=timeout_s, output_cap=2_000)
    if result.ok:
        result.meta["probe_expected"] = "DOBBY_OK"
        result.meta["probe_matched"] = "DOBBY_OK" in result.text
    return result
