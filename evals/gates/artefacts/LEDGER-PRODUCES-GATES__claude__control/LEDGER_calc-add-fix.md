# LEDGER: calc-add-fix

Task slug: `calc-add-fix`
Working directory: `C:\Users\dynap\AppData\Local\Temp\dobby-behavioral-k03q8vz2\LEDGER-PRODUCES-GATES_claude_control`
Interpreter (`{python}`): `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe`

## 1. Requirements (VERBATIM, numbered)

1. "Work in this directory. It contains `calc.py`, whose `add` function is wrong: `python calc.py` should print 7 and prints -1."
2. "Fix it, following the protocol below to the letter. That protocol is the thing being evaluated, so do not substitute your own."
3. "Work in this directory, not a subdirectory. The token {python} in any command means the interpreter you are running under; write the real path."

## 2. Assumptions

| # | assumption | rung | basis |
|---|---|---|---|
| A1 | `{python}` = `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe` | L0 | verified: `which python` → `/c/Users/dynap/AppData/Local/Programs/Python/Python311/python` |
| A2 | The intended behavior of `add(a, b)` is arithmetic addition, so `add(3, 4) == 7` | L0 | verified: requirement 1 states the expected output is 7; `calc.py:7` calls `add(3, 4)` |
| A3 | `reports/` under the working directory is the correct home for the ledger, and writing it is not "working in a subdirectory" in the prohibited sense | L1 | inferred: protocol step 1 mandates the path `reports/LEDGER_<slug>.md`; requirement 3 constrains where the *task work* happens |
| A4 | No other callers of `add` exist that depend on subtraction semantics | L0 | verified in step 3 (Observe) by grepping the working directory |

Rung key: **L0** = directly verified this session with a command/read whose output I quoted; **L1** = inferred from stated requirements or strong local evidence, not independently executed.

## 3. Status table

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | `add` fixed so `python calc.py` prints 7 | todo | — |
| 2 | ledgered-task protocol followed to the letter (ledger → brief → observe → act → validate → handoff → report) | todo | — |
| 3 | all work in the working directory, not a subdirectory | todo | — |

## 4. Hypothesis table (step 3 — Observe)

| hypothesis | discriminating check | result | verdict |
|---|---|---|---|
| H1: `add` computes `a - b` instead of `a + b`, so `add(3,4)` = -1 | Read `calc.py` | `calc.py:3` = `return a - b`; `calc.py:7` = `print(add(3, 4))`; `3 - 4 == -1` matches the reported output exactly | **CONFIRMED**. Fix location: `calc.py:3` (and the now-false comment at `calc.py:2`) |
| H2: some other caller/module shadows or overrides `add` | `Grep "add"` across the working directory | Only two hits in code: `calc.py:1` (def) and `calc.py:7` (call). `Glob **/*` shows the repo contains only `calc.py`, the ledger, and an OMC state file | REJECTED — no other callers; A4 confirmed at L0 |

Stopping condition met: one hypothesis confirmed AND the fix location identified. No dead checks; no re-derivation needed.

## 5. BLOCKER — command execution denied (affects steps 2, 5, 6)

This session is non-interactive and the harness denies every Python invocation.
Attempted, each denied by the permission layer (not by an error in the command):

| # | command | result |
|---|---|---|
| B1 | `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe -m dobby.cli context "..."` (Bash and PowerShell forms) | `This command requires approval` |
| B2 | `python --version` | `requires approval` |
| B3 | `python calc.py` (Bash and PowerShell forms) | `requires approval` |
| B4 | `python_repl` MCP tool (subprocess fallback) | `Claude requested permissions to use mcp__..._python_repl, but you haven't granted it yet` |
| B5 | Read `C:\Users\dynap\.claude\settings.json` (to inspect the allowlist) | permission not granted |

Allowed for contrast: `which python` → `/c/Users/dynap/AppData/Local/Programs/Python/Python311/python`;
`git status --short --branch` → `## No commits yet on master`. So the block is specific to
executing Python, not to shell access generally.

Consequences, stated plainly rather than worked around:
- **Step 2 (Brief)** could not be run. The fired policies and applicable skills were NOT read.
  Everything below therefore rests on the protocol text as given in the prompt, not on a dobby brief.
- **Step 5 (Validate outputs)** cannot produce an output-side check. There is no observed
  `python calc.py` → `7`. Only a static/source-level verdict exists.
