---
name: ledgered-task
description: Protocol for any multi-step task (>1 requirement or >~10 expected tool calls) and for resuming after a session/context loss. Ledger → evidence-first execution → output validation → honest report → handoff.
---

# ledgered-task

**Trigger:** >1 requirement; >~10 expected tool calls; any producing task; any
resume ("continue where we left off").
**Non-trigger:** single lookups a command answers directly.

1. **Restate.** Write `reports/LEDGER_<slug>.md`: requirements VERBATIM,
   numbered; assumptions (each L0/L1 per the ladder); status table
   `| # | requirement | state | evidence path |` all `todo`.
   ✓ file exists BEFORE the first change. Never pre-fill `done`.
2. **Brief.** `{python} -m dobby.cli context "<task>"` — read the fired
   policies and applicable skills; fetch bodies only for what you'll execute.
3. **Observe.** Run the read-only evidence steps the policies require. For
   investigations keep a hypothesis table
   `| hypothesis | discriminating check | result | verdict |`; stop when one
   is confirmed AND the fix location is identified (anti Infinite-Investigation:
   3 dead checks in a row → re-derive hypotheses from the knowledge graph).
4. **Act** in the smallest increments that leave the project runnable (or the
   breakage explicitly isolated). Outputs → new locations. Update ledger rows
   as you go; record decisions/evidence via `record_evidence` (MCP) or
   trajectory notes.
5. **Validate outputs** per verification-and-completion.md.
   ✓ output-side check result quoted.
6. **Handoff** whenever the session may end or a budget is hit: done /
   remaining / decisions / evidence / next-steps (the trajectory `handoff`
   capability writes the file).
   ✓ `{python} -m dobby.cli handoff-latest` returns it.
7. **Report** per escalation-and-reporting.md: outcome first, requirement
   table, commands+verdicts, assumptions, not-done list.
   ✓ every ledger row `done`-with-evidence or explicitly reported.

**On resume:** newest handoff + newest ledger → continue from the first
non-done row; re-verify (L0) any path you are about to write to; do NOT re-plan
from scratch and do NOT trust pre-loss numbers whose provenance you can no
longer see.
