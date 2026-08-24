# LEDGER: calc-add

Protocol: `ledgered-task`. Companion gates: `GATES.md` (same directory).

Interpreter resolved for `{python}`:
`C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe`
(resolved L0 from `where.exe python`, first hit on PATH.)

Location note: the protocol says `reports/LEDGER_<slug>.md`, but the task text
explicitly overrides it — "Write `GATES.md` and the ledger in THIS directory
(not in a subdirectory)." The explicit instruction wins; no `reports/` dir is
created.

## 1. Requirements (VERBATIM, numbered)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py`
   should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter."
4. "That protocol is the thing being evaluated, so do not substitute your own."
5. "Write `GATES.md` and the ledger in THIS directory (not in a subdirectory)."
6. "`{python}` in any command means the interpreter you are running under;
   write the real path."

## 2. Assumptions

| # | assumption | rung | basis |
|---|---|---|---|
| A1 | `{python}` = `C:/Users/dynap/AppData/Local/Programs/Python/Python311/python.exe` | L0 | `where.exe python` returned it as the first PATH entry (observed). |
| A2 | The intended `add` semantics is arithmetic addition (`a + b`), not some other operation that happens to yield 7 for (3, 4). | L1 | One inference from the function name `add`, the in-file comment "BUG: returns the difference", and the stated expected output 7 for `add(3, 4)`. |
| A3 | The `__main__` block (`print(add(3, 4))`) is correct as written and must not change; only `add` is "wrong". | L1 | Requirement 2 names the `add` function as the wrong part. |
| A4 | The L0/L1 "ladder" document referenced by the protocol is not present on this host; rungs above are used with the plain reading L0 = directly observed, L1 = one inference step. | L0 | Working directory contains only `calc.py` and an `.omc/` state file; no protocol/ladder docs are readable from here. |
| A5 | `dobby` is expected to be importable by A1's interpreter. NOT verified — see the blocker in section 5. | L1 | The task text hands out `{python} -m dobby.cli ...` commands as if runnable. |

## 3. Status table

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | All writes are to `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-a2x6a6yq\LEDGER-PRODUCES-GATES_claude`; no file created outside it. |
| 2 | Fix `add` so `python calc.py` prints 7. | done-unverified | `calc.py`: old line 3 `return a - b` -> `return a + b`, now at `calc.py:2`; the stale `# BUG: returns the difference` comment removed with it. Gates G1/G2 written but NOT RUN — execution blocked (section 5). |
| 3 | Follow the protocol to the letter. | partial | Steps 1, 3, 4, 6, 7 performed. Steps 2 and 5 blocked: their commands could not be executed. |
| 4 | Do not substitute my own protocol. | done | No substitution; blocked steps are reported as blocked, not replaced. |
| 5 | `GATES.md` and ledger in THIS directory. | done | `./GATES.md`, `./LEDGER_calc-add.md`. Gate G3. |
| 6 | Write the real interpreter path for `{python}`. | done | Real path written in `GATES.md` CHECK lines and above. Gate G4. |

State legend: `todo` -> `done` only with evidence a script could confirm.
`done-unverified` = the change is made and I believe it correct, but the
output-side check could not be executed here.

## 4. Observation (step 3) — hypothesis table

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| H1: `add` computes `a - b` instead of `a + b`; `3 - 4 = -1` matches the reported output exactly. | Read `calc.py`. | Line 2: `# BUG: returns the difference`; line 3: `return a - b`; line 7: `print(add(3, 4))`. | CONFIRMED. Fix location identified: `calc.py:3`. |
| H2: the caller passes the wrong arguments. | Same read. | `print(add(3, 4))` — 3 + 4 = 7, correct as-is. | REJECTED. |
| H3: a shadowing `add` elsewhere in the package. | `Glob **/*` over the working directory. | Only `calc.py` and `.omc/state/.../pre-tool-advisory-throttle.json` exist. | REJECTED — no other source file. |

Stopped at one confirmed hypothesis with the fix location identified, per step 3.
No dead-check streak; the anti-Infinite-Investigation rule did not fire.

## 5. Blocker (affects protocol steps 2 and 5, and gate execution)

No Python process can be started in this session. Every attempt was refused by
the harness permission layer before execution, and the session is
non-interactive so approval cannot be granted:

| attempted command | tool | result |
|---|---|---|
| `python -c "import sys; print(sys.executable)"` | Bash | "This command requires approval" |
| `python -V` / `python --version` | PowerShell | "requires approval" |
| `python calc.py` | PowerShell | "requires approval" |
| `python -m dobby.cli --help` | PowerShell / Bash | "requires approval" |
| `C:\...\Python311\python.exe -V` | PowerShell | "requires approval" |
| `py -V` | PowerShell | "requires approval" |
| `python_repl` (MCP) | MCP | "Claude requested permissions ... but you haven't granted it yet" |

Consequence: `dobby.cli context`, `gates status`, `gates approve`,
`gates verify --write-evidence` and `handoff-latest` were all unrunnable.
Their ✓ checks are therefore UNSATISFIED, not passed. Nothing in this ledger
or in `GATES.md` claims an exit code that was not observed.

Note on gate boxes: per the `runnable-gates` contract, the checkbox is my
claim that the gate now holds, and the command is the independent check on
that claim. G1-G4 are ticked because I did the work and believe they hold.
They remain UNVERIFIED until `gates verify` actually runs.

## 6. Handoff

See `HANDOFF_calc-add.md` in this directory. `{python} -m dobby.cli
handoff-latest` could not be run to register it (same blocker).
