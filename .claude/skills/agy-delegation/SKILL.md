---
name: agy-delegation
description: Hand a task to the Antigravity CLI (`agy`) instead of doing it here — web-grounded search, image generation, whole-codebase architecture passes, bulk generation, or anything that would cost more tool calls than it is worth. Use when a task needs a capability this process does not have, or when doing it here would take more than ~15 tool calls. Do NOT use for work the current context already holds.
---

# agy-delegation

**Trigger:** the task needs live web search, image generation, a browser, a
scientific database, a whole-codebase architecture pass — or an estimated 15+
tool calls of mechanical work whose intermediate output nobody will re-read.
**Non-trigger:** anything under ~5 tool calls; anything where this context is
already loaded with the answer; anything the user asked *you* to do.

Ported from `github.com/SafeMantella/claude-code-agy-CLI-skill`. Its flag list
and its macOS binary path were NOT copied — the flag surface here was measured
from `agy --help` on this machine (`reports/AGY_FLAG_SURFACE.md`).

---

## 1. Decide. Do not delegate by reflex.

    {python} -m dobby.cli agy check "<task>" --tool-calls <your estimate>

✓ The verdict prints `delegate: true|false` with its `basis`
(`capability` | `volume` | `trivial` | `unknown`). A `basis: unknown` verdict
means you skipped the estimate — go back and make one; delegating without it is
a guess wearing a decision's clothes.

Delegation is the exception. Under 5 estimated calls do it here; over 15,
delegate; between, delegate only if the intermediate output is bulky and
disposable.

## 2. Build the prompt and read it before paying for it.

    {python} -m dobby.cli agy prompt "<task>" --template <t> --file <path> --require "a|b|c"

✓ Prints the exact prompt, the argv, and both timeouts. Nothing is spent.
Check three things in the printed prompt:

- every path is **absolute** (the delegate's cwd is not necessarily yours);
- the **output contract** is something you can verify when it comes back;
- the constraints say **"Do NOT modify"** unless you passed `--write`. On this
  tool that sentence is the only read-only control there is — see step 4.

Templates: `research` `investigate` `review` `generate` `refactor` `websearch`
`image` `science` — see `{python} -m dobby.cli agy templates`.

## 3. Grant tool permission, or get an empty answer.

`--print` mode cannot prompt for permission, so it **auto-denies and exits 0 with
no output**. Measured 2026-08-04 (agy 1.1.8): a read-only prompt naming one file
returned rc=0, 0 characters, 18.6s. The same prompt with permission granted
returned a correct 334-character answer.

Either pass `--skip-permissions`, or add allow-rules under `permissions.allow`
in agy's own `settings.json` (narrower, and it persists).

✓ If a run comes back `ok: false` with `diagnosis: headless tool-permission
auto-deny`, that is this and nothing else. Do not go looking at the harness.

## 4. Run it.

    {python} -m dobby.cli agy run "<task>" --template <t> --file <p> --skip-permissions --yes

`--yes` is required: this is a real call that costs money and leaves the machine.

- `--write` — declares that edits are intended (`--mode accept-edits`). It does
  **not** unlock them, and its absence does not lock them: measured 2026-08-04
  (agy 1.1.8), a "create hello.txt" prompt produced the file in all four
  mode × permission combinations. The real boundaries are the **working
  directory** the call is launched in, worktree isolation, and the `Do NOT
  modify` line in the prompt. Launch anything you have not read from a scratch
  directory, and `git status` after any delegation you did not expect to write.
- `--timeout N` — seconds; becomes `--print-timeout`, and the process ceiling is
  set above it so a slow answer is reported by agy rather than mislabelled a hang.
- `--model` — verbatim from `{python} -m dobby.cli agy models`. Never from memory.
- `--effort low|medium|high`, `--add-dir`, `--continue`, `--output-format json`.

## 5. Validate the output. It is a claim, not a result.

✓ Generated code: run it, or at minimum compile it.
✓ Findings: open each `file:line` cited and confirm it says what the finding says.
✓ Web/science answers: the URLs and identifiers must resolve — a delegate has no
  more authority to assert a fact than you do.
✓ `--write` runs: `git diff` and read every hunk before anything is committed.

A truncated answer arrives with `truncation_warning`; it is a PREFIX and must
never be summarised as complete.

## 6. Record it.

The returned envelope carries the full prompt, the argv, both timeouts, and the
version observed. Put the prompt in the ledger with the result — a provider
answer whose prompt cannot be reproduced is a rumour.

---

## Anti-patterns (from the upstream skill, kept because they are right)

- Delegating a one-liner. The round trip costs more than the edit.
- Delegating without context — "fix the bug" reaches a process that has never
  seen the bug and returns a confident answer about something else.
- Trusting the output without a check.
- More than 2–3 concurrent delegations: rate limits.
