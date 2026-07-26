"""Prove that non-ASCII output survives a non-UTF-8 default locale.

This was an inline `python -c "... print('em—dash 한국어 OK')"` in the workflow.
Two problems with that, both real rather than stylistic:

  * The literal characters sat inside a double-quoted shell string, and the
    Windows jobs run on pwsh while the others run on bash. How each one hands
    those bytes to Python is not the same, so a failure could have come from the
    shell rather than from the code being tested.
  * A single print() either works or raises, with nothing to say about WHICH
    character failed. The two code pages involved are complementary - measured:
    cp949 encodes Korean but not an em dash, cp1252 an em dash but not Korean -
    so knowing which one broke is the whole diagnosis.

Each sample is therefore checked against the raw console encoding first, and then
the whole set is printed through force_utf8_io(), which is the function actually
under test.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dobby.core.platform import force_utf8_io  # noqa: E402

SAMPLES = [
    ("em dash", "em—dash"),
    ("korean", "한국어"),
    ("cjk mixed", "漢字 / かな"),
    ("accented latin", "résumé naïve"),
    ("box drawing", "─│└"),
]


def main() -> int:
    raw_encoding = sys.stdout.encoding or "ascii"
    # Report before reconfiguring: this is the state the rest of the suite runs
    # in, and it is the variable that differs between machines.
    print(f"console encoding before force_utf8_io: {raw_encoding}")
    for label, sample in SAMPLES:
        try:
            sample.encode(raw_encoding)
            verdict = "representable"
        except UnicodeEncodeError:
            verdict = "NOT representable in this code page"
        print(f"  {label:16} {verdict}")

    force_utf8_io()
    after = sys.stdout.encoding
    print(f"console encoding after force_utf8_io: {after}")
    if (after or "").lower().replace("-", "") not in ("utf8", "utf8mb4"):
        print("::error title=unicode round-trip::force_utf8_io() left stdout at "
              f"{after!r}; non-ASCII output is not safe", flush=True)
        return 1

    # The actual round trip. If this raises, the pinning did not take effect and
    # the exception is the finding - no try/except, because swallowing it here
    # would report success for a broken guarantee.
    for label, sample in SAMPLES:
        print(f"  {label:16} {sample}")
    print("em—dash 한국어 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
