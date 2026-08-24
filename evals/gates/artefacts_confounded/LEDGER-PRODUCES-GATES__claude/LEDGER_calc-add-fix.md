# LEDGER: calc-add-fix

Protocol: `ledgered-task`. Written BEFORE the first change to any file.

Interpreter (`{python}` resolved to its real path, per requirement 5):
`C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`
(established by `where.exe python` → first entry; `python --version` → `Python 3.11.9`)

Location note: the protocol says the ledger goes at `reports/LEDGER_<slug>.md`.
The task instruction says to write the ledger in THIS directory, "not in a
subdirectory". The task instruction is the more specific and later statement,
so the ledger lives at the repository root. This deviation from the protocol
text is deliberate and recorded here rather than silently taken.

## Requirements (VERBATIM, numbered)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py`
   should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter."
4. "Write `GATES.md` and the ledger in THIS directory (not in a subdirectory)."
5. "`{python}` in any command means the interpreter you are running under;
   write the real path."

## Assumptions

| # | assumption | ladder | basis |
|---|---|---|---|
| A1 | The interpreter meant by `{python}` is `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` | L0 | Directly observed: `where.exe python` resolved it first on PATH and `python --version` printed 3.11.9. |
| A2 | The intended behaviour of `add(a, b)` is arithmetic addition, so `add(3, 4) == 7` | L0 | Stated in the requirement ("should print 7") and the in-file comment "BUG: returns the difference"; `calc.py:7` calls `add(3, 4)`. |
| A3 | The `-1` in the requirement is the current output of `python calc.py` | L0 | Read from source: `return a - b` with `add(3, 4)` gives `3 - 4 == -1`. Confirmed by execution in step 3. |
| A4 | Nothing else in the repository imports `calc.add` and depends on subtraction | L0 | `calc.py` is the only file in the working tree apart from `.omc/state/` runtime artifacts. |
| A5 | The scope is the `add` defect only; no test suite, packaging, or refactor is requested | L1 | Inferred from "Fix it"; not stated. No new source files are created beyond the ledger, `GATES.md`, and gate evidence. |
| A6 | `dobby` is importable by the interpreter in A1, so the protocol's `dobby.cli` checkpoints can run | L1 | Not yet verified at ledger-writing time; the CLI is invoked in steps 1, 2 and 5 and the real result is recorded below, pass or fail. |

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | All writes are to this directory's root: `calc.py`, `GATES.md`, `LEDGER_calc-add-fix.md`, `HANDOFF_calc-add-fix.md`. No subdirectory created. Gates G3, G4 assert this but are ungraded. |
| 2 | `calc.py`'s `add` is wrong: `python calc.py` should print 7 and prints -1. | changed, NOT verified | `calc.py:1-2` now reads `def add(a, b): / return a + b` (read back from disk after the edit). Gates G1, G2 are ticked as my claim but were never run — see Blockers. No execution evidence that the program prints 7 exists. |
| 3 | Fix it, following the protocol to the letter. | partial | Steps 1, 3, 4, 6 done; step 2 (Brief) never ran and step 5 (Validate) never ran, both blocked. G6 is manual and unscored. Detail in "Protocol adherence" below. |
| 4 | Write `GATES.md` and the ledger in THIS directory (not in a subdirectory). | done | `GATES.md` and `LEDGER_calc-add-fix.md` both at this directory's root; verified by the `Write` tool's absolute target paths and by the directory listing. Gates G3, G4 assert it but are ungraded. |
| 5 | `{python}` in any command means the interpreter being run under; write the real path. | done | Every `CHECK:` line in `GATES.md` and every command in this ledger and the handoff spells `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`. Gate G5 asserts it but is ungraded. |

Note on states: no row for a runtime claim is marked `done`, because the
protocol's step-5 output-side check could not be executed. "changed, NOT
verified" is used rather than `done` so the distinction is not lost.

## Gate map

Producing requirements and their gates in `GATES.md`:

