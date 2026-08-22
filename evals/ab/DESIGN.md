# A/B benchmark: does the dobby workflow change the outcome?

Written BEFORE any task was chosen, so the selection cannot be fitted to a
result. Nothing in this file is a number; every number lands in `RESULTS.md`
with the run id that produced it.

## The claim this can and cannot support

It can support: *on this corpus, with this model, under these budgets, arm B
finished more tasks VERIFIED than arm A, or the same number for less.*

It cannot support "dobby makes the model better". `docs/EVAL_DESIGN.md` forbids
that claim in this repository and this does not weaken it. A corpus of 5-10 local
tasks is a PILOT: `runtime/bench.py` sets `MIN_TASKS = 8` below which it refuses
to declare a winner, and that refusal is kept.

## Arms

| arm | what runs | what checks it |
|---|---|---|
| **A — baseline** | ONE provider call with the whole task as a prompt. No contract, no gate, no state, no failure classification, no retry-with-repair. | the acceptance check, run ONCE on the final artifact only |
| **B — dobby** | the project loop: structured item, acceptance checks, PROPOSED→VERIFIED→PROMOTED, failure classification, repair-derived retry | the same acceptance checks, applied by the runtime gate |

Deliberately identical between arms: provider, model, task text, acceptance
checks, working tree, per-task call budget, machine.

**The middle arm is kept.** `runtime/bench.py` also defines `gated` — one call
WITH the contract and gate but no graph. Without it, a difference between A and B
has at least three explanations and the reader cannot tell "checking the output"
from "structuring the work". It costs a third of the budget and it is the arm
that makes the result arguable rather than merely favourable.

## Baseline fairness — the thing that decides whether any of this means anything

A baseline chosen to lose proves nothing. This one is pinned by construction:

- **Same prompt text.** Arm A receives the task statement verbatim, the same
  string arm B puts in the work item's `outcome`. It is not a deliberately vague
  prompt.
- **Same budget.** Both arms get the same ceiling on provider calls per task,
  recorded in `RESULTS.md`. Arm A may use its whole budget in one call; arm B
  spends its across nodes. Neither gets more.
- **Same checks.** The acceptance command is identical and is run on arm A's
  output too. Arm A is not graded by a human reading it.
- **Arm A is a competent single-agent use, not a strawman.** It is what the
  operator did before: state the task, take the answer, check it once.

What arm A does NOT get is the thing under test: gates between steps, state that
survives, a classified failure, and a retry that carries the failure into the
next attempt.

## Task selection criteria — fixed before looking at candidates

A task enters the corpus only if ALL of these hold:

1. **Machine-checkable.** A command exits 0 when it is done and non-zero when it
   is not. No task graded by reading prose.
2. **The check existed before the task was chosen**, or is derived from the
   repository's own declared checks. No check written to suit an arm.
3. **Currently failing or absent.** A task whose check already passes measures
   nothing.
4. **Self-contained.** Touches this repository only; no network, no external
   service, no other machine.
5. **Under ~15 minutes for a competent single attempt**, so a pilot fits in a
   session and a stuck arm is a signal rather than a timeout artifact.

And these EXCLUSIONS, written to stop the corpus tilting toward dobby:

6. **Not chosen for having sub-steps.** Multi-step tasks favour the arm that
   structures work. The corpus must include tasks a single call plausibly
   finishes in one shot, or the comparison is rigged.
7. **Not chosen for having a failure mode dobby classifies well.**
8. **No task drawn from work done this week**, where the answer is already in
   this repository's recent history.

Difficulty mix is recorded per task as `one_shot_plausible: true|false` BEFORE
running, so the result can be split by it afterwards. If every task is
`one_shot_plausible: false`, the corpus is rigged and the report says so.

## Order

Randomised over (task, arm) pairs with a seed recorded in `RESULTS.md`, so a
provider warming up, rate-limiting, or degrading over a session cannot land
systematically on one arm.

## Metrics — every one of them measured, none derived from belief

Primary:

| metric | how it is obtained |
|---|---|
| verified success rate | the acceptance check's exit code, per task per arm |
| first-pass verification rate | verified with `attempts == 1` and zero repairs |
| provider calls to final success | counted at the provider adapter, not estimated |
| retries | node attempts beyond the first |
| wall-clock per task | monotonic clock around the arm |
| agent seconds | summed provider call durations |
| acceptance-check failures | count of non-zero exits from the gate |
| schema / contract violations | `CONTRACT_VIOLATION` classifications |
| human interventions | count of stops requiring a person (`NEEDS_*`, `ITEM_BLOCKED` with no derivable repair) |

Secondary:

| metric | how |
|---|---|
| unverified artifact reaching a downstream step | arm B: artifacts promoted without VERIFIED. Structurally impossible; asserted, and reported as 0 with that caveat rather than as an achievement |
| bad intermediate reaching downstream (arm A) | arm A has one step, so this is 0 BY CONSTRUCTION and is reported as not-applicable rather than as a win for A |
| redundant re-work | a node re-run after it had already succeeded |
| same error repeated | identical failure classification twice on one task |

Cost, now measurable and previously not:

`claude -p --output-format json` was probed this session and returns
`total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`,
`usage.output_tokens_details.thinking_tokens`, and cache read/creation counts.
`runtime/metrics.py` currently states the engine "cannot see money"; that note is
true of the other CLIs and is now FALSE for claude. Instrumentation is
implemented separately from this benchmark, and cost is reported only from
collected usage, never estimated.

## Stopping rules, declared now

- Below 8 paired tasks the verdict is `inconclusive`, whatever the direction.
- A bootstrap interval spanning zero is `inconclusive`, not "a trend".
- If arm A wins on any metric, that metric appears in the report unchanged.
- A task that fails for an environmental reason (provider unavailable, timeout,
  rate limit) is recorded as `void` for BOTH arms and excluded from the paired
  statistic, because dropping it from one arm only is how a corpus tilts.
