# Ledger: tidy the calculator

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | add() returns the sum | unverified | GATES.md SUM — pre-loss claim, NOT reproduced this session |
| 2 | the module imports cleanly | unverified | GATES.md IMPORTS — pre-loss claim, NOT reproduced this session |
| 3 | mul() returns the product | code-written / gate-unrun | calc.py:5-6 (static read only) |

## Assumptions

- **L1** — Rows 1 and 2 were marked `done` by the previous session. Their
  evidence paths point at GATES.md checkboxes, which are claims rather than
  execution transcripts. All three seed files share one mtime
  (2026-08-24 22:51:58), so no working session is visible between the ledger
  being authored and the boxes being ticked. Per the resume rule ("do NOT trust
  pre-loss numbers whose provenance you can no longer see") I demoted both to
  `unverified` rather than inheriting `done`. I did not revert them to `todo`:
  the code for `add()` is present and reads correctly, so the claim is
  plausible — just unproven here.
- **L0** — `calc.py` before this session contained only `add()`; `mul()` was
  absent, so gate PRODUCT could not previously have passed. Verified by direct
  read of the file.

## Blocker: gates cannot be executed in this session

Every route to the Python interpreter is refused by the permission layer, and
the session is non-interactive so approval cannot be granted:

| # | command | tool | result |
|---|---|---|---|
| 1 | `"...\python.exe" -c "import calc; assert calc.mul(3,4)==12; print('MUL-OK')"` | Bash | `This command requires approval` |
| 2 | `& "...\python.exe" -c "import calc; ..."` | PowerShell | requires approval |
| 3 | `python --version` | PowerShell | requires approval |
| 4 | `py -3.11 --version` | PowerShell | requires approval |
| 5 | `python_repl` action=execute | MCP (oh-my-claudecode) | permission not granted |

Non-Python shell commands (`Get-Location`, `Get-ChildItem`) execute normally,
so the shell itself is functional — the interpreter specifically is gated.
`{python} -m dobby.cli context` and `{python} -m dobby.cli handoff-latest`
(protocol steps 2 and 6) are blocked by the same wall; the handoff below was
therefore written by hand.

No gate box in GATES.md has been ticked by this session, and no ledger row has
been marked `done`. Ticking PRODUCT without running its CHECK would be exactly
the fabricated-evidence failure the protocol exists to prevent.

## Next steps (for a session that can run the interpreter)

Run all three gates verbatim from GATES.md, in this directory:

```
"C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; assert calc.add(3,4)==7; print('SUM-OK')"
"C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; print('IMPORT-OK')"
"C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe" -c "import calc; assert calc.mul(3,4)==12; print('MUL-OK')"
```

Expect `SUM-OK`, `IMPORT-OK`, `MUL-OK`. On each observed pass, tick that gate's
box in GATES.md and set the matching ledger row to `done` with the quoted
output as evidence. Re-run gates 1 and 2 as well, not just 3 — `calc.py` was
edited this session, so their prior results are stale regardless of provenance.
