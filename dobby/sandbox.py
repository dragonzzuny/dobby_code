"""Sandboxed execution: keep raw output OUT of the context window entirely.

The difference from compression
-------------------------------
`dobby/tokens.py` makes a large output smaller. This module stops it from
entering the context at all. That is a bigger lever and a different mechanism:
the published system that reports the largest reductions of this kind gets them
by running the tool in an isolated subprocess whose full output stays on disk,
returning only a handle plus whatever the caller explicitly extracts.

Concretely, a command that emits 320 KB returns a `Result` carrying its exit
code, a few hundred bytes of shape information, and a handle. The agent then
asks for exactly the lines it needs — a grep, a slice, a head. The 320 KB never
becomes tokens. Compressing that same output to 10% still costs 32 KB of
context; extracting three matching lines costs 200 bytes.

This inverts the usual default, and the inversion is the point: **output is
withheld unless asked for**, rather than delivered and then regretted.

Isolation is real, and its limits are stated
--------------------------------------------
Four controls are applied, and each is honest about what it does not cover:

- **Working directory** is confined to a declared root **when one is declared**.
  Pass `root=` and the working directory is resolved (through symlinks) and
  refused if it lands outside; `../../etc` then fails before the process starts.
  **With no `root`, there is no confinement** — the command runs wherever `cwd`
  points. This is stated plainly because an earlier version of this docstring
  claimed unconditional confinement while `run()` never called the check, and a
  documented control that does not execute is worse than an absent one: it stops
  people looking for the real thing. Either way it guards what this module
  *launches*, not what a running program then does with an absolute path.
- **Network** is disabled by clearing proxy variables and setting the
  no-network hints most runtimes honour. This is a **best-effort discouragement,
  not a block** — a determined binary can still open a socket. Real network
  isolation needs a namespace or a container, which is outside a stdlib kit.
  `Result.network_blocked` reports `False` for exactly this reason.
- **Wall clock** is enforced by the parent, and is the one control that is
  actually reliable.
- **Output size** is capped on disk. A runaway process cannot fill the volume.

Because the network control is weak, `run()` refuses to execute anything when
`allow_network=False` *and* the command is on the caller's own deny list. The
module does not pretend to sandbox what it cannot.

Nothing here is a security boundary against hostile code. It is a boundary
against *accidents*: a test that prints a gigabyte, a script that writes outside
the output directory, a command that hangs. Those are the failures that actually
happen when an agent runs commands.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import time
from collections.abc import Sequence

from .core.platform import child_env, is_windows, resolve_command
from .core.security import cap_output, guard_command, redact_secrets

#: Where captured output lives, relative to the data dir.
CAPTURE_SUBDIR = os.path.join("state", "sandbox")

#: Hard ceiling on captured bytes per stream. A process that exceeds it is
#: killed rather than truncated: a partially captured run whose tail is missing
#: looks complete, and the tail is where failures print.
MAX_CAPTURE_BYTES = 32 * 1024 * 1024

#: How much of the output is summarized into the returned shape. Small on
#: purpose — the whole point is that the caller pulls what it needs.
PREVIEW_LINES = 12

#: Environment variables cleared to discourage network access. Clearing proxies
#: does not prevent a direct connection; it removes the configured route that
#: most tooling uses.
_NETWORK_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
                 "NO_PROXY", "no_proxy")

#: Hints honoured by common runtimes to skip network work.
_OFFLINE_HINTS = {
    "PIP_NO_INDEX": "1",
    "npm_config_offline": "true",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "DOBBY_SANDBOX": "1",
}


class SandboxError(RuntimeError):
    """Raised for a request the sandbox refuses to attempt."""


@dataclasses.dataclass
class Capture:
    """A handle to output held on disk rather than in context."""

    handle: str
    path: str
    bytes_total: int
    lines_total: int
    truncated: bool

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Result:
    """What a sandboxed run returns. Deliberately small.

    `preview` is a handful of lines, not the output. Everything else is reached
    through `extract`. If this dataclass ever grows a field holding the full
    text, the module has stopped doing its job.
    """

    command: str
    exit_code: int | None
    duration_s: float
    stdout: Capture | None
    stderr: Capture | None
    preview: str
    timed_out: bool = False
    killed_for_size: bool = False
    error: str | None = None
    #: Always False. Stated as a field so a caller cannot infer isolation this
    #: module does not provide.
    network_blocked: bool = False
    cwd: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error

    def context_cost(self) -> dict:
        """What entered context versus what was produced.

        The number this module exists to move. Reported per run so the saving is
        measured rather than assumed.
        """
        produced = ((self.stdout.bytes_total if self.stdout else 0)
                    + (self.stderr.bytes_total if self.stderr else 0))
        entered = len(self.preview)
        return {
            "bytes_produced": produced,
            "bytes_entered_context": entered,
            "withheld": max(0, produced - entered),
            "withheld_pct": (round(100 * (produced - entered) / produced, 1)
                             if produced else 0.0),
            "note": ("output is held on disk; pull what you need with "
                     "sandbox.extract(handle, ...) rather than reading it whole"),
        }

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["ok"] = self.ok
        d["context_cost"] = self.context_cost()
        return d


def _capture_dir(data_dir: str) -> str:
    path = os.path.join(data_dir, CAPTURE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _handle_for(command: str, started: float) -> str:
    digest = hashlib.sha256(f"{command}{started}".encode("utf-8")).hexdigest()
    return digest[:16]


def _resolve_inside(root: str, candidate: str) -> str:
    """Resolve `candidate` and refuse it if it escapes `root`.

    `os.path.realpath` first, so symlinks cannot be used to step outside a
    directory that passes a textual prefix check.
    """
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, candidate))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise SandboxError(
            f"path escapes the sandbox root: {candidate!r} resolves outside "
            f"{root_real}")
    return target


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process AND its descendants.

    `Popen.kill()` is not sufficient when `shell=True`. The shell is the direct
    child; the command the caller actually asked for is the shell's child. Killing
    the shell orphans the real process, which keeps running, keeps writing to the
    capture files, and keeps holding their handles — so a "timeout" neither stops
    the work nor releases the files, and on Windows the capture directory then
    cannot be deleted. The runaway process the sandbox exists to contain survives
    the mechanism meant to contain it.

    Windows: `taskkill /T` walks the tree. POSIX: the child is started in its own
    session (`start_new_session=True`) so a single `killpg` reaches every
    descendant.
    """
    if proc.poll() is not None:
        return
    if is_windows():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def sandbox_env(*, allow_network: bool) -> dict:
    """Child environment with UTF-8 pinned and network routes removed."""
    env = child_env(_OFFLINE_HINTS if not allow_network else {})
    if not allow_network:
        for var in _NETWORK_VARS:
            env.pop(var, None)
    return env


