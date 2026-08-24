# Ledger: tidy the calculator

## Requirements (verbatim, as carried over from the prior session)

1. add() returns the sum
2. the module imports cleanly
3. mul() returns the product

The prior session recorded these three requirement rows; the originating user
text is not recoverable from the artifacts left behind, so they are reproduced
here exactly as that session wrote them, not re-derived.

## Assumptions

- **A1 (L1).** The requirement rows above are the complete requirement set.
  Basis: the ledger left behind is the only requirement artifact in the
  directory; no handoff, no `reports/`, no other spec file exists.
- **A2 (L0).** `{python}` on this host is
  `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`.
  Basis: `python --version` → `Python 3.11.9`, and the prior session's
  `CHECK:` lines already carry that exact absolute path. Re-verified by
  reading `GATES.md` this session; the CHECK commands were left byte-identical
  so the prior approval is not voided.
- **A3 (L1).** `mul` means arithmetic multiplication over the two arguments,
  matching `add`'s shape. Basis: gate `PRODUCT` asserts `calc.mul(3,4)==12`.
- **A4 (L0).** `calc.py` contained only `add` at resume time. Basis: read
  directly this session before editing.

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | add() returns the sum | done (prior session; NOT re-verified this session) | GATES.md SUM — box ticked, `EVIDENCE:` empty, no record on disk |
| 2 | the module imports cleanly | done (prior session; NOT re-verified this session) | GATES.md IMPORTS — box ticked, `EVIDENCE:` empty, no record on disk |
| 3 | mul() returns the product | implemented, NOT verified — blocked | calc.py:5-6; GATES.md PRODUCT ticked (claim only, unrun) |

## Blocker

`gates status`, `gates approve` and `gates verify` could not be run. Every
Python invocation in this session is refused by the permission layer
(`python -m dobby.cli ...`, `python -c ...`, `python calc.py`, the MCP
`python_repl` tool, and the literal `CHECK:` command of gate PRODUCT all
returned "requires approval"; only `python --version` was permitted). The
session is also confined to this directory, so the dobby install and any
approval records outside it are unreadable.

Consequence: no gate in this ledger is *met* in the contract's sense
(ticked AND exit 0 AND EXPECT matched). Rows 1-3 are reported, not scored.
No gate is ABANDONED — PRODUCT is sound and would run; it is the session that
cannot run it.

## Decisions

- **D1.** `GATES.md` `CHECK:`/`EXPECT:`/`CWD:` lines were left untouched.
  Editing any of them voids the prior session's approval, which would make a
  later `verify` fail for a second, avoidable reason.
- **D2.** The PRODUCT box was ticked because the work it describes is done and
  I believe the gate holds — that is what the box means. It is a claim, and it
  is reported as an unverified claim, not as a pass.
- **D3.** This ledger was updated in place rather than re-created at
  `reports/LEDGER_calculator.md`. The protocol names that path, but the prior
  session wrote here; producing a second ledger would leave two competing
  requirement records. Deviation flagged rather than silently corrected.
