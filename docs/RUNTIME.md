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

### Leases: who holds a node, and who may take it back

The claim is a single `UPDATE ... WHERE state='READY'`, so two workers racing for
one node produce one winner. That was never the hard part. The hard part is
recovery: an attempt with no finish means *either* the process died holding it
*or* another worker is running it right now, and those want opposite treatment.
Recovering both — which an open attempt alone justifies — hands a second worker
the node the first is still executing, reintroducing the duplicate the lease
exists to prevent, from inside the code that repairs crashes.

So a lease records `lease_owner` (`host/pid`) and `lease_expires`, written in the
same statement as the state. `lease_is_held` returns True only when all of these
hold — and False whenever the evidence is missing or unreadable, because a run
that stalls forever is worse than one that reclaims a node whose owner cannot be
identified:

- the owner string is one this runtime wrote (`host/pid`),
- the lease has not expired,
- the owner names **this** host — another machine's PID cannot be probed, so an
  unexpired lease there is evidence *for* the holder and recovery waits out the
  TTL,
- and that PID is running. `core/platform.process_alive` answers in three values,
  never two: on Windows it reads the exit code rather than `os.kill(pid, 0)`,
  which on that platform would *terminate* the process it was asked about.

TTL is the node's own timeout plus a margin, not a heartbeat. A node cannot
legitimately run longer than its timeout, so a second liveness mechanism would be
a second thing to keep correct. Expiry beats liveness in both directions: it
bounds PID reuse, and it bounds a holder that hangs forever.

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
The key is claimed **before** the effect. The other ordering's failure mode is an
invisible duplicate.

A crash in the window between claiming and acting leaves a claimed-but-unconfirmed
effect. An effect therefore has **three** states, and the middle one is the point:

| `effect_status(key)` | means | what resume does |
|---|---|---|
| `None` | never claimed | claim it and perform it |
| `CLAIMED` | intent recorded, outcome never observed | **block the node** |
| `CONFIRMED` | it demonstrably happened | finish the node without repeating it |

`CLAIMED` is not a slower `CONFIRMED`. The effect may have reached the outside
world and may not, and nothing on this machine can decide which — so both
automatic answers are wrong. Repeating it risks a second real-world effect;
calling it done reports a success nobody observed. The node goes to
`BLOCKED_ON_APPROVAL`, the run to `WAITING`, and the run names its two exits:

```python
store.confirm_effect(key, result_digest="checked the outbox; it went")
store.release_effect(key, reason="checked the outbox; it did not")
```

Resume acts on whichever was recorded — confirm finishes the node as an
idempotent no-op, release lets it perform the effect after all. `release_effect`
refuses a `CONFIRMED` effect, because releasing one of those is how the same mail
gets sent twice.

What is still **not** automated: asking the effect provider itself. An
`EffectAdapter.status(key)` that queried the mail API or the deploy service would
resolve most `CLAIMED` effects without a human. Until it exists, a human is the
lookup.

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

## Observation: one trace per run

Every span carries the ids that let it be joined to every other — `trace_id`
(the run), `parent_span_id`, `run_id`/`node_id`/`attempt`, `policy_version`,
`prompt_version`, `provider`/`model`. Field names follow OpenTelemetry, and
`to_otlp()` renders the OTLP JSON shape so "OTel-compatible" is checkable rather
than a word in a docstring. Nothing is sent anywhere: the engine makes no
network calls.

| span kind | the question it exists to answer |
|---|---|
| `orchestrator.plan` | why did this task go to N steps? |
| `scheduler.decision` | why was this node admitted, and this provider chosen? |
| `agent.generation` | which model, how long? |
| `tool.call` | which tool failed or looped? |
| `retrieval` | did the wrong answer start here? |
| `verifier` | which acceptance criterion breaks most? |

Each kind declares the attributes without which it cannot answer its question,
and `Tracer` enforces them at write time. A span that violates is still written,
with the violation recorded in it — losing an observation to enforce a rule
about observations would be its own defect.

**The clock.** `time.time()` has ~15.6ms resolution on Windows, which is longer
than most spans in a run. Measured on the first traced run here: the root `run`
span and the first event inside it got the same timestamp, so `ORDER BY
started_ms` put a child before its own parent. Timestamps are now a wall-clock
anchor advanced by `perf_counter`, which keeps the absolute value real and makes
two spans a microsecond apart distinguishable.

## Placement: which provider, from what was measured

Separate from the router on purpose. The router answers a policy question once —
how much agency, which tier, what budget — and should not change because a
provider is rate-limited this afternoon. Placement answers a runtime question
every time a node starts, and must.

    U(p|n) = wq·q̂(p,n) − wc·ĉ(p) − wl·l̂(p,n) − wr·r̂(p,n)

`q̂` is the share of that provider's attempts on that node kind that survived the
**verifier** — not that exited zero. Optimising for exit codes selects for
providers that answer fast and wrongly. `ĉ` is the catalog's cost tier
normalised, an ordering and never money. `l̂` is p95 against the slowest
candidate. `r̂` is the recent-failure signal the circuit breaker also reads.

An unmeasured provider scores the optimistic prior (`UNKNOWN_PRIOR = 0.75`) for
quality and the *typical* measured latency — not zero. Scoring it zero made
"never tried" the best possible latency, stacking a second advantage on the
prior; measured on the placement tests, a provider with no record beat one with
a 0.9 success rate and a p95 five times better. Exploration comes from the prior
alone, and there is exactly one comparison over all candidates rather than a
separate "should I explore" branch.

**Circuit breaker.** Three consecutive verifier-failing attempts open it for
120s, then one half-open probe. Held in memory, not in the store: the failures
that trip it are usually local (auth, a proxy, a rate-limit window tied to one
machine), and persisting would turn one process's bad afternoon into a
project-wide ban.

