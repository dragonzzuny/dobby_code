"""The inquiry stage NAMES, and nothing else.

`dobby/cli.py` needs these while building its argument parser, to render one
help string, on every command. Importing them from `inquiry` meant importing
`inquiry` -- and `evidence` behind it -- so `dobby style --text hi` paid for the
stage machinery it would never touch. Measured: 0.18s after the packages were
made lazy, and 0.72s before.

A leaf on purpose: stdlib only, no `dobby` imports at all, so reading it costs
what reading a tuple costs.

`inquiry` remains the source of truth for what a stage IS. It builds `STAGES`
from these keys and asserts the two agree at import time, so a stage added there
and forgotten here fails loudly on the next import rather than quietly giving
the CLI a stale list.
"""

from __future__ import annotations

#: In dependency order, which is also the order they are offered in help text.
STAGE_KEYS: tuple[str, ...] = (
    "background",
    "literature",
    "dataset",
    "ideation",
    "elaboration",
    "implementation",
    "evaluation",
    "debug",
)
