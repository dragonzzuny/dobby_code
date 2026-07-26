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


#: Windows extensions that are interpreted by cmd.exe's batch parser rather than
#: launched directly. npm ships its CLIs this way.
_BATCH_EXTENSIONS = (".cmd", ".bat")


def shim_safe_argv(resolved: str,
                   args: "list[str]") -> "tuple[list[str] | None, str]":
    """Build an argv that delivers `args` INTACT, or refuse and say why.

    A `.CMD` shim silently truncates any argument at its first newline. Measured
    with the same string through both routes:

        via .CMD shim    ["line one"]
        direct exe       ["line one\\nline two\\nline three"]

    Everything else survives - `%`, `&&`, `^`, `|` all arrive unharmed - so the
    hazard is narrow and specific: multi-line arguments, and npm installs every
    one of its CLIs as a `.CMD` on Windows.

    This is why a judge prompt came back with "Ready to grade, but I'm missing the
    inputs": the provider received only the prompt's first line, which was the
    preamble, and answered it faithfully. Nothing errored. A round of that is
    worse than a failure, because the reply looks like an opinion about the work.

    npm also installs a `.ps1` beside the `.CMD`, and PowerShell's `-File` mode
    does preserve newlines and percent signs (measured). So a batch shim carrying
    a multi-line argument is re-routed through the vendor's own PowerShell shim.

    The execution policy is `RemoteSigned`, and that number was measured rather
    than guessed. An earlier version of this used `Bypass` on the assumption that
    an unsigned shim needed it. `codex.ps1` is indeed `NotSigned`, and with every
    policy scope `Undefined` the machine default refuses to run it at all — but:

        flag omitted (default)   rc 1, argument not delivered
        RemoteSigned             rc 0, delivered
        AllSigned                rc 1, script is unsigned
        Bypass                   rc 0, delivered

    `RemoteSigned` is sufficient and strictly narrower: it still refuses an
    unsigned script that carries the internet-zone marker, which is the case worth
    refusing. If a provider's shim is ever blocked for that reason the error says
    so, and that refusal is correct rather than something to override here.

    When there is no `.ps1` to fall back to, this returns `(None, reason)`. A
    truncated prompt must never be sent silently - the caller refuses instead.
    """
    argv = [resolved] + list(args)
    if not is_windows():
        return argv, ""
    if os.path.splitext(resolved)[1].lower() not in _BATCH_EXTENSIONS:
        return argv, ""
    multiline = [a for a in args if isinstance(a, str) and ("\n" in a or "\r" in a)]
    if not multiline:
        return argv, ""

    sibling = os.path.splitext(resolved)[0] + ".ps1"
    if os.path.exists(sibling):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            return ([powershell, "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                     "-File", sibling] + list(args),
                    f"multi-line argument re-routed through {os.path.basename(sibling)}: "
                    f"a .cmd shim truncates it at the first newline")
        return None, (
            f"{os.path.basename(resolved)} is a batch shim that would truncate a "
            f"multi-line argument at its first newline, and neither pwsh nor "
            f"powershell is on PATH to use {os.path.basename(sibling)} instead")
    return None, (
        f"{os.path.basename(resolved)} is a batch shim, which truncates a "
        f"multi-line argument at its first newline, and there is no "
        f"{os.path.basename(sibling)} beside it to route through. Sending the "
        f"argument would deliver only its first line, and the reply would look "
        f"like an answer to the whole thing")


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