**Concurrency.** Two ceilings — global (protects the machine) and per-provider
(protects the run from a rate limiter that turns excess parallelism into
serialized, billed retries). Acquired both-or-neither, because taking the global
slot then waiting on the provider slot is how a fan-out deadlocks itself.

**Hedging** is computed only for a node whose contract touches nothing outside
the run and which asks for it. Racing a node with side effects sends the email
twice.

## Metrics

`dobby runtime metrics`. The rule every function follows: **a metric with no
data returns `None` and says why.** Zero is a measurement; `None` is the absence
of one, and collapsing them is how a dashboard shows 0% success for a system
nobody has run.

| metric | alarm |
|---|---|
| Task Success@Verifier | 7-day mean 5pp below baseline |
| p50/p95 completion latency | p95 past 1.5× the SLO |
| Cost per verified task | **unmeasurable here, and it says so** |
| Retry amplification | above 1.3 |
| Recovery success rate | anything below 1.0 is a P0 |
| Side-effect duplicate rate | anything but 0 stops everything |

## The flywheel

`dobby runtime harvest`. Failures that recur become **candidates** for golden
tasks — never golden tasks. A repeated failure is evidence that something
recurs, not that the system is wrong: the three most common causes of a repeated
`QUALITY_FAILURE` are a real defect, a broken check, and a task nobody should
have asked for. Promoting automatically would enshrine the second and third as
requirements, and a golden set with a wrong entry is worse than a small one.

Grouping is by `(node_kind, failure_class, signature)`, where the signature has
paths, hashes, times and numbers removed — `timeout after 120s` and `timeout
after 300s` are one failure mode, and counting them separately makes twenty
identical problems look like twenty unrelated ones. Writes MERGE, so a human's
"rejected: the check was wrong" survives the next harvest.

## The benchmark

`dobby runtime bench --corpus <file>`. Three conditions, paired per task:

    baseline   one node, no contract, no gate
    gated      the same step WITH the contract and the checks
    runtime    the full graph, retries classified, artifacts promoted

The middle arm exists because a difference between two arms has at least three
explanations. **It ships no corpus** — a benchmark whose tasks come with the tool
measures its authors' imagination — and it refuses a verdict below eight paired
tasks or when the bootstrap interval spans zero.

## What this does NOT do

Stated rather than implied, because a runtime that quietly does less than its
diagram is worse than a smaller diagram.

- **The hedge is decided and never raced.** `Placement.hedge_with` names a
  partner; nothing starts the second call.
- **No cost accounting.** `RunBudget.max_cost_usd` is enforced against
  `cost_spent` and nothing charges it, because this engine cannot see money.
- **No benchmark result.** The harness exists; no corpus does. Whether the
  runtime finishes more tasks verified than the primitives did is UNANSWERED
  here, not answered weakly.
- **The semantic layer is one advisory judge.** No panel, no cross-examination.
  Model judgment stays where `docs/EVAL_DESIGN.md` puts it: advisory, invoked as
  an ordinary node so it costs a visible provider call, and labelled wherever
  the artifact travels.
- **No effect-provider lookup.** A `CLAIMED` effect is resolved by a human
  calling `confirm_effect` or `release_effect`, and there is no CLI for either
  yet — it is a Python call against `RunStore`. An `EffectAdapter.status(key)`
  that asked the mail API or the deploy service would answer most of these
  without a person; nothing implements one.
- **Leases are single-machine.** Ownership is `host/pid` and liveness is a local
  process check, so a second host sharing the store is answered only by the TTL.
  There is no heartbeat, no fencing token, and no orphan detector running
  independently of a resume.
- **Only the linear default graph is assembled.** `TaskGraph` is a general DAG
  and runs nodes in parallel when asked (`--parallel N`), but `default_graph`
  still produces four nodes in a line. Parallel implement-A / implement-B with a
  merge node is expressible and nothing builds it.

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

`tests/test_runtime_lease.py` covers the two guarantees that only hold when a
worker stops existing partway through:

- `test_a_node_held_by_a_live_worker_is_left_alone` — a second runner attaches to
  a run whose node is leased by a live process. It starts no second attempt. Its
  companion `test_a_node_held_by_a_dead_worker_is_recovered` uses a PID it
  watched exit, and `test_an_expired_lease_is_recovered_even_from_a_live_owner`
  shows expiry overriding liveness.
- `test_an_unconfirmed_effect_blocks_instead_of_reporting_success` — the defect
  this file was written for. The run reports `WAITING`, not `SUCCEEDED`.
  `test_confirming_it_...` and `test_releasing_it_...` drive both operator exits
  through to a finished run, and `test_a_confirmed_effect_cannot_be_released`
  asserts the one that would duplicate an effect is refused.

`tests/test_runtime_injection.py` injects the four faults a runtime has to
survive — provider timeout, worker crash, verify failure, duplicate callback —
and asserts in each that **no external effect is performed twice**. The
invariant is asserted per-fault rather than once, because the point is that it
holds on every path and not on the happy one.

`tests/test_runtime_observability.py` and `tests/test_runtime_placement.py`
cover the trace tree (including that two threads do not become each other's
parents), the metrics' refusal to report zero for absent data, the circuit
breaker's cooldown, and that two independent nodes actually overlap in time
under `--parallel 2`.

One property worth naming because breaking it is silent: **a node spec must
round-trip through the store as JSON**, since the runner executes the graph it
LOADED and not the one passed to `start()`. An object placed in `config` used to
come back as its `str()`; four injection tests then reported a healthy system.
`RunStore` now refuses a non-serialisable spec at `start()`, where the fix is
one line.
