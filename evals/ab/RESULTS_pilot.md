# Pilot results — 3 tasks x 3 arms, executed 2026-08-22

Every number below came from `evals/ab/RESULTS_pilot.json`, produced by
`evals/ab/runner.py`. Nothing is estimated or extrapolated. Raw per-task rows are
kept beside the averages, because three tasks is not enough for an average to
mean much on its own.

Conditions, identical across arms: provider `claude`, model `claude-opus-5[1m]`,
`max_calls=4`, seed `20260822`, a fresh copy of the fixture tree per arm,
randomised (task, arm) order. Executed order:

    C/paginate · A/discount · A/paginate · C/discount · A/invoice
    B/discount · B/invoice · B/paginate · C/invoice

## Summary

| Metric | A_direct | B_gated | C_dobby |
|---|---:|---:|---:|
| Verified success | 3/3 | 3/3 | 3/3 |
| First-pass verification | 3/3 | 3/3 | 3/3 |
| Provider calls (avg) | 1.0 | 1.0 | 3.0 |
| Retry count (total) | 0 | 0 | 0 |
| Wall time s (avg) | 85.9 | 96.7 | 291.7 |
| Agent seconds (avg) | 85.8 | 93.2 | 283.3 |
| Cost USD (total) | 1.8534 | 1.8883 | 5.4450 |
| **Cost USD per verified task** | **0.6178** | **0.6294** | **1.8150** |
| Acceptance failures | 0 | 0 | 0 |
| Human interventions | 0 | 0 | 0 |
| False successes | 0 | 0 | 0 |
| Output tokens (total) | 5,713 | 6,635 | 25,809 |
| Thinking tokens (total) | 358 | 521 | 5,576 |
| Cache creation tokens (total) | 99,124 | 100,153 | 325,412 |

## Raw rows

| task | arm | verified | first-pass | calls | wall s | agent s | cost USD |
|---|---|---|---|---:|---:|---:|---:|
| paginate_offbyone | A_direct | yes | yes | 1 | 67.1 | 67.0 | 0.5577 |
| paginate_offbyone | B_gated | yes | yes | 1 | 94.4 | 91.3 | 0.6066 |
| paginate_offbyone | C_dobby | yes | yes | 3 | 337.1 | 328.5 | 2.0303 |
| discount_validation | A_direct | yes | yes | 1 | 89.9 | 89.9 | 0.6683 |
| discount_validation | B_gated | yes | yes | 1 | 90.1 | 86.0 | 0.6429 |
| discount_validation | C_dobby | yes | yes | 3 | 249.5 | 240.8 | 1.7157 |
| invoice_missing_field | A_direct | yes | yes | 1 | 100.6 | 100.6 | 0.6273 |
| invoice_missing_field | B_gated | yes | yes | 1 | 105.7 | 102.5 | 0.6389 |
| invoice_missing_field | C_dobby | yes | yes | 3 | 288.6 | 280.7 | 1.6991 |

Per-row run ids are in `RESULTS_pilot.json` under `rows[].run_id` — the runtime's
own ids for the B and C arms, and `direct:<task_id>` for the A arm, which runs no
graph and therefore has no runtime run.

## Measured improvement

**None.** Every arm verified every task on the first pass. There is no metric on
which B or C beat A, and reporting one would mean inventing it.

## Not improved

Verified success, first-pass rate, retries, acceptance failures, human
interventions and false successes are IDENTICAL across all three arms — 3/3, 3/3,
0, 0, 0, 0. The gates had nothing to catch, because nothing went wrong.

## Overhead dobby added

Real, measured, and the only difference the pilot found:

| | A to C |
|---|---|
| provider calls | 1.0 to 3.0 (**3.0x**) |
| wall time | 85.9s to 291.7s (**3.4x**) |
| agent seconds | 85.8s to 283.3s (**3.3x**) |
| cost per verified task | $0.6178 to $1.8150 (**2.94x**) |
| output tokens | 5,713 to 25,809 (**4.5x**) |
| thinking tokens | 358 to 5,576 (**15.6x**) |

B costs almost exactly what A costs — +1.9% on cost per verified task, +12.6% on
wall time. **The gate is nearly free; the entire overhead is the graph.** C runs
plan, execute and report as separate provider calls where A runs one.

## Why this result was predictable, and why the corpus was built this way anyway

Every task was recorded `one_shot_plausible: true` BEFORE running, per
`DESIGN.md` criterion 6, which forbids choosing tasks for having sub-steps
because that favours the structured arms and rigs the comparison. The consequence
is exactly what happened: on work a single call finishes correctly, every gate is
pure cost and every retry mechanism goes unused.

**This pilot cannot show what dobby is for.** It measured the overhead honestly
and found no benefit, because the corpus contains no failure for a gate to catch.
The claim it supports is narrow and worth stating plainly: *on one-shot tasks
with a capable model, the harness costs about 3x and buys nothing measurable.*

## What the pilot DID validate: the instrumentation

| check | result |
|---|---|
| provider call count matches adapter calls | yes — 1 / 1 / 3, counted at `run.recording()` |
| token and cost recorded per call | yes — all 9 runs, none null |
| wall time and agent seconds recorded | yes |
| first-pass verification | yes, per row |
| retries | yes, 0 everywhere |
| acceptance failures | yes, 0 everywhere |
| human interventions | yes, 0 everywhere |
| false successes | yes, 0 everywhere |

## Not measurable here

- **Whether the gates help.** Needs a corpus where the single call FAILS. Nothing
  in this pilot's design or result says anything about that case.
- **Any provider but claude.** `usage_extra` is empty for codex, gemini and agy,
  so cost and tokens would be null for them.
- **Statistical significance.** `runtime/bench.py` sets `MIN_TASKS = 8`; three is
  below it and no verdict is claimed.

## Two harness faults this pilot found in its own first run

Both would have produced a false comparison. The first run's numbers are void and
are described here rather than reported.

1. **The C arm never called a provider.** Its baseline was set to the task's own
   failing test, so PK-1 correctly refused to start work on a tree that fails its
   own checks: 0 calls, `baseline_failed`, three "losses" that were never run.
   The invariant was right and the harness was misusing it — a baseline says the
   tree is sound enough to work in, an acceptance check says the work is done.
   Fixed: the baseline is a soundness check and the acceptance check is separate.

2. **The B arm failed all three with the file correctly fixed.** The
   `PERMISSION_DENIED` rule added earlier the same day checked
   `permission_denials` before the effect, so claude fixing the code while being
   refused five unrelated tools was reported as a permission failure. That is the
   same defect the rule was written to prevent, pointing the other way: a verdict
   with no basis, this time a negative one. Fixed: the effect decides, and a
   denial only refines the diagnosis when the effect is missing.
