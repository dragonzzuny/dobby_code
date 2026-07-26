"""Turn a failing step's output into GitHub annotations.

WHY

Job logs need admin rights on this repository. Measured, not assumed:

    GET /actions/jobs/{id}/logs  -> 403 "Must have admin rights to Repository."
    GET /actions/runs/{id}/logs  -> 403 "Must have admin rights to Repository."
    GET /check-runs/{id}/annotations -> 200

So annotations are the only channel that carries failure detail off a runner and
back to someone holding no token. That asymmetry is why fourteen red runs went
undiagnosed: the pipeline reported *that* it failed and kept *why* behind a
permission the reader did not have.

Piping a step through this script puts the failing blocks in a place the public
API serves, which turns "windows-latest failed" into a named test and a
traceback.

USAGE

The workflow does not call this directly - `tools/ci_step.py` wraps a command,
streams it, and calls in here on failure, which avoids forcing `shell: bash` onto
the Windows jobs that are the ones being debugged. This entry point stays for the
case where a log already exists on disk:

    python tools/ci_annotate.py step.log --title "engine tests"

Outside CI it just prints the workflow commands, so the extraction is testable
locally without pushing anything.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# GitHub truncates a single annotation; keeping well under the documented ceiling
# costs nothing and a truncated traceback is worse than a split one.
MAX_MESSAGE_CHARS = 3000
MAX_ANNOTATIONS = 12

# unittest starts each failure block with one of these, and the block runs until
# the next separator line or the next block header.
BLOCK_START = re.compile(r"^(FAIL|ERROR):\s+(\S+)")
SEPARATOR = re.compile(r"^[=-]{20,}$")
# Steps that are not unittest (a CLI command) have no blocks; fall back to a tail.
SUMMARY = re.compile(r"^(Ran \d+ tests?|OK|FAILED)\b")


def emit(line: str) -> None:
    """Write one workflow command without ever dying on the encoding.

    A plain print() is wrong here. The runner's stdout is the ANSI code page
    unless UTF-8 mode is on - cp1252 on GitHub's Windows images - and this
    project's test output contains em dashes and Korean. Printing a block that
    holds either one raises UnicodeEncodeError *inside the reporter*, so the step
    that failed for one reason reports a second, unrelated failure and buries the
    first. A diagnostic that can be killed by the content it is diagnosing is not
    a diagnostic.

    Unrepresentable characters become backslash escapes: still readable, never
    fatal.
    """
    encoding = sys.stdout.encoding or "utf-8"
    safe = line.encode(encoding, "backslashreplace").decode(encoding, "replace")
    sys.stdout.write(safe + "\n")
    sys.stdout.flush()


def escape(message: str) -> str:
    """Escape per the workflow-command spec.

    Order matters: `%` must be escaped before the sequences that introduce `%`,
    otherwise the escapes themselves get double-escaped and the annotation shows
    literal `%250A` instead of a newline.
    """
    return (message.replace("%", "%25")
                   .replace("\r", "%0D")
                   .replace("\n", "%0A"))


def extract_blocks(text: str) -> list[str]:
    """Pull out the FAIL:/ERROR: blocks, in order, whole."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if BLOCK_START.match(line):
            if current:
                blocks.append(current)
            current = [line]
            continue
        if current is not None:
            if SEPARATOR.match(line) and len(current) > 1:
                blocks.append(current)
                current = None
                continue
            if SUMMARY.match(line):
                blocks.append(current)
                current = None
                continue
            current.append(line)
    if current:
        blocks.append(current)
    return ["\n".join(b).strip() for b in blocks if any(x.strip() for x in b)]


def fallback_tail(text: str, lines: int = 40) -> list[str]:
    """No unittest blocks: keep the tail, which is where a traceback ends up."""
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    return ["\n".join(tail)] if tail else []


def chunk(message: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    if len(message) <= limit:
        return [message]
    out, buf = [], []
    size = 0
    for line in message.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            out.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        out.append("".join(buf))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", help="captured step output")
    ap.add_argument("--title", default="step failed",
                    help="annotation title, e.g. the step name")
    ap.add_argument("--max", type=int, default=MAX_ANNOTATIONS)
    args = ap.parse_args()

    if not os.path.exists(args.logfile):
        emit(f"::error title={args.title}::no log at {args.logfile} - the step "
              f"produced no capture, which is itself the finding")
        return 0

    with open(args.logfile, encoding="utf-8", errors="replace") as f:
        text = f.read()

    blocks = extract_blocks(text) or fallback_tail(text)
    if not blocks:
        emit(f"::error title={args.title}::step failed with empty output")
        return 0

    # Platform and interpreter are the two variables that made these failures
    # invisible locally, so every annotation carries them.
    where = (f"{sys.platform} py{sys.version_info[0]}.{sys.version_info[1]} "
             f"enc={sys.stdout.encoding} utf8_mode={sys.flags.utf8_mode} "
             f"cwd={os.getcwd()}")

    emitted = 0
    emit(f"::error title={args.title} (context)::{escape(where)}")
    emitted += 1
    for index, block in enumerate(blocks, 1):
        for part in chunk(block):
            if emitted >= args.max:
                emit(f"::error title={args.title}::"
                      f"{len(blocks) - index + 1} further block(s) not "
                      f"annotated (limit {args.max}) - the full set is in the "
                      f"step log")
                return 0
            emit(f"::error title={args.title} [{index}]::{escape(part)}")
            emitted += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
