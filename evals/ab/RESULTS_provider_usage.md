# Three providers, one harness, measured usage

Executed 2026-08-23. Every number came from `evals/ab/RESULTS_compare.json`,
produced by `evals/ab/smoke_providers.py --compare`. Nothing is estimated.

Same three fixtures, same gate, same acceptance command, one gated call per
task, a fresh fixture tree per provider. This is the first comparison in this
repository where all three providers ran through the **same** harness — the
previous table put claude's numbers from one run beside codex's from another,
which measures the harness as much as the provider.

## The table

| provider | verified | calls | failed | wall | input tok | output tok | thinking tok | cache read | cost USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **claude** | 3/3 | 3 | 0 | 169.2s | 46 | 6,083 | 942 | 1,125,126 | **$1.7080** |
| **codex** | 3/3 | 3 | 0 | **111.3s** | 205,112 | 1,764 | 156 | 168,704 | *null* |
| **agy** | 3/3 | 3 | 0 | 407.6s | 766,805 | 44,518 | 30,692 | 2,507,664 | *null* |

All three solved all three. No retries, no failures, no acceptance failures.

### Per task

| task | provider | wall | in | out | thinking | cache read | cache create | cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| paginate_offbyone | claude | 72.2 | 14 | 2,061 | 559 | 338,558 | 33,146 | 0.5523 |
| discount_validation | claude | 56.3 | 18 | 2,447 | 261 | 447,566 | 33,558 | 0.6206 |
| invoice_missing_field | claude | 44.0 | 14 | 1,575 | 122 | 339,002 | 32,612 | 0.5351 |
| paginate_offbyone | codex | 30.0 | 62,789 | 405 | 9 | 51,200 | 0 | null |
| discount_validation | codex | 46.9 | 79,240 | 777 | 108 | 66,304 | 0 | null |
| invoice_missing_field | codex | 36.8 | 63,083 | 582 | 39 | 51,200 | 0 | null |
| paginate_offbyone | agy | 107.9 | 315,530 | 16,439 | 11,034 | 908,588 | null | null |
| discount_validation | agy | 210.0 | 227,468 | 16,367 | 11,832 | 885,336 | null | null |
| invoice_missing_field | agy | 92.1 | 223,807 | 11,712 | 7,826 | 713,740 | null | null |

## Read this before comparing the input column

**The three vendors do not mean the same thing by `input_tokens`, and the
column above is NOT comparable across rows.**

- claude reports 14–18 input tokens per task and 338k–447k cache READS beside
  them. Its `input_tokens` counts only what was NOT served from cache.
- codex reports 62k–79k input against 51k–66k cached. Its probe output carries
  `input_tokens` and `cached_input_tokens` as separate fields where the cached
  figure is smaller than the total, which reads as `input_tokens` INCLUDING the
  cached portion. **That reading is not verified** — nobody here has run a
  controlled cache-hit experiment against codex — so it is written down as an
  open question rather than used.
- agy reports neither a cache-creation counter nor a cost.

What IS comparable across all three, because each vendor reports it and it means
one thing: **output tokens, thinking tokens, wall time, and call count.**

| comparable | claude | codex | agy |
|---|---:|---:|---:|
| wall time | 169.2s | **111.3s** | 407.6s |
| output tokens | 6,083 | **1,764** | 44,518 |
| thinking tokens | 942 | **156** | 30,692 |
| calls | 3 | 3 | 3 |

On these three one-file fixtures codex was **1.5x faster than claude and 3.7x
faster than agy**, produced **3.4x fewer output tokens than claude and 25x fewer
than agy**, and did the least reasoning of the three. agy spent an order of
magnitude more of everything and still finished 3/3.

## Cost is not comparable and no "cheapest" claim is made

Only claude reports money: **$1.7080 for three tasks, $0.5693 per verified
task.** codex and agy report no cost figure, so `cost_usd` is null for both and
`providers/policy.economics` reports `economics_status: unmeasured`.

Null is not zero and it is not cheap. The operator's Agy subscription and Codex
allowance are already paid for, which is a real reason to prefer them — but that
is a statement about a budget, not a measured price per task, and this file does
not turn one into the other.

## Isolation, checked four ways

agy runs isolated because the policy will not let it run anywhere else. The
protected root was compared before and after on:

    git rev-parse HEAD
    git diff --no-ext-diff
    git diff --cached --no-ext-diff
    git status --porcelain --untracked-files=all
    a content hash of every tracked and untracked file

**0 differing views across all agy runs.** A HEAD comparison alone would have
missed an untracked file; `git diff` alone would have missed a staged one; the
content manifest catches a file rewritten to the same length.

## What PR 9 changed

- **codex and agy usage parsers**, from probed output rather than documentation.
  codex emits JSONL and has no envelope, so it has its own reader that sums
  `turn.completed` events — taking only the last would report the final turn's
  cost as the call's.
- **Names mapped into one shape.** `reasoning_output_tokens`,
  `thinking_tokens` and `output_tokens_details.thinking_tokens` are one quantity
  under three spellings.
- **`roll_up`**: per provider, `calls_total` / `calls_succeeded` /
  `calls_failed` kept apart, every attempt recorded individually, and usage
  summed with its own denominator. A provider that fails half its calls and one
  that makes half as many are the same number to a single counter.
- **A failed call is recorded with `usage: null`**, not with zeros. It launched
  and spent time; nobody knows what it spent.

## What is still not known

- Whether codex's `input_tokens` includes its cached portion. One controlled
  cache experiment would settle it.
- Anything about any of these providers on work harder than a one-file fix with
  a pre-written failing test. **`codex is the default implementer` remains
  smoke-verified only** — a provider that is fast and accurate here may take
  many more calls on a multi-file change, which is what the S1–S4 corpus is for.
- agy's variance. An earlier run of the same fixture failed with exit 1 after
  309 seconds and no stderr; this run it succeeded in 107.9s. Two samples is not
  a failure rate.