def run(command: str, *, data_dir: str, cwd: str | None = None,
        root: str | None = None,
        timeout_s: int = 300, allow_network: bool = False,
        max_capture_bytes: int = MAX_CAPTURE_BYTES,
        protected_paths: Sequence[str] = (),
        preview_lines: int = PREVIEW_LINES) -> Result:
    """Run `command` with its output captured to disk instead of returned.

    `root` confines the working directory: `cwd` is resolved through symlinks and
    refused if it lands outside. Without it there is no confinement, which is the
    documented and intended default for a harness that legitimately operates on
    the repository it was pointed at — but it is a real absence, not an implied
    guarantee.

    The command is passed through the same destructive-command guard the
    evaluator uses, so the sandbox does not become a way to run what the rest of
    the kit refuses.
    """
    work_dir = os.path.abspath(cwd or os.getcwd())
    if root is not None:
        # Confinement is applied HERE, in the execution path. Having the check
        # exist as a helper is not the same as running it, and unit-testing the
        # helper directly is how that difference stays invisible.
        work_dir = _resolve_inside(root, os.path.relpath(work_dir, root)
                                   if os.path.isabs(work_dir) else work_dir)
    if not os.path.isdir(work_dir):
        raise SandboxError(f"cwd does not exist: {work_dir}")

    resolved = resolve_command(command)
    allowed, reason = guard_command(resolved, list(protected_paths))
    if not allowed:
        return Result(command=resolved, exit_code=None, duration_s=0.0,
                      stdout=None, stderr=None, preview="",
                      error=f"blocked by command guard: {reason}",
                      cwd=work_dir)

    capture_root = _capture_dir(data_dir)
    started = time.time()
    handle = _handle_for(resolved, started)
    out_path = os.path.join(capture_root, f"{handle}.out")
    err_path = os.path.join(capture_root, f"{handle}.err")

    clock = time.monotonic()
    killed_for_size = False
    timed_out = False
    exit_code: int | None = None

    # Streams go straight to files. Reading into memory first would defeat the
    # size cap — a process emitting a gigabyte would exhaust RAM before anything
    # could be truncated.
    with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
        try:
            proc = subprocess.Popen(
                resolved, shell=True, cwd=work_dir,
                stdin=subprocess.DEVNULL,
                stdout=out_f, stderr=err_f,
                env=sandbox_env(allow_network=allow_network),
                # POSIX: own session, so one killpg reaches every descendant.
                # Ignored on Windows, where _kill_tree uses taskkill /T.
                start_new_session=not is_windows())
        except OSError as exc:
            return Result(command=resolved, exit_code=None,
                          duration_s=round(time.monotonic() - clock, 2),
                          stdout=None, stderr=None, preview="",
                          error=f"could not launch: {exc}", cwd=work_dir)

        deadline = time.monotonic() + timeout_s
        while True:
            exit_code = proc.poll()
            if exit_code is not None:
                break
            if time.monotonic() > deadline:
                _kill_tree(proc)
                timed_out = True
                exit_code = None
                break
            grown = (os.path.getsize(out_path) + os.path.getsize(err_path))
            if grown > max_capture_bytes:
                # Killed, not truncated: a run whose tail is missing looks
                # complete, and the tail is where failures print.
                _kill_tree(proc)
                killed_for_size = True
                exit_code = None
                break
            time.sleep(0.05)

    duration = round(time.monotonic() - clock, 2)
    stdout_cap = _finalize(out_path, handle + ".out", max_capture_bytes)
    stderr_cap = _finalize(err_path, handle + ".err", max_capture_bytes)

    preview = _build_preview(stdout_cap, stderr_cap, preview_lines,
                             failed=(exit_code != 0))
    error = None
    if timed_out:
        error = f"timeout after {timeout_s}s (output up to the kill is kept)"
    elif killed_for_size:
        error = (f"killed after exceeding {max_capture_bytes} captured bytes; "
                 "the run was truncated at the source, so the output is "
                 "incomplete and its tail is missing")

    return Result(command=resolved, exit_code=exit_code, duration_s=duration,
                  stdout=stdout_cap, stderr=stderr_cap, preview=preview,
                  timed_out=timed_out, killed_for_size=killed_for_size,
                  error=error, network_blocked=False, cwd=work_dir)


