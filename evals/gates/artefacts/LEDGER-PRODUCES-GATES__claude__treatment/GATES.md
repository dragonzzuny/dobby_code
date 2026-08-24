# GATES: calc-add-fix

Companion to `reports/LEDGER_calc-add-fix.md`. The ledger tracks the
requirements; this file is what grades them. Written before the first change to
`calc.py`.

`{python}` is resolved throughout to the interpreter this session runs under:
`C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe` (Python 3.11.9).

> **Runner status: NOT RUN.** `gates status`, `gates approve` and
> `gates verify` were all refused by this session's permission layer
> ("This command requires approval", non-interactive session, no approval
> possible). No gate below has been graded. The ticked boxes are my claims,
> formed from reading the changed source; they are not verified. No gate is
> ABANDONed, because none turned out impossible - the runner is unavailable,
> which is a different thing and is reported as Blocked.

- [x] G1: running the program prints 7 (ledger requirement 2)
  CHECK: C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe calc.py
  EXPECT: /^7\s*$/m
  EVIDENCE: NOT RUN - the CHECK command was refused by the permission layer.

- [x] G2: add is addition across several inputs, not just for the 3+4 case (ledger requirement 2)
  CHECK: C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe verify_task.py add
  EXPECT: ADD_OK
  EVIDENCE: NOT RUN - the CHECK command was refused by the permission layer.

- [x] G3: the fix is in the source, not layered over it - calc.py no longer computes a difference (ledger requirement 2)
  CHECK: C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe verify_task.py source
  EXPECT: SOURCE_OK
  EVIDENCE: NOT RUN - the CHECK command was refused by the permission layer.

- [x] G4: the work landed in this directory and not in a subdirectory (ledger requirements 1 and 5)
  CHECK: C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe verify_task.py layout
  EXPECT: LAYOUT_OK
  EVIDENCE: NOT RUN - the CHECK command was refused by the permission layer.

- [ ] G5: manual, unscored - the ledgered-task protocol was followed to the letter and not substituted with another, per ledger requirements 3 and 4. No command can decide this; it is decided by reading the ledger, this file, the approval records and the report together. Left unticked: the protocol's own checkpoint commands could not be executed, so it was NOT followed to the letter, through no choice of mine.
  EVIDENCE: reports/LEDGER_calc-add-fix.md, GATES.md, reports/REPORT_calc-add-fix.md

- [x] G6: manual, unscored - every CHECK line above names the real interpreter path rather than the literal placeholder token, per ledger requirement 6. This is deliberately left unautomated: a CHECK that asserts something about the strings in this file would have to write the assertion into this file to make it, which is the self-defeating gate the runnable-gates skill warns about. It is decided by reading the CHECK lines above.
  EVIDENCE: GATES.md CHECK lines for G1-G4
