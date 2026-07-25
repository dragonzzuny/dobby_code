---
name: author-evals
description: Build the host project's evaluation assets — behavioral scenarios with objective verification, retrieval gold labels (dev/val/holdout), and evaluator criteria. Use after bootstrap, or when asked to measure/evaluate the harness.
---

# author-evals

**Trigger:** post-bootstrap hardening; "measure/evaluate the harness"; a new
failure mode was observed (add a scenario for it).
**Non-trigger:** running existing evals (`cli optimize`, `cli slice`).

1. **Mine reality, not imagination.** Sources for scenarios: real incidents,
   recurring requests, past regressions, ambiguous asks ("clean this up"),
   adversarial shortcuts users actually attempt ("skip the checks, I trust
   you"). Each scenario gets: task text verbatim-realistic, hidden traps,
   required observations/actions, forbidden actions, expected artifacts, and
   an OBJECTIVE verification (commands whose exit codes decide).
   ✓ every scenario's `verification` field is executable, not a judgment call.
2. **Write retrieval gold** per `evals/retrieval_gold.template.yaml`: for each
   scenario, the KG nodes a weak model must have in context to avoid the trap.
   Curate missing nodes FIRST (with provenance), then reference them.
   Split dev/val/holdout ≈ 40/30/30.
   ✓ `{python} -m dobby.cli optimize --seeds 0 --iters 5 --pop 6` runs clean.
3. **Write evaluator criteria** (`.dobby/criteria/<name>_criteria.json`) for
   the artifact types this project actually produces: deterministic checks
   first (`command`/`path_exists`/`path_absent`), `model_judgment` only for
   what commands cannot measure.
   ✓ `Evaluator` loads it (any slice run) and criteria_integrity=true.
4. **Add a slice plan** (`.dobby/slice_plans.json`) for at least one
   fixture-based scenario so the loop is exercisable end-to-end without a
   model.
   ✓ `{python} -m dobby.cli slice --scenario <id>` completes all 10 steps.
5. **Freeze.** Gold, criteria, and holdout are immutable for the improvement
   loop (already enforced) and for YOU during any task that they measure —
   edits happen only in a dedicated eval-maintenance task with a changelog row.

**Failure modes:** scenarios that test doc-reading instead of behavior;
verification that requires opinion; gold nodes that don't exist in the KG
(optimize will crash — that's the check working); holdout reused for tuning
(invalidates every later claim).