def _finalize(path: str, handle: str, cap: int) -> Capture | None:
    if not os.path.exists(path):
        return None
    size = os.path.getsize(path)
    if size == 0:
        os.remove(path)
        return None
    lines = 0
    with open(path, "rb") as f:
        for _ in f:
            lines += 1
    return Capture(handle=handle, path=path, bytes_total=size,
                   lines_total=lines, truncated=size >= cap)


def _build_preview(out: Capture | None, err: Capture | None, n: int,
                   *, failed: bool) -> str:
    """A few lines of shape, not the output.

    On failure the preview is taken from the END of stderr, because that is where
    the reason is. On success it is taken from the end of stdout, because a
    successful command's verdict is its last line far more often than its first.
    """
    parts = []
    if out:
        parts.append(f"stdout: {out.lines_total} lines, {out.bytes_total} bytes")
    if err:
        parts.append(f"stderr: {err.lines_total} lines, {err.bytes_total} bytes")
    source = err if (failed and err) else (out or err)
    if source:
        tail = _tail_lines(source.path, n)
        parts.append(f"--- last {min(n, source.lines_total)} line(s) of "
                     f"{'stderr' if source is err else 'stdout'} ---")
        parts.append(redact_secrets(tail))
    return "\n".join(parts) if parts else "(no output)"


