r"""A failure detail with the parts that differ between runs removed.

Lifted out of `runtime.flywheel` when `core.friction` needed the same
grouping. `core` does not import `runtime` anywhere and must not start: the
alternative was a second copy of the noise table, and two copies of a
normaliser drift into two different answers to "is this the same failure".

The table is the interesting part, and one entry in it is a scar. A digit
pattern written with word boundaries refuses `120s` -- there is no boundary
before `s` -- and takes only the `0` out of `0.03s`, so two runs of one failure
that merely took different amounts of time got different signatures and were
counted as unrelated. That is exactly the miscount this grouping exists to
prevent, so the digit pattern carries none.
"""

from __future__ import annotations

import re

_NOISE = (
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"/[^\s'\"]{2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<hash>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:]+"), "<time>"),
    (re.compile(r"\d+(?:\.\d+)*"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def signature(detail: str) -> str:
    """`detail` with paths, hashes, timestamps and numbers replaced.

    Bounded at 200 characters so one enormous detail cannot dominate a group.
    """
    text = detail or ""
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()[:200]
