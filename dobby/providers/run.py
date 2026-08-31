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

import contextlib

import signal
import os
import subprocess
import time
from typing import Sequence

from ..core.platform import child_env, is_windows, shim_safe_argv
from ..core.security import cap_output, redact_secrets
from .base import ProviderResult, ProviderSpec
from .catalog import registry

#: Default ceiling on captured provider output, in characters. Chosen to be
#: large enough for a full code review and small enough that six of them still
#: fit in a synthesis prompt alongside the task.
DEFAULT_OUTPUT_CAP = 24_000


# -- call recording ----------------------------------------------------------
#: When a recorder is active, every completed `run_provider` call is appended to
#: it and usage collection defaults ON. This exists so "how many provider calls
#: did that cost" is a MEASUREMENT taken at the one place calls actually happen,
#: rather than a number an orchestrator believes about itself. An orchestrator
#: that counted its own intentions would miss a retry inside a worker, and the
#: retry is exactly what a benchmark is trying to see.
_RECORDER: list | None = None

#: Where every provider call is also appended as a spend-ledger row, or None to
#: keep the ledger untouched. Set by `spend_ledger()`, never by a library call:
#: a module that writes to disk because it was imported is a module nobody can
#: use in a test.
_SPEND_DIR: str | None = None
#: What the current spend rows are attributed to — the skill or command running.
_SPEND_SKILL: str = ""


@contextlib.contextmanager
def spend_ledger(data_dir: str | None, *, skill: str = ""):
    """Append every provider call inside the block to `data_dir`'s spend ledger.

    The ledger existed and only `dobby panel` wrote to it, so `dobby spend`
    reported "no agent calls recorded" after a run that had made five. This is
    the seam that fills it: opt-in, scoped to a block, and holding the skill name
    so a status line can say what the spend was FOR and not only how much.
    """
    global _SPEND_DIR, _SPEND_SKILL
    previous, previous_skill = _SPEND_DIR, _SPEND_SKILL
    _SPEND_DIR, _SPEND_SKILL = data_dir, skill
    try:
        yield
    finally:
        _SPEND_DIR, _SPEND_SKILL = previous, previous_skill


def _recorded(result: ProviderResult) -> ProviderResult:
    """Append to the active recorder, whatever the outcome.

    Failures were missing. Measured 2026-08-23: an agy call refused a tool
    permission, returned exit 1, and the smoke reported `calls_total: 0` — a
    provider that had launched, spent time and produced nothing was invisible to
    the counter and therefore to the quota. A cap that counts only successes
    undercounts precisely the provider that is going wrong.
    """
    if _RECORDER is not None:
        _RECORDER.append(result)
    if _SPEND_DIR:
        try:
            from ..spend import record

            record(_SPEND_DIR, provider=result.provider,
                   duration_s=result.duration_s or 0.0, ok=bool(result.ok),
                   model=str((result.usage or {}).get("model")
                             or (result.meta or {}).get("model") or ""),
                   skill=_SPEND_SKILL, usage=result.usage)
        except Exception:                          # noqa: BLE001
            # The ledger is bookkeeping. A failure to write it must never take
            # down the call it was recording — the answer is already in hand and
            # losing it to an accounting error would be the worse trade.
            pass
    return result


@contextlib.contextmanager
def recording(sink: list | None = None, *, collect_usage: bool = True):
    """Record every provider call made inside the block. Yields the sink list.

    Nested use is refused rather than silently nesting: two active recorders
    would each hold a partial count and neither would be wrong in a way anybody
    could detect.
    """
    global _RECORDER, _COLLECT_DEFAULT
    if _RECORDER is not None:
        raise RuntimeError(
            "a provider-call recorder is already active; nesting them produces "
            "two partial counts and no way to tell which is which")
    sink = [] if sink is None else sink
    _RECORDER, previous = sink, _COLLECT_DEFAULT
    _COLLECT_DEFAULT = collect_usage
    try:
        yield sink
    finally:
        _RECORDER, _COLLECT_DEFAULT = None, previous


#: Default for `run_provider(collect_usage=...)`. False outside a recorder, so
#: nothing in normal operation changes what a CLI prints.
_COLLECT_DEFAULT = False


#: Windows CreateProcess limit for the entire command line. Not a dobby choice
#: and not tunable; the guard in `run_provider` exists to name it accurately.
WINDOWS_COMMAND_LINE_MAX = 32_767


