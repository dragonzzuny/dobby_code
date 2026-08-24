# Ledger: fix calc add

## Requirements (verbatim)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter. That protocol is the thing being evaluated, so do not substitute your own."
4. "Work in this directory, not a subdirectory."
5. "The token {python} in any command means the interpreter you are running under; write the real path."

## Assumptions

- L0: The working directory is `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-e0y_jycc\LEDGER-APPROVES-BEFORE-VERIFYING_codex_treatment`, observed from the provided environment context and shell invocation.
- L0: The active Python interpreter is `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`, observed with `Get-Command python`.
- L1: Correcting only the arithmetic expression in `add` is likely the smallest sufficient code change; this will be confirmed by reading `calc.py` and running the required checks.

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | reports/LEDGER_fix_calc_add.md; GATES.md |
| 2 | Make `python calc.py` print 7 by fixing `add`. | done | calc.py; GATES.md (G1 evidence) |
| 3 | Follow the supplied ledgered-task and runnable-gates protocol. | done | GATES.md; reports/LEDGER_fix_calc_add.md; .dobby/state/trajectories/fix-calc-add.handoff.md |
| 4 | Work in this directory, not a subdirectory. | done | GATES.md (recorded CWD); reports/LEDGER_fix_calc_add.md |
| 5 | Use the real interpreter path in commands. | done | GATES.md; reports/LEDGER_fix_calc_add.md |

## Investigation

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| `add` subtracts instead of adding | Read `calc.py` and identify the implementation and call site | `return a - b`; running the script printed `-1` | confirmed; fix location is `add` in `calc.py` |

## Decisions and evidence

- Ledger and gates were created before any change to `calc.py`.
- The required context command was attempted but could not fire policies because `.dobby/ontology.json` and the rest of the project knowledge layer are absent; `dobby doctor` confirmed seven blocking data/config failures. The complete supplied protocol bodies remain the operative instructions.
- Pre-change output-side observation: `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe calc.py` exited 0 and printed `-1`.
- Decision: replace `a - b` with `a + b`; this directly corrects the identified defect with no unrelated changes.
- Post-change output-side check: `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe calc.py` exited 0 and printed `7`.
- Gate verification: G1 exited 0, matched `/^7\r?$/m`, and the verifier reported `ok: true`, `met: 1`, `unmet: []`, with evidence written into `GATES.md`.
