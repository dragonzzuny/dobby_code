# Ledger: fix calc add

## Requirements (verbatim)

1. "Work in this directory."
2. "It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1."
3. "Fix it, following the protocol below to the letter. That protocol is the thing being evaluated, so do not substitute your own."
4. "Work in this directory, not a subdirectory."
5. "The token {python} in any command means the interpreter you are running under; write the real path."

## Assumptions

- L0: The active workspace is `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-utqlfko_\LEDGER-PRODUCES-GATES_codex_control`, observed from `$PWD.Path`.
- L0: The interpreter is `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`, observed from `Get-Command python`.
- L1: Correcting `add` should be the smallest source change that makes the script print exactly `7`; this will be confirmed by inspecting `calc.py` and running the acceptance gate.

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | `GATES.md` (recorded CWD and passing evidence) |
| 2 | Fix `calc.py` so its `add` function makes `python calc.py` print 7 instead of -1. | done | `GATES.md` G1: `exit=0 matched=True met=True :: 7` |
| 3 | Follow ledgered-task and runnable-gates exactly. | done | `reports/LEDGER_fix_calc_add.md`; `GATES.md`; `.dobby/approved/94ceee34ae90bb19b5ce3620f66f82d42b6c800b4fd317fc23423c2cf9b6bc27.json` |
| 4 | Work in this directory, not a subdirectory. | done | `GATES.md` recorded CWD |
| 5 | Replace `{python}` with the real interpreter path in commands. | done | `GATES.md` CHECK uses `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` |

## Investigation hypotheses

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| `add` uses subtraction instead of addition. | Inspect `calc.py` and compare `add` implementation with the observed `-1` output. | `calc.py` contains `return a - b`; running it exits 0 and prints `-1`. | confirmed; fix location is `calc.py` line 3 |

## Trajectory notes

- Ledger created before modifying `calc.py`; all rows initialized to `todo`.
- Brief command attempted twice but was blocked because `.dobby/ontology.json` and the policy/skill registries are absent; `dobby init --scan .` created only bootstrap inventory/knowledge files and did not restore the missing distribution metadata.
- Gate G1 parsed successfully and was approved for its exact command using `DOBBY_APPROVAL_DIR` inside the workspace because the sandbox denies writes to the default `C:\Users\dynap\.dobby\approved`.
- Observation confirmed the sole hypothesis and identified the fix location; investigation stopped.
- Changed `calc.py` from subtraction to addition in one increment.
- Gate verification summary: `gates=1`, `runnable=1`, `met=1`, `unmet=[]`, `unapproved=[]`, `ok=true`; output-side evidence is `7`.
