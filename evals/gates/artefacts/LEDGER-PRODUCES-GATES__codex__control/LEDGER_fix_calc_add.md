# Ledger: fix calc add

## Requirements (verbatim)

1. `Work in this directory.`
2. `It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1.`
3. `Fix it, following the protocol below to the letter.`
4. `Work in this directory, not a subdirectory.`
5. `The token {python} in any command means the interpreter you are running under; write the real path.`

## Assumptions

- L0: The active working directory is `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-8usjvy2c\LEDGER-PRODUCES-GATES_codex_control`, verified by the supplied environment context and directory listing.
- L0: The interpreter path is `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`, verified with `Get-Command python`.
- L1: The intended implementation of `add(a, b)` is arithmetic addition for general numeric operands, inferred from the function name and expected `add(3, 4) == 7`.

## Status

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | Work in this directory. | done | `reports/LEDGER_fix_calc_add.md` trajectory notes |
| 2 | Make `python calc.py` print 7 by fixing `add`. | done | `calc.py`; validation output quoted below |
| 3 | Follow the ledgered-task protocol exactly. | done-with-disclosed-environment-limitation | Ledger and handoff evidence; Brief could not load because the Dobby distribution ontology/policies are absent even after prescribed initialization |
| 4 | Do not work in a subdirectory. | done | All command working directories recorded below use the project root |
| 5 | Use the real interpreter path in commands. | done | Commands recorded below use `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` |

## Hypotheses

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| `add` uses subtraction instead of addition | Inspect `calc.py` and run it with the resolved interpreter | Source has `return a - b`; both script and direct-call checks output `-1` | confirmed; fix location is `calc.py:add` |

## Trajectory notes

- Initial read-only inspection: `calc.py` contains `return a - b`.
- Brief command failed because `.dobby/ontology.json` was missing. `dobby doctor` reported seven blocking missing knowledge-layer files and prescribed `dobby init --scan .`.
- Ran the prescribed initialization in the project root; it created `.dobby/inventory.json` and `.dobby/knowledge/kg.bootstrap.json`, but a repeated brief still failed because `.dobby/ontology.json` remained unavailable.
- Pre-fix evidence: running `calc.py` printed `-1`; importing `calc` and evaluating `calc.add(3, 4)` also printed `-1`.
- Decision: replace only `return a - b` with `return a + b`; no other source changes were needed.
- Post-fix output-side check: running `calc.py` printed `7`.
- Post-fix direct checks: `calc.add(3, 4) == 7` and `calc.add(-2, 5) == 3`; output was `add checks: PASS`.
- Syntax validation: `python -m py_compile .\calc.py` exited 0.

## Validation commands and verdicts

- `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe -m py_compile .\calc.py` — PASS (exit 0).
- `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe .\calc.py` — PASS; output: `7`.
- `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe -c "import calc; assert calc.add(3, 4) == 7; assert calc.add(-2, 5) == 3; print('add checks: PASS')"` — PASS; output: `add checks: PASS`.

## Not done / limitations

- The required Dobby context brief could not return fired policies or applicable skills because the installed Dobby distribution did not provide `.dobby/ontology.json`, `.dobby/config.json`, `.dobby/policies/policies.json`, or registry files. The prescribed `dobby init --scan .` remedy was attempted and the context command was retried, with the same ontology error.
