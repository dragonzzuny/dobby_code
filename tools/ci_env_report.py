"""Print the environment values that decide whether the tests can pass.

A file rather than a `python -c` one-liner on purpose. The workflow runs on pwsh
for Windows and bash elsewhere, and a one-liner holding nested quotes, `==` and
parentheses is quoted differently by each - the report meant to remove ambiguity
would become a new source of it.

Each value here corresponds to a failure this project has actually had:

  stdout encoding    cp1252 on the runner, cp949 on the maintainer's machine.
                     The two are complementary, measured: cp949 encodes Korean
                     but not an em dash, cp1252 the reverse. A test whose output
                     contains either can therefore pass on one Windows box and
                     die on another with UnicodeEncodeError.
  utf8_mode          Local shells export PYTHONUTF8=1; the workflow does not.
                     Every encoding failure was invisible locally for this reason.
  cwd vs tempdir     On the runner the workspace is on D: and TEMP on C:.
                     os.path.relpath across volumes raises ValueError on Windows
                     and never on POSIX.
  provider CLIs      A runner has none. Their presence changes fleet size, panel
                     composition and several verdicts.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile

PROVIDER_BINS = ("claude", "codex", "gemini", "agy", "qwen", "ollama")


def main() -> int:
    cwd = os.getcwd()
    tmp = tempfile.gettempdir()
    rows = [
        ("platform", f"{sys.platform} / {platform.platform()}"),
        ("python", sys.version.split()[0]),
        ("stdout encoding", str(sys.stdout.encoding)),
        ("stderr encoding", str(sys.stderr.encoding)),
        ("filesystem encoding", sys.getfilesystemencoding()),
        ("utf8_mode", str(sys.flags.utf8_mode)),
        ("PYTHONUTF8", str(os.environ.get("PYTHONUTF8"))),
        ("PYTHONIOENCODING", str(os.environ.get("PYTHONIOENCODING"))),
        ("cwd", cwd),
        ("tempdir", tmp),
        ("same volume", str(os.path.splitdrive(cwd)[0].lower()
                            == os.path.splitdrive(tmp)[0].lower())),
        ("provider CLIs found",
         ", ".join(b for b in PROVIDER_BINS if shutil.which(b)) or "none"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        # ASCII only, deliberately: this report must survive every code page,
        # including the one whose limits it is reporting.
        line = f"{key.ljust(width)}  {value}"
        encoding = sys.stdout.encoding or "utf-8"
        sys.stdout.write(line.encode(encoding, "backslashreplace")
                             .decode(encoding, "replace") + "\n")

    # Non-fatal, but say it out loud: this combination is the one that breaks
    # relpath, and a silent report would let it pass unnoticed again.
    if os.name == "nt" and os.path.splitdrive(cwd)[0].lower() != \
            os.path.splitdrive(tmp)[0].lower():
        print("::warning title=volumes::workspace and TEMP are on different "
              "volumes; os.path.relpath between them raises ValueError on "
              "Windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
