"""Every Python file the agent changed still parses. Run inside the clone.

This is the D arm's acceptance check and it is deliberately weak. The strong
check — the instance's own FAIL_TO_PASS and PASS_TO_PASS — needs the pinned
per-instance environment, which in practice needs the official Docker images,
and Docker is absent on this machine. Pretending otherwise would be the worse
error, so the loop is given a check it can actually run and the SCORE comes from
somewhere the arm cannot see.

Why a check at all, rather than a no-op: the project loop refuses an item with no
machine-checkable acceptance, and giving it `exit 0` would make its gate
decorative — every run would report DONE and the arm's own notion of success
would carry no information at all. Syntax is the strongest thing available here,
and it is not nothing: an agent that leaves a half-written edit fails it.

Why it cannot leak the answer: it reads `git status`, never the gold patch. It
knows which files MOVED, not which files SHOULD have.
"""

from __future__ import annotations

import os
import subprocess
import sys


def changed_python_files(repo: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    out = []
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.endswith(".py"):
            out.append(path.replace("\\", "/"))
    return sorted(out)


def main() -> int:
    repo = os.getcwd()
    files = changed_python_files(repo)
    if not files:
        # An agent that changed nothing has not satisfied the item. Reporting
        # this as a pass would let a provider that silently did nothing register
        # as DONE, which is the failure the run exists to catch.
        print("no python file was changed; nothing was done")
        return 1

    broken = []
    for rel in files:
        path = os.path.join(repo, rel)
        if not os.path.exists(path):          # deleted; nothing to parse
            continue
        # `compile()` on the text rather than `py_compile`: py_compile insists on
        # writing a .pyc somewhere, and pointing it at os.devnull raises
        # FileExistsError on Windows, where devnull is `nul`. That failed EVERY
        # file including valid ones — measured, and it would have scored a
        # correct edit as a syntax error.
        try:
            with open(path, "rb") as fh:
                source = fh.read()
            compile(source, rel, "exec")
        except SyntaxError as exc:
            broken.append(f"{rel}:{exc.lineno}: {exc.msg}")
        except (OSError, ValueError) as exc:
            broken.append(f"{rel}: {type(exc).__name__}: {exc}")

    if broken:
        print(f"{len(broken)} of {len(files)} changed file(s) do not parse:")
        for row in broken:
            print("  " + row)
        return 1

    print(f"{len(files)} changed python file(s) parse")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
