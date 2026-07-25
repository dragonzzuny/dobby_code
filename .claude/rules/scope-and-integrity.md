# Scope and integrity

Applies to: every file operation.

1. Never modify, move, rename, or delete anything you did not create this
   session without explicit user approval AND a named restore path. Check
   `.dobby/config.json` `protected_paths` — those are absolute no-touch.
2. All produced artifacts go to NEW files/directories (or the project's
   designated output locations). If a producing step fails midway, delete only
   what you created and restart.
3. Generated files (build outputs, lockfiles-from-manifests, compiled assets,
   anything a tool regenerates) are never hand-edited — fix the source and
   regenerate.
4. "Clean up / tidy / fix the duplicates" requests default to the NON-destructive
   reading: produce a cleaned copy or a report. Moving/deleting originals is an
   escalation (P-ESCALATE) with the exact file list.
5. Touch exactly what the ledger scopes. Defects you notice elsewhere are
   REPORTED as findings, not fixed inline — even one-line ones.
6. Every write/delete you perform appears in the final report's operation list.
