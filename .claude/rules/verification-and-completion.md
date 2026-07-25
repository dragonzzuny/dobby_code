# Verification and completion

Applies to: every producing task and every final report.

1. Outputs are validated by an independent check AFTER producing. The producing
   command exiting 0 is not validation. Inputs passing says nothing about outputs.
2. Prefer the strongest available check, bottom-up: existence → structure/
   syntax → contract (consumer-side schema/test) → end-to-end behavior. State
   explicitly which levels were NOT available in this environment.
3. A FAIL is reported as FAIL, with the failing rows quoted. Never soften to
   "mostly fine", never silently fix the check's target to make it pass.
4. DONE means: every ledger row `done` with an evidence path · required checks
   passed ON OUTPUTS · zero writes outside scope · report follows P-REPORT.
   Anything else opens with "Partially complete" or "Blocked" + why.
5. Evaluation assets (criteria files, gold labels, holdout sets) are immutable
   during a task. Changing the test to fit the output is task failure
   (Evaluation Gaming — docs/FAILURE_CATALOG.md).
6. Before the final message: re-read the ledger top to bottom and verify each
   evidence path exists and supports its claim.
