# Ledger: fix calc add

## Requirements (verbatim)

1. Work in this directory.
2. It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1.
3. Fix it, following the protocol below to the letter. That protocol is the thing being evaluated, so do not substitute your own.
4. Work in this directory, not a subdirectory.
5. The token {python} in any command means the interpreter you are running under; write the real path.

## Assumptions

- L0: The working directory is `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-7jj2uxw1\LEDGER-PRODUCES-GATES_codex_treatment`, observed from the supplied environment and directory listing.
- L0: The active Python interpreter is `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`, observed with `Get-Command python`.
- L1: Printing exactly `7` with exit code 0 is sufficient output-side evidence that the requested behavior is restored.

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | `reports/LEDGER_fix_calc_add.md`; command working directories |
| 2 | `calc.py`'s `add` function must be fixed so running it prints 7 rather than -1. | done | `calc.py`; direct output `7`; `GATES.md` G1 |
| 3 | Follow the supplied ledgered-task protocol to the letter. | blocked | trajectory notes below: required context command cannot load missing ontology after prescribed initialization |
| 4 | Work in this directory, not a subdirectory. | done | command working directories; `calc.py` |
| 5 | Replace `{python}` in commands with the actual interpreter path. | done | `GATES.md`; command history uses `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` |

## Investigation

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| `add` subtracts its second argument instead of adding it. | Read `calc.py` and run it with the resolved interpreter. | Source contains `return a - b`; output is `-1`. | confirmed; fix location is `calc.py`, inside `add` |

## Trajectory notes

- Gate status parsed successfully and G1 was approved before changing `calc.py`; approval record: `.dobby/approved/f50a4c0677a26900f85b9e9bc4bf1333c0879719425f8fbb7e385520e6d7d7b2.json`.
- `dobby context` was attempted as required but could not run because `.dobby/ontology.json` was absent. `dobby doctor` prescribed `dobby init --scan .`; both that command and its `--overwrite` variant exited 0 but did not create the required ontology.
- First G1 verification ran the approved command with exit 0 and output `7\r\n`, but the initial `^7$` regex did not match Windows CRLF. The gate expectation was corrected to the literal deciding token `7`, requiring fresh approval.