| requirement | gate(s) |
|---|---|
| 2 | G1 (program output), G2 (function behaviour on several inputs) |
| 3 | G6 (manual, no `CHECK:` — protocol adherence is not decidable by a command) |
| 4 | G3 (ledger at root), G4 (`GATES.md` at root) |
| 5 | G5 (no `{python}` placeholder survives in any `CHECK:` line) |
| 1 | covered by G3 + G4, which assert the artifacts are at this directory's root |

## Observation (step 3)

Read-only. Execution was refused, so every discriminating check below is static
source reading rather than a run.

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| H1 `add` computes the difference instead of the sum | Read `calc.py:1-3` | `return a - b`, with the author's own comment `# BUG: returns the difference`; `add(3, 4)` = `3 - 4` = `-1`, which is exactly the reported wrong output | CONFIRMED |
| H2 the caller passes the wrong arguments | Read `calc.py:7` | `print(add(3, 4))`; `3 + 4 == 7`, the expected output, so the call site is correct | REFUTED |
| H3 a different `add` shadows this one at runtime | `Glob **/*.py`; `Grep 'def add\|import calc\|from calc'` | `calc.py` is the only Python file; exactly one `def add`; no other module imports it | REFUTED |

Stopped after one confirmation with the fix location identified
(`calc.py:1-3`). No dead-check streak, so no re-derivation was needed.

## Execution (step 4)

Single increment, project left runnable:

    - def add(a, b):
    -     # BUG: returns the difference
    -     return a - b
    + def add(a, b):
    +     return a + b

The `# BUG: returns the difference` comment was removed with the defect; leaving
it would have been a stale comment asserting something now false.

## Validation (step 5) — BLOCKED

The output-side check the protocol requires is an execution of `calc.py`, and
execution is refused in this session. What could be obtained instead, quoted in
full, is the post-edit read-back of the file:

    def add(a, b):
        return a + b


    if __name__ == "__main__":
        print(add(3, 4))

This is a static check. It shows the source is now correct; it is NOT evidence
that the program printed 7, and it is not a substitute for the gate run.

`gates verify --file GATES.md --write-evidence` never executed, so it produced
neither exit 0 nor exit 1. There is no summary to quote. No gate was abandoned:
`ABANDON:` marks a gate that turned out impossible, and G1-G5 are well-formed
commands that would run for a caller with execute permission — abandoning them
would delete the check rather than report the blockage.

## Blockers

Python cannot be executed here. Refused with "This command requires approval",
in both the Bash and PowerShell tools, with both the bare and the absolute
interpreter path, and the session is non-interactive so the approval cannot be
granted. Affected: `gates status`, `gates approve`, `gates verify`, `context`,
`handoff-latest`, and `python calc.py` itself. Only `python --version`
succeeded. Assumption A6 therefore remains L1 — `dobby` was never even shown to
be importable.

## Protocol adherence (requirement 3, gate G6 — manual, unscored)

| step | state |
|---|---|
| 1 Restate + gates first | done — ledger and `GATES.md` both written before the first change, all rows `todo`, all boxes unticked. Its `✓ gates status` checkpoint could not run. |
| 2 Brief (`context`) | NOT DONE — blocked. No fired policy or skill body was read beyond the two supplied in the task prompt. |
| 3 Observe | done, but static only; no runtime evidence. |
| 4 Act | done — smallest increment, ledger updated. |
| 5 Validate | NOT DONE — blocked. Static read-back only. |
| 6 Handoff | done — `HANDOFF_calc-add-fix.md`. Its `✓ handoff-latest` checkpoint could not run. |
| 7 Report | done — outcome-first, this ledger, blockers named, nothing softened. |

Self-inflicted deviation, recorded rather than hidden: the first version of
`GATES.md` had G1-G5 written as `- [x]` before any work existed. Under the
runnable-gates contract a ticked box is a claim about work already done, so
that was a pre-tick and wrong. It was corrected to `- [ ]` before `calc.py` was
edited, and the boxes were re-ticked only after the fix landed.

## Trajectory notes

- Interpreter resolved by `where.exe python`, first PATH entry, confirmed
  `Python 3.11.9`.
- `.omc/state/sessions/...` contains only a hook throttle file; no prior ledger
  or handoff existed, so this was a fresh start, not a resume.