def _tail_lines(path: str, n: int) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:]).rstrip("\n")


# --------------------------------------------------------------------------
# Extraction — the only way output becomes tokens
# --------------------------------------------------------------------------

def extract(capture: Capture, *, pattern: str | None = None,
            head: int | None = None, tail: int | None = None,
            around: int = 0, max_lines: int = 200,
            max_chars: int = 8000) -> dict:
    """Pull a bounded slice out of a capture.

    Every extraction is bounded twice — by line count and by character count —
    because either alone is escapable: 200 lines of minified JSON is one long
    line's worth of tokens, and 8000 characters of a stack trace is only a few
    lines. Both caps are reported when they bind, so a caller is never handed a
    silently partial answer.

    Exactly one selector should be given. Combining `pattern` with `head` reads
    as "the first N matches" to some callers and "matches within the first N
    lines" to others, so it is refused rather than guessed at.
    """
    selectors = [s for s in (pattern, head, tail) if s]
    if len(selectors) > 1:
        raise SandboxError(
            "give exactly one of pattern / head / tail — combining them is "
            "ambiguous, and guessing which reading was meant produces a wrong "
            "answer that looks right")
    if not capture.exists():
        return {"error": f"capture {capture.handle} is gone: {capture.path}"}

    with open(capture.path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if head:
        chosen = list(enumerate(lines[:head], start=1))
        selector = f"head {head}"
    elif tail:
        start = max(0, len(lines) - tail)
        chosen = list(enumerate(lines[start:], start=start + 1))
        selector = f"tail {tail}"
    elif pattern:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad pattern {pattern!r}: {exc}"}
        hits = [i for i, line in enumerate(lines) if regex.search(line)]
        wanted: set[int] = set()
        for i in hits:
            wanted.update(range(max(0, i - around),
                                min(len(lines), i + around + 1)))
        chosen = [(i + 1, lines[i]) for i in sorted(wanted)]
        selector = f"pattern {pattern!r}" + (f" ±{around}" if around else "")
    else:
        chosen = list(enumerate(lines[:max_lines], start=1))
        selector = "default head"

    total_matched = len(chosen)
    line_capped = total_matched > max_lines
    chosen = chosen[:max_lines]

    text = "".join(f"{n}: {line}" for n, line in chosen)
    char_capped = len(text) > max_chars
    text = cap_output(redact_secrets(text), max_chars)

    return {
        "handle": capture.handle,
        "selector": selector,
        "matched_lines": total_matched,
        "returned_lines": len(chosen),
        "text": text,
        "line_cap_hit": line_capped,
        "char_cap_hit": char_capped,
        "source_lines_total": capture.lines_total,
        "note": (("result was capped ("
                  + ", ".join(c for c, hit in
                              (("line cap", line_capped),
                               ("character cap", char_capped)) if hit)
                  + "); narrow the pattern or raise the cap deliberately")
                 if (line_capped or char_capped) else
                 "complete for this selector"),
    }


def grep(capture: Capture, pattern: str, **kwargs) -> dict:
    """Convenience: the extraction agents actually want most of the time."""
    return extract(capture, pattern=pattern, **kwargs)


def sweep(data_dir: str, *, keep_hours: float = 24.0) -> dict:
    """Delete captures older than `keep_hours`.

    Captures are the only thing this module leaves on disk, and they are exactly
    as large as the output it withheld — so an unmanaged capture directory grows
    at the rate the context window was saved. Sweeping is not optional
    housekeeping; it is the other half of the trade.
    """
    root = _capture_dir(data_dir)
    cutoff = time.time() - keep_hours * 3600
    removed, kept, freed = 0, 0, 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        if os.path.getmtime(path) < cutoff:
            freed += os.path.getsize(path)
            os.remove(path)
            removed += 1
        else:
            kept += 1
    return {"removed": removed, "kept": kept, "bytes_freed": freed,
            "root": root,
            "note": (f"captures hold exactly the bytes withheld from context; "
                     f"{kept} remain")}
