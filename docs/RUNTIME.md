# The execution runtime

`dobby/runtime/` — a durable control loop over the primitives this kit already
had.

## The gap it closes

Before this, the harness could route a task, fan work out to several providers,
isolate mutating agents in git worktrees, judge an output, and record a
trajectory. Every primitive, and no loop that closed over them. The connective
tissue was a person: run this command, read the JSON, decide, run the next one.

That works until the work outlasts the person's attention. Then the run has no
state anyone can resume and no record of what it already did — so recovering
means starting again, and starting again means paying for the finished half
twice and hoping the side effects were idempotent.

The runtime is that missing loop, and nothing more. It does not replace the
router, the fan-out, the swarm protocols or the evaluator; it schedules them.

## The pipeline

```
router → task graph → scheduler → worker → verifier gate → promote or repair
                ↑                                              │
                └──────────────── re-plan ─────────────────────┘
```

## Objects and their invariants

| object | states | the invariant |
|---|---|---|
| `TaskRun` | `QUEUED → RUNNING → WAITING\|RECOVERING → SUCCEEDED\|FAILED\|CANCELLED` | a terminal state is final; a run over budget admits no new node |
| `TaskNode` | `PENDING → READY → LEASED → RUNNING → VERIFYING → SUCCEEDED\|FAILED\|SKIPPED` | a node cannot be `READY` until every dependency `SUCCEEDED` |
| `Attempt` | `STARTED → FINISHED\|RETRYABLE_FAILURE\|PERMANENT_FAILURE` | `(run_id, node_id, attempt)` is recorded exactly once |
| `Artifact` | `PROPOSED → VERIFIED → PROMOTED`, or `REJECTED` | only a `PROMOTED` artifact may be another node's input |

`LEASED` exists so that "a worker claimed this and then died" is a *detectable
state* rather than a hang. `RunStore.open_attempts` is the crash signature, and
it is what `resume` reads.

## Why SQLite and not another JSONL file

Every other record in this kit is append-only JSONL, and that is right for a
record. Two things a resumable state machine needs are not expressible in an
append:

1. **Exactly-once.** `(run_id, node_id, attempt)` must be recordable once. That
   is a uniqueness constraint; a file has none.
2. **Read-modify-write under a lock.** Leasing a node — check `READY`, then
   claim — must be atomic against a second `dobby` process on the same run.
   `core/jsonl.py` makes each *append* atomic, which is weaker: two processes
   can both read `READY` and both append a lease.

`sqlite3` is in the standard library, so this costs no dependency. The event log
is the source of truth and is never updated; the `runs`/`nodes`/`attempts`/
`artifacts`/`effects` tables are a projection written in the same transaction.
`RunStore.rebuild()` recomputes the projection from the log, so "the log is the
truth" is a testable claim rather than an assertion.

The JSONL trajectory stays exactly where it was. A record you can read and a
state you can resume are different jobs.

## Artifact contracts

A node hands the next node a contract, not prose:

| field | what it stops |
|---|---|
| `input_refs` | an attempt that cannot be reproduced because nobody recorded what it was given |
| `output_schema` | a hallucinated handoff that reads plausibly and parses as nothing |
| `acceptance_checks` | "done" as a declaration rather than a demonstration |
| `side_effect_class` | approval, idempotency and hedging decided by guesswork |
| `promotion_rule` | an unverified draft becoming an input |

The machine promotion rule is fixed and is not configurable at runtime: **schema
clean, every acceptance check passed, and no check that failed to run**. A
threshold a failing run may lower is not a gate.

A check that *could not run here* — a missing binary, a timeout — blocks
promotion. That is stricter than necessary for a linter and correct for the case
that matters: a machine without the test runner would otherwise promote an
unverified patch and report it as verified.

## Side effects

| class | approval | idempotency key | may be hedged |
|---|---|---|---|
| `NONE` | no | no | yes |
| `LOCAL_WRITE` | no | no | no |
| `EXTERNAL_REVERSIBLE` | no | **yes** | no |
| `EXTERNAL_IRREVERSIBLE` | **yes** | **yes** | never |

`idempotency_key = sha256(run_id, node_id, effect_version)` — derived from
identity, not content, so a retry that rewords the same email still collides.
The key is claimed **before** the effect. A crash in the window between claiming
and acting leaves a claimed-but-unconfirmed effect, which the run reports and a
human resolves. The other ordering's failure mode is an invisible duplicate.