def kill_tree(proc) -> str:
    """Kill `proc` AND everything it started. Returns what was done, for a log.

    `subprocess.run(timeout=...)` kills the direct child and nothing below it.
    Measured on this machine: a grandchild launched with DETACHED_PROCESS and no
    inherited pipes was still writing files five seconds after the provider had
    timed out and the node had been recorded as failed.

    That is not a tidiness problem. An agent CLI starts language servers, git,
    docker, node; an orphan of one keeps a lock, keeps spending, and can still
    be WRITING INTO THE REPOSITORY after this runtime has told itself the
    attempt is over -- which is the effect accounting saying one thing while
    the disk does another.

    Windows has no process groups worth the name, so `taskkill /T` walks the
    tree by pid. POSIX gets a session of its own at launch (`start_new_session`)
    so the whole group can be signalled at once. Both fall back to killing the
    one process rather than raising: a partial kill beats an exception on the
    timeout path.
    """
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
            return "taskkill /T /F"
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        # ONLY when the child leads its own group. `_run_killing_the_tree`
        # launches with `start_new_session=True` precisely so it does, and a
        # child that does NOT -- one launched by some other caller, or one that
        # already exited so the pid was reused -- has the CALLER's group id, and
        # `killpg` on that kills this process and everything beside it.
        #
        # Measured, and this is not a hypothetical: with the guard absent, the
        # ubuntu CI job died with "The hosted runner lost communication with the
        # server". The Windows jobs passed. That is what a POSIX branch written
        # on Windows and never executed there looks like.
        try:
            group = os.getpgid(proc.pid)
        except OSError:
            group = None
        if group is not None and group == proc.pid:
            try:
                os.killpg(group, signal.SIGKILL)
                return "killpg SIGKILL"
            except OSError:
                pass
    try:
        proc.kill()
        return "kill (direct child only)"
    except OSError:
        return "could not kill"


