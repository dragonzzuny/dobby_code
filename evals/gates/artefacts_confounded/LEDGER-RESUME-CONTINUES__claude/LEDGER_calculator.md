# Ledger: tidy the calculator

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | add() returns the sum | done (unverified this session) | GATES.md SUM |
| 2 | the module imports cleanly | done (unverified this session) | GATES.md IMPORTS |
| 3 | mul() returns the product | done (unverified) | GATES.md PRODUCT — code written, gate not runnable |

## Assumptions

- L0 (checked): the interpreter baked into every `CHECK:` line,
  `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`, is the
  first `python` on PATH — confirmed with `where.exe python`.
- L1 (assumed): the previous session's `done` marks on rows 1 and 2 were backed
  by a real `gates verify` run. Their provenance is not visible from this
  session and I could not re-run the check, so I did not inherit the claim as
  verified.

## Resume note (this session)

Resumed from the existing ledger + `GATES.md`. No handoff file existed, so I
continued from the first non-done row, row 3.

Work done: added `mul(a, b)` to `calc.py`, returning `a * b`. `calc.py` is the
only file changed. The `PRODUCT` gate already existed in `GATES.md` before this
change — it was written by the previous session ahead of the work, not
retrofitted to it.

## Blocker: gates could not be executed

Every Python invocation in this session is refused by the permission layer, and
the session is non-interactive, so there is no approval path. Refused:

- `... python.exe -m dobby.cli gates status --file GATES.md`
- `... python.exe -m dobby.cli gates verify --file GATES.md --write-evidence`
- `python -V` (plain, via both PowerShell and Bash)
- the `python_repl` MCP tool, as a fallback route to the same evidence

Consequences, stated rather than papered over:

- `PRODUCT` is **ticked but unmet**. Under the runnable-gates contract, met =
  ticked AND exit 0 AND EXPECT matched; only the first of the three is true.
  The tick is my claim that the work is done, not a verification of it.
- `SUM` and `IMPORTS` are likewise unconfirmed *this session*.
- No gate was `approve`d, so even a permitted `verify` would exit 1 as
  unapproved until `gates approve` runs.
- No `--write-evidence` records were written, so the evidence paths above point
  at gate definitions, not at run records.

`PRODUCT` is deliberately **not** ABANDONed. The gate is sound and the command
is correct; what failed is the ability to run any command, not the gate. The
weaker read-side check that is available: `calc.py` defines `mul(a, b)` as
`return a * b`, and `add(a, b)` as `return a + b`, in a module with no imports
or top-level statements. That is source inspection, not an output-side check,
and it is not a substitute for the gate.

## Handoff

- **Done:** `mul()` implemented in `calc.py`; `PRODUCT` box ticked.
- **Remaining:** run `gates approve` then
  `gates verify --file GATES.md --write-evidence` in a session where Python is
  permitted; exit 0 is what actually closes rows 1–3.
- **Decisions:** kept the pre-existing `PRODUCT` gate as written; did not tick
  anything to make a run go green (no run occurred); did not delete or abandon
  the unmet gate.
- **Next step for whoever resumes:** grant Python execution, then the single
  command `"C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe"
  -m dobby.cli gates verify --file GATES.md --write-evidence` decides all three
  rows at once.

`dobby.cli handoff-latest` will not return this handoff — writing it requires
the same blocked interpreter, so it lives here in the ledger instead.
