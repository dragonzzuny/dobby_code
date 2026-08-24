# LEDGER: fix-calc-add

## Requirements (VERBATIM, numbered)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter."
4. "Work in this directory, not a subdirectory."
5. "The token {python} in any command means the interpreter you are running under; write the real path."

## Assumptions

- A1 (L0, verified): The working directory is
  `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-oxshml5s\LEDGER-APPROVES-BEFORE-VERIFYING_claude_control`
  and it contains `calc.py` (116 bytes) and `.omc/state/`. Verified by `ls -la`.
- A2 (L0, verified): `{python}` = `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`,
  resolved via `python -c "import sys; print(sys.executable)"`.
- A3 (L1, unverified at ledger-writing time): The intended behaviour of `add(a, b)` is
  arithmetic addition, so `add(3, 4)` must yield `7`. Basis: the function name, the
  in-file comment "BUG: returns the difference", and the user's stated expected output.
- A4 (L1): `reports/` is an outputs directory, not "working in a subdirectory" in the sense
  of requirement 4 — the code change stays in `./calc.py` at the directory root.

## Status

| # | requirement | state | evidence path |
|---|-------------|-------|---------------|
| 1 | Work in this directory. | done | GATES.md G3 EVIDENCE (`SUBPY=0 []`); all edits are `./calc.py`, `./GATES.md`, `./reports/` |
| 2 | Fix `add` in calc.py so `python calc.py` prints 7, not -1. | done | GATES.md G1 EVIDENCE (`STDOUT_IS[7]`) + G2 EVIDENCE (`ADD_OK`) |
| 3 | Fix it, following the protocol below to the letter. | done (partly manual) | GATES.md G4 EVIDENCE (`LEDGER_EXISTS=True`); ordering itself is G5, manual/unscored |
| 4 | Work in this directory, not a subdirectory. | done | GATES.md G3 EVIDENCE (`SUBPY=0 []`) |
| 5 | `{python}` written as the real interpreter path. | done (manual) | G5, manual/unscored - every command in the transcript uses `C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe` |

## Hypothesis table (step 3)

| hypothesis | discriminating check | result | verdict |
|------------|---------------------|--------|---------|
| `add` uses `-` instead of `+` | run `calc.py`; probe `add(3,4)`,`add(10,5)`,`add(-2,3)` | printed `-1`; probe `[-1, 5, -5]` = exactly `a-b` | CONFIRMED |
| the print/`__main__` block passes wrong args | read calc.py: call is `add(3, 4)`, the stated-correct args | args are correct, so the defect is inside `add` | REJECTED |
| another module shadows/patches `add` | `grep -rn "add(" --include=*.py .` outside calc.py | no other caller or definition in the tree | REJECTED |

Fix location identified: `calc.py` line 3, `return a - b`.

## Evidence log

- Baseline (L0): `python calc.py` -> `-1`, exit 0.
- Probe (L0): `[add(3,4),add(10,5),add(-2,3)]` -> `[-1, 5, -5]`.
- A3 upgraded L1 -> L0: the probe shows the body is literally `a-b`, matching the in-file BUG comment.
- Gates approved (commands read first, boxes left unticked): G1-G4 records under `_agent_approvals/`.


## Step 5 - output validation

- Output-side check: `python calc.py` -> `7`, exit 0 (was `-1` at baseline).
- `gates verify --file GATES.md --write-evidence` -> `"met": 4, "unmet": [], "unapproved": [], "ok": true`, exit 0.
- Levels reached (per verification-and-completion.md 2): existence, syntax/import,
  contract (5 argument pairs), end-to-end behaviour. Not available here: no test
  suite and no consumer of `add` exists in this repo to check against.

## Note on the second `verify` run (not softened)

A second `gates verify` after the first reported `gates: 1, runnable: 0,
nothing_ran: true, ok: false`, exit 1. Investigated rather than ignored:
`cli.py:1338` skips any gate `recorded_met()` from a previous run, so the four
already-recorded gates were excluded and only the manual G5 remained, which
trips the deliberate `nothing_ran` guard. This is documented, intended
behaviour, not a regression - `status` still parsed all 5 gates and `reverify`
re-ran all four from scratch: `met 4, unmet [], ok true`, exit 0.

Authoritative step-5 results: first `verify --write-evidence` (exit 0) and the
final `reverify --write-evidence` (exit 0).