`max_irreversible` defaults to **0**. A run acquires the right to do something
irreversible explicitly; it does not inherit it from having been started.

## Failure classes

`retry_count` answers "how many times has this broken" and never "is trying
again the thing that could work". The class picks the action; the count only
bounds it.

| class | action | why |
|---|---|---|
| `TRANSIENT_PROVIDER` | retry same, exponential backoff | not about this prompt |
| `CAPACITY` | retry elsewhere | rate limits do not clear by asking harder |
| `CONTRACT_VIOLATION` | repair, avoid that provider | resending the identical prompt to the identical model cannot fix a shape |
| `QUALITY_FAILURE` | repair with the failure text | the failure *is* the instruction |
| `POLICY_BLOCKED` | wait, costs no attempt | a node waiting for a human has not failed |
| `NON_RETRYABLE` | fail | nothing about repeating changes the outcome |

An **unrecognised** provider failure is classified `NON_RETRYABLE`. Pattern
matching on error prose is fragile, and it is fragile in the safe direction: an
unmatched transient fault stops the run and prints the provider's own words. The
opposite default spends the budget on a permanently broken call and reports
"3 attempts failed" instead of the reason.

Authentication failures are never transient. A retry loop against an expired
login is the classic way to spend a budget on nothing.

## Using it

```bash
# a durable run: plan -> execute -> verify -> report
python -m dobby.cli runtime run "add rate limiting to the upload endpoint" \
    --provider claude \
    --execute "pytest -q tests/test_ratelimit.py" \
    --check "pytest -q|ruff check ."

# kill the process at any point, then:
python -m dobby.cli runtime resume <run_id>     # finished nodes are not re-run

python -m dobby.cli runtime status <run_id>     # nodes, attempts, artifacts
python -m dobby.cli runtime events <run_id>     # the append-only log
python -m dobby.cli runtime list
```

Without `--provider` or `--execute` the graph runs on the `static` worker: a dry
run that exercises the kernel — leases, promotion, resume — and spends nothing.

## What this does NOT do

Stated rather than implied, because a runtime that quietly does less than its
diagram is worse than a smaller diagram.

- **No provider scoring or scheduling.** Selection is dependency order, then
  declaration order. A utility function over quality, cost and p95 latency needs
  per-node outcome data that does not exist until runs have been recorded, and a
  policy fitted to no data is a random policy with a formula in front of it. The
  store now records exactly what such a policy will need.
- **No hedged execution.** The `hedgeable` predicate exists and nothing consults
  it yet.
- **No parallel node execution.** The lease is atomic and two processes can
  safely work one run, but the loop itself runs one node at a time.
- **No cost accounting.** `RunBudget.max_cost_usd` is enforced against
  `cost_spent`, and nothing charges it yet — `spend.py` measures agent *time*,
  not money.
- **No semantic verifier layer.** Deterministic and grounded checks only. Model
  judgment stays where `docs/EVAL_DESIGN.md` puts it: advisory, invoked as an
  ordinary node so it costs a visible provider call.
- **Only the linear default graph is built.** `TaskGraph` is a general DAG and
  `default_graph` produces four nodes in a line. Parallel implement-A /
  implement-B with a merge node is expressible today and is not yet assembled by
  anything.

## Evidence

`tests/test_runtime.py`. The tests that matter are not the ones asserting a
happy run succeeds:

- `test_work_that_finished_before_the_kill_is_not_repeated` — three nodes, each
  appending one line to a file. A subprocess runs two of them and calls
  `os._exit(1)`. A second process resumes. The file has **three** lines, and
  each node has exactly one recorded attempt. The line count is the measurement:
  work that ran twice cannot hide from it.
- `test_a_process_killed_mid_node_leaves_a_recoverable_run` — a real process is
  killed while a node is running. The run is left with an open attempt, and the
  next runner closes it, frees the lease, and reports the interruption.
- `test_an_unverified_artifact_never_reaches_the_next_node` — a node whose
  output fails its schema is `FAILED`, its dependent is `SKIPPED`, and nothing
  is promoted.
- `test_an_external_effect_is_performed_once_across_two_runs`,
  `test_a_claimed_but_unconfirmed_effect_is_reported_not_repeated`.
