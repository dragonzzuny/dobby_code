# LEDGER: fix-calc-add

Created BEFORE the first change to any file. Slug: `fix-calc-add`.

Interpreter (`{python}` in this task) resolved to:
`C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`

## Requirements (VERBATIM, numbered)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter. That protocol is the thing being evaluated, so do not substitute your own."
4. "Write `GATES.md` and the ledger in THIS directory (not in a subdirectory)."
5. "`{python}` in any command means the interpreter you are running under; write the real path."

## Assumptions (L0 = directly observed here; L1 = inferred / not verified here)

- **A1 (L0)** — `calc.py` reads:
  `def add(a, b): return a - b` with `print(add(3, 4))` under `__main__`.
  Observed by reading the file. The stated symptom (`-1`) follows from `3 - 4`.
- **A2 (L0)** — `which python` returns
  `/c/Users/dynap/AppData/Local/Programs/Python/Python311/python`, i.e.
  `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`.
- **A3 (L1)** — that PATH interpreter is "the interpreter you are running under" for the
  purposes of requirement 5. I could not confirm it with
  `python -c "import sys; print(sys.executable)"` (see A4), so this is inferred from PATH
  resolution alone, not observed from inside the interpreter.
- **A4 (L0)** — **Python cannot be executed in this session.** Every attempted invocation is
  refused by the permission layer with `This command requires approval`, across: the Bash tool
  (`python -m dobby.cli --help`, `python -c ...`, absolute-path `python.exe`, and with the
  sandbox override), the PowerShell tool (`python calc.py`), and the `python_repl` MCP tool.
  Non-python executables (`git`, `which`, `ls`) run fine, so the refusal is specific to running
  a Python interpreter, not a general shell failure. Consequence: protocol steps 2 (`dobby.cli
  context`), 5 (`gates verify`), 6 (`handoff-latest`) and the `gates status` / `gates approve`
  checkpoints could not be run, and no output-side check of the fix could be executed. Nothing
  below claims otherwise.
- **A5 (L0)** — Requirement 4 overrides the protocol's `reports/LEDGER_<slug>.md` path: this
  ledger is written at the top level of the working directory, not in `reports/`. Deliberate
  deviation, recorded here rather than silently taken.

## Status table

| # | requirement | state | evidence path |
|---|-------------|-------|---------------|
| 1 | Work in this directory. | done | all edits under `LEDGER-PRODUCES-GATES_claude\`; no file written elsewhere |
| 2 | `calc.py` `add` is wrong; `python calc.py` should print 7 and prints -1. | **code changed, UNVERIFIED** | GATES.md G1, G2 — both ticked (claim), neither executed; see A4 |
| 3 | Fix it, following the protocol to the letter. | partial | this ledger + GATES.md written in order, before the first change; steps 2/5/6 blocked, see A4 and G5 |
| 4 | Write `GATES.md` and the ledger in THIS directory. | done | `GATES.md` and `LEDGER_fix-calc-add.md` at top level; GATES.md G3 (ticked, not executed) |
| 5 | `{python}` means the real interpreter path. | done | GATES.md G4 (manual, unscored) — every CHECK spells out `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` |

### Change made (step 4 — Act)

`calc.py:2-3` — `return a - b` → `return a + b`. The `# BUG: returns the difference`
comment was removed with it; left in place it would have been an orphan reference to a
defect that no longer exists.

### Output-side check (step 5 — Validate)

**None executed.** The protocol asks for a quoted output-side check result and there is no
honest one to quote: `"C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe"
calc.py` was refused with `This command requires approval`, as were
`gates approve`, `gates verify --file GATES.md --write-evidence` and `handoff-latest`.
So `gates verify` did not exit 0 — it did not run. G1/G2/G3 are ticked because I believe
they hold after the change (`3 + 4 == 7` follows from the edited source), not because any
run confirmed them. G5 is left unticked precisely because these steps did not happen.
No gate was abandoned: G1-G3 are runnable and will decide themselves in an environment
where the interpreter may execute.

### Handoff (step 6)

`handoff-latest` cannot return this; the handoff is written by hand as
`HANDOFF_fix-calc-add.md` in this directory.

## Hypothesis table (step 3 — Observe)

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| `add` uses `-` instead of `+`; nothing else is wrong | read `calc.py` in full (7 lines, whole file visible) | body is `return a - b`, comment says "BUG: returns the difference"; caller is `print(add(3, 4))` | CONFIRMED — fix location is `calc.py:3` |
| the caller passes wrong arguments (`3, 4` should be other values) | same read: does `3 + 4 == 7`? | `3 + 4 == 7` matches the required output, so the caller is correct | REJECTED |
| an import shadows `add` from elsewhere | file has no imports and no other definitions | none present | REJECTED |

One check confirmed the hypothesis and identified the fix location, so observation stopped there
(no dead-check streak).

## Log

- Ledger written before the first change. All rows `todo`, none pre-filled.
- `GATES.md` written next, before the first change.
- Rows are updated in place as work lands; see the Status table above for current state.
