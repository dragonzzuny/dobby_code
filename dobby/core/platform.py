"""Cross-platform command resolution.

Command templates live in DATA (`.dobby/registry/capabilities.json`,
`.dobby/criteria/*.json`, `.dobby/slice_plans.json`) so a host project can
register its own build/test/lint commands without touching engine code. Those
templates must therefore be portable across the shells this kit actually runs
in: POSIX `sh` on Linux/macOS and `cmd.exe` on native Windows.

Two portability hazards are handled here:

1. **The interpreter name.** `python3` does not exist on a default Windows
   install (`cmd.exe` returns exit 9009, "command not found"), and on some
   Linux images `python` is absent instead. Neither name is safe to hard-code.
   Templates write `{python}` and this module substitutes the interpreter that
   is *actually running the engine* (`sys.executable`), so the subprocess can
   never disagree with the parent about which Python it is.

2. **Spaces in the interpreter path.** The common Windows location is
   `C:\\Program Files\\Python311\\python.exe`. Because every execution site
   runs with `shell=True`, an unquoted path splits at the space and the shell
   tries to run `C:\\Program`. Substitution quotes the path when needed.

`resolve_command` is deliberately the ONLY transformation applied: it is not a
general templating engine, and it never inspects or rewrites the rest of the
command. Anything else a template needs stays the host's responsibility, which
keeps the command that gets audited identical in shape to the command the
author wrote.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from functools import lru_cache

#: Placeholder that data files use in place of a hard-coded interpreter name.
PYTHON_PLACEHOLDER = "{python}"

#: Legacy interpreter names rewritten for portability. Matched only as a whole
#: leading token so a path such as `tools/python3-wrapper.sh` is left alone.
_LEGACY_NAMES = ("python3", "python")


def python_executable() -> str:
    """The interpreter running this engine, shell-quoted when necessary.

    Uses `sys.executable` rather than a PATH lookup so a subprocess inherits
    the exact interpreter (and therefore the exact site-packages, including
    PyYAML) that the caller already validated by importing this module.
    """
    exe = sys.executable or "python"
    if " " in exe:
        # cmd.exe understands double quotes; POSIX shells accept them too, so
        # one form covers both. shlex.quote would emit single quotes, which
        # cmd.exe treats as literal characters.
        return f'"{exe}"'
    return exe


def resolve_command(command: str) -> str:
    """Substitute the interpreter into a data-defined command template.

    Replaces every `{python}` placeholder, and — for templates written before
    the placeholder existed — a leading bare `python3`/`python` token.
    Returns the command unchanged when neither appears.
    """
    if not command:
        return command
    exe = python_executable()
    resolved = command.replace(PYTHON_PLACEHOLDER, exe)
    if resolved != command:
        # An explicit placeholder is authoritative: do not also rewrite a
        # legacy name that may legitimately appear later in the command.
        return resolved
    stripped = resolved.lstrip()
    for name in _LEGACY_NAMES:
        # Require a following space so `python3` alone (a version probe such as
        # `python3 --version`) and `python3 -m x` both match, but `python3x`
        # does not.
        if stripped.startswith(name + " ") or stripped == name:
            indent = resolved[: len(resolved) - len(stripped)]
            return indent + exe + stripped[len(name):]
    return resolved


def is_windows() -> bool:
    """True on native Windows (not WSL, which reports `linux`)."""
    return os.name == "nt"


def process_alive(pid: int) -> "bool | None":
    """True if `pid` is running, False if it is not, None if it cannot be told.

    Three-valued deliberately. "I cannot tell" is not "it is dead", and a lease
    recovery that collapses the two either strands work forever or takes a node
    away from the worker still executing it.

    Windows needs more than opening the process. A handle can be opened for a
    process that has already exited as long as somebody still holds a handle to
    it — the test suite holds exactly that, through the `Popen` object of a
    process it just killed — so the exit code, not the open, is the answer.
    `os.kill(pid, 0)` is NOT the portable spelling: on Windows `os.kill` calls
    TerminateProcess, so the POSIX liveness idiom would kill the process it was
    asked about.

    Caveats, both narrow and both real: a Windows process whose real exit code
    is 259 (`STILL_ACTIVE`) reads as alive, and a PID reused between the lease
    and this call reads as the original holder. The lease expiry is the bound on
    both — it is why callers must treat an expired lease as recoverable no
    matter what this returns.
    """
    if pid <= 0:
        return None
    if is_windows():
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # Without an explicit restype ctypes truncates the 64-bit HANDLE to
            # an int, and the CloseHandle that follows closes a handle that was
            # never opened.
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int,
                                             ctypes.c_ulong)
            kernel32.GetExitCodeProcess.argtypes = (
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to somebody else. Existence is the question.
        return True
    except OSError:
        return None
    return True


#: Emitted by the probe below. Deliberately ASCII and unlikely to occur by
#: accident, so a shell that prints something else is not mistaken for a pass.
_POSIX_PROBE_TOKEN = "dobby_posix_shell_ok"

#: Where Git for Windows puts a real POSIX shell. Worth checking explicitly:
#: it is frequently installed and frequently absent from PATH, and it is the one
#: Windows shell that both speaks POSIX and understands `D:/a/repo` paths.
_GIT_BASH_GUESSES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
)


def _posix_shell_candidates() -> list[str]:
    from shutil import which
    found: list[str] = []
    for name in ("sh", "bash", "dash", "zsh"):
        path = which(name)
        if path and path not in found:
            found.append(path)
    if is_windows():
        for guess in _GIT_BASH_GUESSES:
            if os.path.exists(guess) and guess not in found:
                found.append(guess)
    return found


@lru_cache(maxsize=1)
def posix_shell_path() -> str | None:
    """A shell that can actually RUN a POSIX script here, or None.

    This used to be `which("sh") or which("bash")`, and that was wrong in the way
    this project keeps rediscovering: existence is not a measurement. On a GitHub
    Windows runner `bash` resolves to `C:\\Windows\\System32\\bash.exe`, the WSL
    launcher. With no distribution installed it prints, in UTF-16LE,

        Windows Subsystem for Linux has no installed distributions.

    and exits 1. The old check saw a file named bash and said yes, so a suite
    guarded by `skipUnless(posix_shell_available(), ...)` did not skip — it ran,
    the installer never executed, and seven downstream assertions failed with
    messages about missing files. The guard was present and the predicate was
    the bug.

    The same shape as the `python3` hazard this module already handles: a name on
    PATH that resolves to a stub which cannot do the job.

    So the shell is probed, and the probe asks for the capability actually needed
    rather than a greeting: resolve a path of the form Python hands out and
    confirm the file is there. That distinction matters because a WSL bash WITH a
    distribution installed would happily echo a token while being unable to see
    `D:/a/repo` at all — functional as a shell, useless for running an installer
    against Windows paths.

    Cached: the answer cannot change within a process, and callers ask often.
    `posix_shell_path.cache_clear()` exists for tests.
    """
    import subprocess

    probe_path = os.path.abspath(__file__).replace("\\", "/")
    script = f'test -f "{probe_path}" && printf %s {_POSIX_PROBE_TOKEN}'
    for shell in _posix_shell_candidates():
        try:
            proc = subprocess.run(
                [shell, "-c", script], capture_output=True, timeout=20,
                stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        # Decoded permissively on purpose: a UTF-16LE reply is itself the WSL
        # launcher's signature, and it simply will not contain the ASCII token.
        out = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode == 0 and _POSIX_PROBE_TOKEN in out:
            return shell
    return None


#: What `install.sh` actually requires. It declares `#!/usr/bin/env bash` and uses
#: `set -o pipefail` and `${BASH_SOURCE[0]}`, neither of which is POSIX.
_BASH_PROBE = (
    'set -o pipefail || exit 1\n'
    ': "${BASH_SOURCE[0]-}" || exit 1\n'
    'test -f "%s" || exit 1\n'
    'printf %%s %s\n'
)


@lru_cache(maxsize=1)
def bash_path() -> str | None:
    """A shell that can run THIS PROJECT'S bash scripts, or None.

    Distinct from `posix_shell_path` on purpose, and the distinction cost a green
    pipeline to learn. `install.sh` was being run through `posix_shell_path()`,
    which on Ubuntu resolves `/bin/sh` — dash — and dash answers:

        install.sh: 24: set: Illegal option -o pipefail

    Windows went green and Linux went red in the same commit. The probe was asking
    "can you resolve a path", while the script needs "do you support pipefail and
    BASH_SOURCE". Probing for a capability is only progress if it is the capability
    the caller depends on; a capability probe aimed at the wrong capability is just
    a slower way to be wrong.

    So this probe exercises the three things that actually matter: `pipefail`,
    `BASH_SOURCE` (a bash-only array whose subscript syntax dash rejects), and the
    ability to see a path in the form Python hands out — which is what excludes the
    WSL launcher on Windows.
    """
    import subprocess

    probe_path = os.path.abspath(__file__).replace("\\", "/")
    script = _BASH_PROBE % (probe_path, _POSIX_PROBE_TOKEN)

    candidates: list[str] = []
    for name in ("bash",):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if is_windows():
        for guess in _GIT_BASH_GUESSES:
            if guess.lower().endswith("bash.exe") and os.path.exists(guess):
                if guess not in candidates:
                    candidates.append(guess)

    for shell in candidates:
        try:
            proc = subprocess.run([shell, "-c", script], capture_output=True,
                                  timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        out = (proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode == 0 and _POSIX_PROBE_TOKEN in out:
            return shell
    return None


#: Windows extensions that are interpreted by cmd.exe's batch parser rather than
#: launched directly. npm ships its CLIs this way.
_BATCH_EXTENSIONS = (".cmd", ".bat")


#: The JS entry point an npm shim forwards to. Both the `.cmd` and the `.ps1`
#: reference it relative to the shim's own directory (`%dp0%` / `$basedir`).
_NPM_TARGET_RE = re.compile(
    r"(?:%dp0%|\$basedir)[\\/]((?:node_modules)[\\/][^\"\s]+?\.js)",
    re.IGNORECASE)


def npm_shim_target(resolved: str) -> "tuple[str, str] | None":
    """`(node_exe, script)` if `resolved` is an npm shim wrapping a node script.

    Bypassing the shim is not a micro-optimisation, it is the only route that
    carries an argument intact. Measured on this machine, one argument through
    three routes, compared by sha256 of its UTF-8 bytes so the instrument cannot
    corrupt the result:

                              .cmd    powershell -File    node directly
        newline               BROKEN  ok                  ok
        double quote          ok      BROKEN              ok
        quote + newline       BROKEN  BROKEN              ok
        Korean, em dash       ok      ok                  ok
        Korean+quote+newline  BROKEN  BROKEN              ok

    A first version of that matrix reported Korean and an em dash broken on all
    three routes. They were not: the echo script was printing non-ASCII to a cp949
    stdout, so the measurement was of the instrument. The corrected run above
    compares hashes of the arguments themselves.

    The PowerShell reroute this replaces was added after testing newlines and
    percent signs, and never tested with a double quote. It then failed on the
    first real prompt that contained one - every call died with
    `error: unexpected argument '...'` because the rules text says
    `"3 failures" without the three names is not a finding`. Incomplete coverage
    of my own fix, in exactly the class of defect the fix was for.

    An npm shim is a standard wrapper - `node "<dir>/node_modules/.../cli.js"
    %*` - so the target is extractable, and `node.exe` is a real executable whose
    argv Python hands to CreateProcess unmodified.
    """
    if not is_windows():
        return None
    base, extension = os.path.splitext(resolved)
    if extension.lower() not in _BATCH_EXTENSIONS + (".ps1",):
        return None
    directory = os.path.dirname(resolved)
    for candidate in (base + ".ps1", base + ".cmd", base + ".bat"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        match = _NPM_TARGET_RE.search(text)
        if not match:
            continue
        script = os.path.normpath(os.path.join(directory,
                                              match.group(1).replace("/", os.sep)))
        if not os.path.exists(script):
            continue
        local_node = os.path.join(directory, "node.exe")
        node = local_node if os.path.exists(local_node) else shutil.which("node")
        if node:
            return node, script
    return None


#: Characters a batch shim cannot carry in an argument. Newline is the measured
#: one; a lone carriage return behaves the same way.
_BATCH_HOSTILE = ("\n", "\r")


def shim_safe_argv(resolved: str,
                   args: "list[str]") -> "tuple[list[str] | None, str]":
    """Build an argv that delivers `args` INTACT, or refuse and say why.

    Windows offers three ways to launch an npm-installed CLI and only one of them
    carries an arbitrary string. Measured, comparing sha256 of each argument's
    UTF-8 bytes so the measurement cannot be confused with a console encoding:

                              .cmd    powershell -File    node directly
        newline               BROKEN  ok                  ok
        double quote          ok      BROKEN              ok
        quote + newline       BROKEN  BROKEN              ok
        Korean, em dash       ok      ok                  ok

    So the order is: go straight to node when the shim reveals its target, which
    handles everything; otherwise use the shim only for arguments it can carry;
    otherwise REFUSE. A prompt that arrives truncated or split is worse than a
    failed call, because the reply looks like an answer to the whole thing.

    The PowerShell route is gone rather than kept as a second choice. It is
    strictly worse than node-direct and it silently mangles the one character -
    a double quote - that appears in almost every prompt carrying quoted text.
    """
    argv = [resolved] + list(args)
    if not is_windows():
        return argv, ""
    if os.path.splitext(resolved)[1].lower() not in _BATCH_EXTENSIONS:
        return argv, ""

    target = npm_shim_target(resolved)
    if target is not None:
        node, script = target
        return ([node, script] + list(args),
                f"launched via {os.path.basename(node)} and the shim's own entry "
                f"point; a batch shim cannot carry a newline and PowerShell -File "
                f"cannot carry a double quote")

    hostile = [a for a in args
               if isinstance(a, str) and any(c in a for c in _BATCH_HOSTILE)]
    if not hostile:
        return argv, ""

    return None, (
        f"{os.path.basename(resolved)} is a batch shim, which truncates an "
        f"argument at its first newline, and its wrapped entry point could not be "
        f"identified so the launch cannot bypass it. Sending the argument would "
        f"deliver only its first line, and the reply would look like an answer to "
        f"the whole thing")


def posix_shell_available() -> bool:
    """Whether a POSIX shell that actually works is available.

    Data-defined commands that need `&&`, pipes, or `2>/dev/null` are portable
    only where this is true. Callers use it to SKIP such a capability with a
    stated reason rather than to silently run it and misreport the failure as a
    defect in the thing being tested — which is exactly what happened while this
    was a `which()` call. See `posix_shell_path`.
    """
    return posix_shell_path() is not None


def force_utf8_io() -> None:
    """Make stdin/stdout/stderr UTF-8 regardless of the system locale.

    Python picks the *locale* encoding for standard streams on Windows, which is
    a legacy code page (cp1252 in the US, cp949 on a Korean install, cp936 on a
    Chinese one) — not UTF-8. Every JSON payload this engine emits can contain
    non-ASCII text: knowledge-graph summaries, policy prose, em dashes in
    docstrings, and user task text in any language. Encoding such a payload with
    a legacy code page raises UnicodeEncodeError and the process dies mid-
    protocol, which a JSON-RPC client sees as an opaque internal error rather
    than as an encoding problem.

    JSON-RPC over stdio is defined in terms of UTF-8, so pinning the streams is
    correctness, not a preference. `errors="replace"` on the *output* side keeps
    a single unencodable character from killing a long-running session; input is
    left strict so malformed client bytes surface as an explicit error instead of
    being silently corrupted into a valid-looking request.

    Safe to call more than once, and a no-op on Python builds whose streams are
    not reconfigurable (e.g. already-wrapped streams under some test harnesses).
    """
    for stream, errors in ((sys.stdin, "strict"),
                           (sys.stdout, "replace"),
                           (sys.stderr, "replace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError):
            # Detached or non-text stream: leave it as the runtime set it up.
            continue


def child_env(extra: dict | None = None) -> dict:
    """Environment for a subprocess this engine spawns, with UTF-8 pinned.

    `force_utf8_io` only fixes the *current* process. A child Python process
    re-derives its stream encodings from its own environment, so a capability
    template such as `{python} -m unittest ...` would go back to the legacy code
    page and its output would arrive mojibake'd — or raise UnicodeDecodeError in
    the parent, which reads as "the tests crashed" when the tests were fine.

    `PYTHONIOENCODING` makes the child's streams UTF-8 regardless of locale, and
    `PYTHONUTF8=1` enables UTF-8 mode so the child's *file* reads/writes default
    to UTF-8 too (knowledge-graph JSON, ledgers, trajectories). Both are honored
    by CPython 3.7+; on non-Python children they are simply ignored, which is
    why setting them unconditionally is safe.

    Inherits the real environment so PATH, auth tokens, and virtualenv variables
    still reach the child. `extra` overrides last.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