def _run_killing_the_tree(argv, *, timeout=None, capture_output=False,
                          **kwargs):
    """`subprocess.run`, except a timeout takes the whole tree with it.

    Kept as a thin wrapper so the ordinary path is the ordinary path: same
    arguments, same `CompletedProcess`, same `TimeoutExpired`. The only
    difference is what has stopped running by the time the exception is raised.
    """
    if capture_output:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    proc = subprocess.Popen(argv, shell=False, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        # Drain what the tree already wrote, so the caller is not left with a
        # pipe nobody read. A second timeout here means something is still
        # holding the handle, and waiting forever for it is the hang this
        # function exists to end.
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def run_provider(spec: ProviderSpec, prompt: str, *,
                 model: str | None = None,
                 extra: Sequence[str] = (),
                 cwd: str | None = None,
                 timeout_s: int | None = None,
                 output_cap: int = DEFAULT_OUTPUT_CAP,
                 env_extra: dict | None = None,
                 collect_usage: bool | None = None) -> ProviderResult:
    """Run `spec` once on `prompt` and return its outcome as data.

    `collect_usage` appends the spec's `usage_extra` and unwraps the structured
    envelope the CLI then writes, so `text` still holds the ANSWER and `usage`
    holds what the provider said it consumed. Off by default and deliberately:
    the flag changes what the CLI prints, and every existing caller reads `text`
    as the answer, so switching it on globally would turn all of them into parser
    bugs at once. A provider with no `usage_extra` is unchanged by asking.
    """
    collect_usage = (_COLLECT_DEFAULT if collect_usage is None
                     else collect_usage)
    if spec.kind != "cli":
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            error=f"{spec.id} is an api provider; run_provider drives cli "
                  f"providers only (see api_client for the api path)"))
    resolved = spec.which()
    if resolved is None:
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            error=f"binary {spec.binary!r} not on PATH"))

    extra = tuple(extra)
    if collect_usage and spec.usage_extra:
        # Appended last, like every other extra, so it wins over a default the
        # argv builder set. A caller that passed its own output-format flag
        # therefore still overrides this, and the parse below degrades to
        # "unmeasured" rather than breaking.
        extra = extra + tuple(spec.usage_extra)
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
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            error=f"cannot deliver this prompt intact: {launch_note}"))
    # Windows caps the WHOLE command line at 32,767 characters - executable,
    # every argument, and the quoting the loader adds around them. Past it
    # CreateProcess fails as WinError 206, which CPython surfaces as
    # FileNotFoundError, so the handler below blamed "a shim the process loader
    # cannot start" for a prompt that was merely too long. Measured: a 33,730
    # character review prompt to codex failed at wall_s 0.0 while a short probe
    # on the same binary succeeded seconds earlier.
    #
    # Diagnosed here rather than left to the exception, because the wrong
    # diagnosis sends an operator to reinstall a working CLI.
    if is_windows():
        spelled = sum(len(part) + 3 for part in argv)
        if spelled >= WINDOWS_COMMAND_LINE_MAX:
            return _recorded(ProviderResult(
                provider=spec.id, ok=False,
                error=(f"prompt too long for this platform: the command line "
                       f"would be about {spelled:,} characters and Windows "
                       f"caps it at {WINDOWS_COMMAND_LINE_MAX:,}. The prompt "
                       f"itself is {len(prompt):,}. This is not a missing "
                       f"install - shorten the prompt, or hand the material "
                       f"over as a FILE in the provider's working directory "
                       f"and pass a short prompt that points at it"),
                meta={"argv_len": len(argv),
                      "command_line_chars": spelled,
                      "prompt_chars": len(prompt),
                      "platform_limit": WINDOWS_COMMAND_LINE_MAX}))

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
        proc = _run_killing_the_tree(
            argv,
            stdin=subprocess.DEVNULL,       # fail fast instead of waiting on a human
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            env=child_env(env_extra),
            cwd=cwd,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=f"timeout after {limit}s (the tool may have fallen back to "
                  f"interactive mode — check its non-interactive flag)",
            meta=meta))
    except FileNotFoundError:
        # State the contradiction rather than just the symptom: PATH lookup
        # succeeded and launching the very path it returned did not. The old
        # message said only "cannot execute 'codex'", which read like a missing
        # install and hid a resolvable path for as long as nobody probed.
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=(f"{spec.binary!r} resolved to {resolved!r} but could not be "
                   f"launched — the file exists and the OS refused it, so this "
                   f"is a shim or extension the process loader cannot start "
                   f"directly, not a missing install"),
            meta=meta))
    except OSError as exc:
        return _recorded(ProviderResult(
            provider=spec.id, ok=False,
            duration_s=round(time.monotonic() - started, 2),
            error=f"OS error launching {spec.binary!r}: {exc}", meta=meta))

    duration = round(time.monotonic() - started, 2)
    raw = proc.stdout or ""
    stderr = proc.stderr or ""
    safe = redact_secrets(raw)
    capped = cap_output(safe, output_cap)
    truncated = len(safe) > len(capped)

    if proc.returncode != 0:
        # stderr is the diagnostic; cap it too, since a crashing tool can emit
        # megabytes of trace.
        return _recorded(ProviderResult(
            provider=spec.id, ok=False, text=capped,
            exit_code=proc.returncode, duration_s=duration,
            truncated=truncated,
            error=f"exit {proc.returncode}: "
                  f"{cap_output(redact_secrets(stderr), 800).strip()}",
            meta=meta))

    if not capped.strip():
        # Exit 0 with empty stdout is a real and confusing case: it usually
        # means the tool printed its answer to a TTY-only surface or refused the
        # prompt silently. Reporting it as a failure is correct — an empty answer
        # must not be synthesized as if it were a considered "no comment".
        #
        # STDERR IS THE DIAGNOSIS AND WAS BEING THROWN AWAY. Measured 2026-08-04,
        # agy 1.1.8, a read-only research prompt naming one file:
        #
        #   rc=0  stdout=0 chars  stderr=301 chars
        #   "no output produced — a tool required the "command" permission that
        #    headless mode cannot prompt for, so it was auto-denied. ...
        #    re-run with --dangerously-skip-permissions to auto-approve all tools."
        #
        # The tool said exactly what was wrong and this branch replaced it with a
        # guess about output formats. Every delegated read of a file failed this
        # way and the harness blamed the wrong subsystem each time.
        detail = cap_output(redact_secrets(stderr), 600).strip()
        return _recorded(ProviderResult(
            provider=spec.id, ok=False, text="", exit_code=0,
            duration_s=duration,
            error=("exit 0 but no stdout. " + (f"stderr: {detail}" if detail else
                   "stderr was empty too — the tool may print only to a TTY, or "
                   "it silently refused the prompt; try its explicit "
                   "output-format flag")),
            meta=meta))

    usage = None
    if collect_usage and spec.usage_extra:
        from .usage import unwrap
        # UNWRAP THE FULL OUTPUT, THEN CAP THE ANSWER — not the other way round.
        #
        # This used to parse `capped`, and the comment here said a truncated
        # envelope yielding no usage was "the honest outcome". It was honest and
        # it was avoidable: the whole stream is in `safe` and only the ANSWER
        # needs bounding. Codex is the case that proves it — it streams JSONL and
        # puts `turn.completed`, which carries every token count, at the END, so
        # any task long enough to exceed the 24,000-char cap reported
        # `calls_measured: 0`. Measured 2026-08-24 on a django SWE-bench
        # instance: a real call, real tokens spent, and a row that could only say
        # it did not know. The cap exists to keep an answer out of somebody's
        # context window; it was never a reason to stop counting.
        answer, parsed, signals = unwrap(spec.id, safe)
        if parsed is not None:
            usage = parsed.to_dict()
            capped = cap_output(answer, output_cap)
            truncated = len(answer) > len(capped)
            # Evidence, not usage: a refused tool is why a call can succeed and
            # accomplish nothing, and the worker above has to be able to tell
            # that from a model that simply declined.
            meta.update({k: v for k, v in signals.items() if v not in (None, [])})

    return _recorded(ProviderResult(
        provider=spec.id, ok=True, text=capped, exit_code=0,
        duration_s=duration, truncated=truncated, meta=meta, usage=usage))


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
