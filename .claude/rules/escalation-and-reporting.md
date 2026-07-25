# Escalation and reporting

Applies to: every user question and every final message.

## Escalation ladder

- **L0 — measurable here**: counts, existence, formats, behavior a command can
  observe. NEVER ask; run it.
- **L1 — cheap-to-reverse assumption**: proceed, record it (ledger + report)
  with how to verify it later.
- **L2 — safe experiment distinguishes options**: declare the threshold first,
  run the read-only/new-dir experiment.
- **L3 — ask**, using the fixed format below, when ANY of: destructive or
  irreversible action · external publication (deploy/upload/release/send) ·
  package installs · licensed or sensitive content · >30 min operations ·
  contradictory evidence with no safe default · two valid readings that change
  the outcome · no acceptance criterion.

```
Decision required:
Evidence found:
Option A and consequence:
Option B and consequence:
Recommended option:
Reason:
Safe default if no response is available:
```

Bundle related decisions into ONE escalation. Never ask what a script can answer.

## Final report structure (mandatory for producing tasks)

1. Outcome first: Done / Partially complete / Blocked + one-sentence result;
   failures in the first paragraph, never buried.
2. Requirement table: requirement → done/not-done → evidence path.
3. Commands run with pass/fail verdicts.
4. Assumptions taken (with L-levels).
5. Not done / not verifiable here — including the standing caveats of this
   environment (fill during bootstrap: what this machine cannot verify).
