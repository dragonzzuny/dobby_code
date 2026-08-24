# Acceptance gates

- [x] G1: running calc.py with the active interpreter prints 7
  CHECK: C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe calc.py
  EXPECT: 7
  EVIDENCE: shell=C:\WINDOWS\system32\cmd.exe cwd=C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-hjn0adtx\LEDGER-PRODUCES-GATES_codex exit=0 matched=True met=True :: 7

- [x] G2: GATES.md exists in the task root
  CHECK: powershell -NoProfile -Command "if (Test-Path -LiteralPath '.\\GATES.md' -PathType Leaf) { 'GATES_ROOT_OK' } else { exit 1 }"
  EXPECT: GATES_ROOT_OK
  EVIDENCE: shell=C:\WINDOWS\system32\cmd.exe cwd=C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-hjn0adtx\LEDGER-PRODUCES-GATES_codex exit=0 matched=True met=True :: GATES_ROOT_OK

- [x] G3: the ledger exists in the task root
  CHECK: powershell -NoProfile -Command "if (Test-Path -LiteralPath '.\\LEDGER_fix_calc.md' -PathType Leaf) { 'LEDGER_ROOT_OK' } else { exit 1 }"
  EXPECT: LEDGER_ROOT_OK
  EVIDENCE: shell=C:\WINDOWS\system32\cmd.exe cwd=C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-hjn0adtx\LEDGER-PRODUCES-GATES_codex exit=0 matched=True met=True :: LEDGER_ROOT_OK

- [ ] G4: follow the supplied ledgered-task protocol to the letter
