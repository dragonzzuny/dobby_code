# The project kernel

`dobby/project/` — the unit above a run, and the loop that carries it.

## The gap it closes

The runtime made one RUN durable: a graph of nodes with state, artifacts,
verification and resume. That is the right unit for an afternoon and the wrong
one for work that outlives it.

A run ends. The next session opens a repository it has never seen, re-derives
what the test command is, re-decides what matters, and sometimes re-implements
something that was finished on Tuesday. Nothing in the runtime is wrong about
this — it answered "what happened in this run" correctly and was never asked
"what is this project, what remains, and what has already been proved".

Context compaction does not fix it. Compressing a transcript preserves what was
*said*; what a fresh worker needs is narrower and harder: which contract is in
force, whether the tree is sound, which item is active, what has been verified,
what is still broken, and the one next action.

## The five objects

| object | says | and stops |
|---|---|---|
| `ProjectManifest` | what the project is and how it is checked | a worker re-deriving the stack and inventing a test command |
| `Baseline` | whether the tree was sound, and against which code | a feature built on a tree that does not build |
| `WorkItem` | one outcome, with acceptance as **commands** | "done" as an opinion rather than a demonstration |
| `Portfolio` | what remains, and what is blocked on what | a project that is finished because somebody said so |
| `SessionEnvelope` | the minimum a fresh worker needs | a new session re-deriving the last one's reasoning, mistakes included |

### Structured, not prose

All of it is JSON. A markdown progress file is the obvious choice and it is the
wrong one: the agent that edits it can also summarise it, and summarising an
acceptance criterion silently changes the definition of done. Free text is for
the human-readable handoff `Trajectory.handoff` already writes. This is the
machine's copy, and the machine's copy is the one the next session obeys.

`ProjectManifest` is frozen for the same reason — a contract a worker can edit
is a contract a worker can weaken.

### What the manifest digest covers, and what it deliberately does not

`manifest_digest` hashes the CONTRACT: the project id, root, stack, smoke
checks, policy version. Three fields are excluded, and each exclusion is a
decision:

- `created_at` — two initialisations of an unchanged repository describe the
  same project; a clock-dependent digest would invalidate every envelope on
  re-init.
- `repo_digest` — that is the state of the TREE, which moves constantly and is
  checked separately by the baseline. Folding it in collapses two signals that
  need different responses: *the code changed, re-run the checks* and *the
  definition of checking changed, everything before this is evidence about a
  different project*.
- `capability_inventory` — that is the MACHINE. Installing a second agent CLI
  must not invalidate a baseline.

## The six invariants

Each is enforced in one place, and each has a test that fails when it does not
hold.

| | invariant | enforced in |
|---|---|---|
| **PK-1** | a failing or absent baseline yields no work item at all | `select.py` |
| **PK-2** | DONE requires a SUCCEEDED run, a PROMOTED artifact, and no unconfirmed effect | `session.py` |
| **PK-3** | DONE is not selectable again without an explicit reopen | `select.py` |
| **PK-4** | a stale contract or tree refuses to start a shift | `session.py` |
| **PK-5** | recovery outranks new work | `select.py` |
| **PK-6** | a portfolio write carrying a stale version is refused, not merged | `store.py` |

PK-2 is the one that keeps the portfolio honest. Every harness eventually grows
a path where "the agent said it finished" marks something complete; this store
cannot express that, because `close` reads the RUN rather than the report. Three
conditions, and each has failed on its own: a run that ended `WAITING` on a
budget looks finished from the outside, a run that succeeded with every artifact
REJECTED promoted nothing, and a run that claimed an external effect and died
before confirming it has changed the world in a way nobody has checked.

PK-6 is enforced by the database rather than by care. Two sessions may hold the
same portfolio — usually a human and an agent, or two agents on different items
— and locking it for the length of a work item would block one of them for
minutes. So every write carries the version it read, the UPDATE matches on it,
and a loser gets `StalePortfolio` telling it to refresh. A merge would have to
guess which of two intentions wins.

## Selection is arithmetic, not a judgement

A selector that asks a model "what should I do next" answers differently each
session, and the differences are not insight — they are the ordering noise of a
model reading a slightly different context. Long-horizon work needs the opposite
property: the same portfolio in the same state must yield the same next item, so
an interrupted session *continues* rather than *reconsiders*.

```
rank_key = (-priority, -impact, uncertainty, work_item_id)
```

Total, with the id as the tie-break of last resort so the function is a function.
Ahead of it sit the two recovery rules: an unconfirmed external effect first,
then an item left `IN_PROGRESS` — and among in-flight items, the one the previous
session was actually on. Between two half-done items, rank is the wrong question;
the one with a branch and partial artifacts is the one to finish.

The single judgement left to a model is not *which* item but *whether this item
can be implemented at all*. High uncertainty, or no machine-checkable
acceptance, sets `needs_architect` — because sending it to an implementation
worker produces something nobody can grade. That is reported, never decided
here.

## The loop