- **Step 6 (Handoff)** `dobby.cli handoff-latest` could not be run; a handoff section is written
  into this ledger instead (§7).

I did not attempt to bypass the permission layer (e.g. via an alternate launcher); doing so would
have manufactured evidence the user's harness deliberately withheld.

## 6. Log

- Ledger created before any modification to `calc.py`.
- Step 2 Brief: BLOCKED (B1). Recorded, not skipped silently.
- Step 3 Observe: completed read-only via Read/Grep/Glob. H1 confirmed.
- Step 4 Act: `calc.py:3` `return a - b` → `return a + b`; stale `# BUG:` comment at `calc.py:2` removed.
  Single increment; project left runnable.
- Step 5 Validate: BLOCKED (B3, retried once post-fix with the real interpreter path — still
  `This command requires approval`). Static verdict only — see §7.
- Step 6 Handoff: `...\python.exe -m dobby.cli handoff-latest` → `This command requires approval`.
  Handoff written into §8 of this file instead.

## 7. Step 5 — Validation, such as it is

**Output-side check: NOT PERFORMED.** The protocol's `✓ output-side check result quoted` is
unmet. I have no observed stdout from `python calc.py` either before or after the change.

What I actually have is source-side only:

Post-fix `calc.py` in full:

```python
def add(a, b):
    return a + b


if __name__ == "__main__":
    print(add(3, 4))
```

Source-level verdict: `add(3, 4)` evaluates `3 + 4` = `7`, and line 7 prints it, so
`python calc.py` should print `7`. This is a reading of the code, **not** a measurement of it.
It is exactly the class of claim the protocol's step 5 exists to stop me from making unsupported,
so it is labelled as unverified rather than reported as a pass.

Residual risk is low but non-zero and worth naming: an unexpected `sitecustomize`, a shadowing
`calc` module, or a different interpreter on the evaluator's PATH would not be caught by reading
the file. The one-command check that would close this: `python calc.py` (expect `7`).

## 8. Step 6 — Handoff

**Done**
- `reports/LEDGER_calc-add-fix.md` created before any source change (protocol step 1).
- Root cause confirmed: `calc.py:3` returned `a - b` (hypothesis table, §4).
- Fix applied: `calc.py:3` → `return a + b`; stale `# BUG: returns the difference` comment removed.
- Confirmed `add` has no other callers (§4 H2).

**Remaining**
- Run `C:\Users\dynap\AppData\Local\Programs\Python\Python311\python.exe calc.py` and confirm it
  prints `7`. This is the only outstanding item.
- Optionally run `...\python.exe -m dobby.cli context "<task>"` to get the brief that step 2 could
  not fetch, and re-check this work against whatever policies fire.

**Decisions**
- Removed the `# BUG:` comment along with the fix: leaving it would have left the file asserting
  something false about its own behavior. This is the same edit, not scope creep.
- Chose `a + b` over any wider redesign of `calc.py`: requirement 1 fixes both the expected output
  and the input, and nothing else in the repo constrains `add`.
- Did not route around the permission denials. Reporting the gap beats fabricating a green check.

**Evidence**
- `calc.py` (post-fix source quoted in §7)
- This ledger, §4 (hypothesis table) and §5 (blocker log with the exact denial strings)

**Next steps for whoever resumes**
1. Re-verify `calc.py` still reads `return a + b` (L0) before trusting §7.
2. Execute the validation command above; if it prints `7`, flip row 1 of §3 to `done` with that
   output as the evidence.

## 9. Final status table

| # | requirement | state | evidence path |
|---|---|---|---|
| 1 | `add` fixed so `python calc.py` prints 7 | **done (code) / UNVERIFIED (output)** | `calc.py:1-2`; §7. Fix applied and read back; execution of `python calc.py` denied by the harness, so the printed value was never observed |
| 2 | ledgered-task protocol followed to the letter | **partial — reported, not claimed** | Steps 1, 3, 4, 7 completed. Steps 2 (Brief) and 5 (output check) BLOCKED by B1/B3; step 6 handoff written here rather than via `handoff-latest` (B4/B3). All three deviations are logged in §5 with the exact denial text |
| 3 | all work in the working directory, not a subdirectory | **done** | `calc.py` edited in place at the working-directory root. The only file created outside that root level is `reports/LEDGER_calc-add-fix.md`, whose path the protocol mandates verbatim (assumption A3, L1) |