#: Every environment variable this engine reads, with what it does when unset.
#:
#: Declared in one place so `doctor` can report the ones an operator has
#: TURNED ON. A machine behaving differently from the defaults is the first
#: thing a diagnosis needs, and until now `doctor` reported the platform, the
#: files and the fleet -- and not the switches, which are the only part a human
#: chose. Two of the three were in no document either.
#:
#: A name here that nothing reads is a lie of the same kind this table exists
#: to prevent, so `tests/test_switches.py` asserts each one is read by the
#: module that claims it.
SWITCHES: tuple = (
    ("DOBBY_SQLITE_SYNCHRONOUS",
     "dobby/runtime/store.py",
     "FULL",
     "sqlite commit durability; NORMAL is ~15x faster per transaction and "
     "loses the most recent commits on an OS crash or power cut"),
    ("DOBBY_REQUIRE_PINNED_MODEL",
     "dobby/providers/models.py",
     "off",
     "when set, a provider answering with a model other than the one pinned "
     "fails the node instead of being recorded and accepted"),
    ("DOBBY_APPROVAL_DIR",
     "dobby/gates.py",
     "(the repo's own)",
     "where gate approval records are read from and written to"),
)


def switches() -> list[dict]:
    """What the operator has turned on, and what each one changes.

    Only the SET ones carry a value: printing every default would bury the one
    line that explains why this machine behaves unlike the last one.
    """
    import os as _os

    out = []
    for name, where, default, what in SWITCHES:
        raw = _os.environ.get(name)
        out.append({"name": name, "set": raw is not None,
                    "value": raw if raw is not None else None,
                    "default": default, "read_by": where, "changes": what})
    return out


def describe_platform() -> dict:
    """Facts a report can cite about what this machine can and cannot verify."""
    return {
        "os_name": os.name,
        "sys_platform": sys.platform,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "posix_shell": posix_shell_available(),
        "posix_shell_path": posix_shell_path(),
        "shlex_quote_example": shlex.quote("a b"),
    }