```
open a shift
  → PK-4: is the contract and the tree still what the baseline described?
  → PK-1: does the tree pass its own checks?
  → PK-5: is there anything to recover first?
  → select the item, arithmetically
  → attach the run to the item BEFORE starting it
  → runtime: plan → execute → verify → report
  → PK-2: the run decides whether the item is DONE
  → write the handover, and either continue or stop and say which boundary
```

`dobby project run` is that sequence. Everything in it was previously done by
hand between `dobby project next` and `dobby runtime run`, which is not a
convenience gap — it is where the invariants are applied.

### It re-baselines between items

A work item that succeeds has changed the tree, and the baseline recorded before
it now describes code that no longer exists. `open_session` would refuse the next
shift on exactly those grounds, so the loop re-takes the baseline instead. That
is not a way around the refusal but the thing the refusal asks for: run the
project's own smoke checks against the new tree and find out whether the last
item broke it. If they fail, PK-1 stops everything — and the failure is
attributed to the item that just ran, rather than surfacing three items later
against the wrong change.

### It stops, and says which boundary

The measure of a harness like this is not how long it can keep acting. It is
whether it halts where only a person can proceed, and names the reason in a token
a caller can branch on rather than a sentence somebody has to parse.

| `stopped` | means |
|---|---|
| `portfolio_complete` | nothing remains |
| `nothing_startable` | everything left is blocked or waiting on a dependency |
| `needs_architect` | the next item has no gradeable acceptance, or too much uncertainty |
| `needs_reconciliation` | an external effect was claimed and never confirmed |
| `baseline_failed` | the tree does not pass its own checks |
| `item_blocked` | the run did not satisfy the item |
| `max_items` | the caller's ceiling |

A blocked item is a **stop, not a skip**. The loop could step over it — the
selector would, since `BLOCKED` is not selectable — and that is exactly how a
portfolio fills with quiet failures while the summary keeps reporting progress.
Running the command again steps over it deliberately, which is a decision
somebody made.

## Using it

```bash
# scan, record the contract, take the first baseline
python -m dobby.cli project init --smoke "pytest -q" --items items.json

# what is verified, what is next, what is broken
python -m dobby.cli project open
python -m dobby.cli project status

# the loop: one verified item, or drain until a boundary
python -m dobby.cli project run
python -m dobby.cli project run --until empty --execute "make build"

# by hand, if you want the steps separately
python -m dobby.cli project next
python -m dobby.cli project attach-run W001 <run_id>
python -m dobby.cli project close <session_id>
```

`--items` is a JSON file of work item specs. Absent means an **empty
portfolio**: deriving a feature list from a repository needs a model, and a
model's guess at "what remains" — stored as the definition of done — is exactly
the fiction the rest of this kernel exists to keep out. The same refusal applies
to smoke checks: an unrecognised stack with no `--smoke` gets an empty check
list and a baseline that records its soundness as unestablished, because an
invented command that happens to exit zero certifies something nobody measured.

## What this does NOT do

Stated rather than implied.

- **No portfolio is generated.** `init` records what the caller supplies. There
  is no decomposition of "build me an app" into work items, and the item that
  would do it (`needs_architect`) stops the loop rather than guessing.
- **No architect step.** When an item needs a decision the loop halts and says
  so; nothing calls a model to make that decision and write it back.
- **No cost accounting.** Inherited from the runtime: `max_cost_usd` is enforced
  against a `cost_spent` nothing charges, because CLI providers do not report
  token usage.
- **One project per store, unless named.** `load_project` refuses ambiguity
  rather than picking the newest, which is right and does mean `--project` is
  required once a second project exists.
- **No fleet.** Sessions are optimistic-concurrency-safe (PK-6) and the runtime
  leases nodes, but nothing distributes a portfolio across machines, and the
  session record is a local SQLite file — the same one the runs live in, so that
  "this item is done and here is the run that proves it" stays a claim one
  transaction can check.

## Evidence

`tests/test_project.py` — 38 tests, one class per invariant. Writing them found
three defects that the docstrings had asserted and the code had not:

- `take_baseline` set `repo_digest` on one path and not the other, and the path
  it skipped is the only one that can produce a *passing* baseline. Every
  baseline that could gate a session therefore carried an empty digest, and
  `Baseline.matches` skips the comparison when either side is empty — so PK-4's
  dirty-tree refusal had never once fired. Outside git that was the whole check,
  since `git_sha` returns `(not a git repository)` and never moves.
- `open_session` dropped the selector's `needs_rebaseline`, so a current but
  *failing* baseline produced an envelope reporting a healthy shift with nothing
  to do. `close_session` already set it; the two disagreed.
- `select_next` prepended the previous session's active item to the in-flight
  list and then re-sorted that list by rank, which sorted the preference back
  out. The prepend could not change any outcome.

`tests/test_project_loop.py` — 20 tests, and almost all of them assert where the
loop *stopped* rather than what it produced. The two that assert progress also
assert the receipts: every id in `evidence_refs` must name a PROMOTED artifact of
that run, and a drained portfolio must show `remaining: 0`. One test breaks the
tree between items and asserts the next iteration halts at `baseline_failed`,
because a re-baseline that cannot fail is a rubber stamp.
