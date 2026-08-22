# A/B/C/D pilot — does the adaptive policy remove the measured overhead?

Executed 2026-08-23. Three tasks, four arms, twelve runs, real provider calls.
Every number came from `evals/ab/RESULTS_pilot_abcd.json`. Nothing is estimated.

Same provider (`claude`, `claude-opus-5[1m]`), same task text, same acceptance
command, same `max_calls=4`, a fresh fixture tree per arm, randomised order,
seed `20260823`. **All three tasks produced a complete quadruple**, so nothing is
dropped from the comparison — see "the void rule" below for why that mattered.

## Result

| Metric | A_direct | B_gated | C_dobby | D_adaptive |
|---|---:|---:|---:|---:|
| Verified success | 3/3 | 3/3 | 3/3 | 3/3 |
| First-pass verification | 3/3 | 3/3 | 3/3 | 3/3 |
| Provider calls (avg) | 1.00 | 1.00 | 3.00 | **1.00** |
| Retries | 0 | 0 | 0 | 0 |
| Wall time s (avg) | 70.3 | 72.6 | 343.7 | 110.3 |
| Agent seconds (avg) | 70.2 | 69.7 | 335.5 | 104.0 |
| Cost USD (total) | 1.7564 | 1.6748 | 5.8122 | 1.8870 |
| **Cost per verified task** | 0.5855 | **0.5583** | 1.9374 | **0.6290** |
| Acceptance failures | 0 | 0 | 0 | 0 |
| Human interventions | 0 | 0 | 0 | 0 |
| False successes | 0 | 0 | 0 | 0 |
| Output tokens | 5,287 | 5,912 | 30,801 | 6,730 |
| Thinking tokens | 379 | 778 | 9,507 | 824 |

### Raw rows

| task | arm | verified | calls | wall s | cost USD |
|---|---|---|---:|---:|---:|
| discount_validation | A_direct | yes | 1 | 65.9 | 0.6013 |
| discount_validation | B_gated | yes | 1 | 68.2 | 0.5757 |
| discount_validation | C_dobby | yes | 3 | 320.2 | 1.9260 |
| discount_validation | D_adaptive | yes | 1 | 91.0 | 0.6542 |
| invoice_missing_field | A_direct | yes | 1 | 65.7 | 0.5648 |
| invoice_missing_field | B_gated | yes | 1 | 80.9 | 0.5878 |
| invoice_missing_field | C_dobby | yes | 3 | 438.1 | 2.0212 |
| invoice_missing_field | D_adaptive | yes | 1 | 115.0 | 0.5830 |
| paginate_offbyone | A_direct | yes | 1 | 79.2 | 0.5903 |
| paginate_offbyone | B_gated | yes | 1 | 68.8 | 0.5113 |
| paginate_offbyone | C_dobby | yes | 3 | 272.7 | 1.8650 |
| paginate_offbyone | D_adaptive | yes | 1 | 125.0 | 0.6498 |

## Measured improvement: D against C, which is what the policy replaces

| | C_dobby | D_adaptive | change |
|---|---:|---:|---|
| provider calls | 3.00 | 1.00 | **-67%** |
| cost per verified task | $1.9374 | $0.6290 | **-68%** (3.08x cheaper) |
| wall time | 343.7s | 110.3s | **-68%** (3.1x faster) |
| thinking tokens | 9,507 | 824 | **-91%** (11.5x fewer) |
| output tokens | 30,801 | 6,730 | -78% |
| verified success | 3/3 | 3/3 | unchanged |

The overhead the first pilot priced is gone, and the gate is still there: D runs
the effect contract, the acceptance check and artifact promotion, and its item
reaches DONE through the same PK-2 judgement C uses.

## The declared threshold, and the half of it D missed

`reports/LEDGER_adaptive_execution.md` recorded the pass condition BEFORE this
ran: *3/3 verified, median provider calls <= 1.1, and cost per verified task
within 10% of B_gated.*

| condition | result | |
|---|---|---|
| 3/3 verified | 3/3 | **pass** |
| median calls <= 1.1 | 1.0 | **pass** |
| cost within 10% of B_gated | $0.6290 vs $0.5583 = **+12.7%** | **FAIL** |

D_adaptive is 12.7% more expensive per verified task than B_gated, against a
threshold of 10% declared in advance. It is reported as a miss rather than
rounded to "about the same".

D is also slower than B: 110.3s against 72.6s wall, and 104.0s against 69.7s in
AGENT seconds — so most of the gap is the model call itself, not harness
bookkeeping. The fast path's prompt states the scope and the acceptance commands
where B's states only the task, and a longer prompt with more to satisfy is a
plausible cause. It is not measured, and it is the first thing to look at.

## Not improved

Verified success, first-pass rate, retries, acceptance failures, human
interventions and false successes are identical across all four arms. This corpus
still contains no failure for a gate to catch — every task is
`one_shot_plausible: true` by design, per `DESIGN.md` criterion 6 — so the value
of the gates remains unmeasured here, exactly as in the first pilot.

## What D still costs over doing nothing

Against A_direct, the operator's previous habit: +7.4% cost per verified task
($0.6290 vs $0.5855) and +57% wall time. That is the price of the effect
contract, the acceptance gate, artifact promotion and durable state on a corpus
where none of them caught anything. Whether it is worth paying is a question this
corpus cannot answer.

## The void rule, and why the run before this one is not reported

The 2026-08-22 four-arm run had three rows with ZERO provider calls — a provider
failure hit `A_direct/invoice`, `B_gated/paginate` and `C_dobby/paginate`. Scored
naively that reads as D winning 3/3 against 2/3, which would have been a result
manufactured by whichever arm the provider happened not to fail on.

`DESIGN.md` already said a task failing for an environmental reason is void for
EVERY arm. `runner.mark_void` now applies it mechanically: a provider-driven row
with no recorded call never ran, and `paired_tasks` drops any task that is not
complete in all arms. Applied to that run, one task of three survived — n=1, and
no comparison was reported from it.

## A defect this pilot found in the policy it was testing

The first D_adaptive run scored **0/3**, two calls per task, every one stopping
at `no_repair_derived`. `profile_item` derived the side-effect class from the
COMPILED PLAN's write set, so an item nobody had planned profiled as read-only,
the fast path granted no write, and the provider was asked to edit a file it was
not permitted to touch.

That is the same defect the write grant was built to fix, arriving through a new
door — a policy layer silently granting less than the thing it replaced.
`runner.default_graph` has always assumed LOCAL_WRITE for its execute node; the
fast path now does the same unless the item says otherwise, an empty scope means
"check the tree" rather than "this writes nothing", and
`test_an_item_with_no_plan_still_gets_the_write_grant` pins it.

## Not measurable here

- **Whether the gates help.** Needs a corpus where the single call fails.
- **Any provider but claude.** `usage_extra` is empty for codex, gemini and agy,
  so their cost and tokens would be null, and `providers/policy.economics`
  reports them as `unmeasured` rather than cheap.
- **Statistical significance.** `runtime/bench.MIN_TASKS` is 8; three is below it
  and no verdict is claimed.
- **Whether Codex is a better implementer than claude.** The role policy names it
  first, and nothing here has run it.
