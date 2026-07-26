"""Run one CI step, stream its output, and annotate it if it fails.

WHY NOT A SHELL WRAPPER

The obvious version of this is three lines of bash with `tee` and `PIPESTATUS`.
Using it would mean setting `shell: bash` on the Windows jobs, and the Windows
jobs are the ones failing. Swapping pwsh for MSYS bash changes PATH, changes
path translation, and changes which `sh` the code under test can find - so the
debugging tool would alter the environment being debugged. This runs the child
directly with no shell at all, on every platform, which keeps the variable being
measured the only one that changed.

Output is streamed as it arrives rather than captured and printed at the end, so
a step that hangs still shows how far it got before the job timeout.

    python tools/ci_step.py --title "engine tests" -- python -m unittest discover -s tests -q

Exit code is the child's, so the workflow still fails.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ci_annotate import (chunk, emit, escape, extract_blocks,
                         fallback_tail)  # noqa: E402

MAX_ANNOTATIONS = 12


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--timeout", type=int, default=3000)
    ap.add_argument("argv", nargs=argparse.REMAINDER,
                    help="the command, after --")
    args = ap.parse_args()

    argv = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    if not argv:
        emit("::error::ci_step.py got no command to run")
        return 2

    # The child inherits the environment untouched. Adding PYTHONUTF8 here would
    # reintroduce exactly the masking that hid these failures locally.
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        emit(f"::error title={args.title}::cannot start {argv[0]!r}: {exc}")
        return 127

    assert proc.stdout is not None
    encoding = sys.stdout.encoding or "utf-8"
    try:
        for line in proc.stdout:
            # The child's bytes were decoded as UTF-8; this process's stdout may
            # be cp1252, which cannot hold Korean or an em dash. Writing the line
            # straight through would make the wrapper die on the very output it
            # exists to relay, turning a real failure into a UnicodeEncodeError
            # from the tooling. What gets CAPTURED stays exact - only the copy
            # echoed to the log is downgraded.
            sys.stdout.write(line.encode(encoding, "backslashreplace")
                                 .decode(encoding, "replace"))
            sys.stdout.flush()
            captured.append(line)
        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        emit(f"::error title={args.title}::exceeded {args.timeout}s and was "
              f"killed; last lines follow%0A"
              + escape("".join(captured[-30:])))
        return 124

    rc = proc.returncode
    if rc == 0:
        return 0

    text = "".join(captured)
    blocks = extract_blocks(text) or fallback_tail(text)
    where = (f"{sys.platform} py{sys.version_info[0]}.{sys.version_info[1]} "
             f"enc={sys.stdout.encoding} utf8_mode={sys.flags.utf8_mode} "
             f"cwd={os.getcwd()} tmp={tempfile.gettempdir()} rc={rc}")
    emit(f"::error title={args.title} (context)::{escape(where)}")

    emitted = 1
    for index, block in enumerate(blocks, 1):
        for part in chunk(block):
            if emitted >= MAX_ANNOTATIONS:
                emit(f"::error title={args.title}::"
                      f"{len(blocks) - index + 1} further block(s) not "
                      f"annotated (limit {MAX_ANNOTATIONS})")
                return rc
            emit(f"::error title={args.title} [{index}]::{escape(part)}")
            emitted += 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
