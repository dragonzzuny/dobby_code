# Real-provider smoke — codex and agy, run for the first time

Executed 2026-08-23. Every number came from `evals/ab/RESULTS_smoke.json`,
produced by `evals/ab/smoke_providers.py`. Nothing is estimated.

Until this ran, every provider figure in this repository came from claude,
because claude was the only provider that had ever been through the harness. The
role policy named codex the default implementer and agy an isolated delegate on
the strength of what their catalogs SAY — which is the kind of claim this session
has repeatedly found to be wrong.

## The four smokes

| # | smoke | result |
|---|---|---|
| 1 | codex direct-gated | **3/3 SUCCEEDED**, one codex call each, **claude_calls = 0** |
| 2 | agy isolated delegate | **2/2 SUCCEEDED**, one agy call each, **original_root_mutations = 0** |
| 3 | claude cap = 0 | **claude excluded at eligibility**: claude_calls = 0, codex dispatched first — see the correction below |
| 4 | policy path, no override | **SUCCEEDED**, codex selected, claude_calls = 0 |

### 1. codex direct-gated

| task | state | calls | wall |
|---|---|---|---:|
| paginate_offbyone | SUCCEEDED | codex ×1 | 36.8s |
| discount_validation | SUCCEEDED | codex ×1 | 33.3s |
| invoice_missing_field | SUCCEEDED | codex ×1 | 29.8s |

Mean 33.3s. The same three fixtures under claude in the D_adaptive arm took
110.3s mean (`RESULTS_adaptive.md`), so **codex was about 3.3x faster in wall
time here.** Same fixtures, same gate, same acceptance command.

### 2. agy isolated delegate

| task | state | calls | changed paths | original root |
|---|---|---|---|---|
| paginate_offbyone | SUCCEEDED | agy ×1 | `paginate.py` (+ pycache) | **unchanged** |
| discount_validation | SUCCEEDED | agy ×1 | `discount.py` (+ pycache) | **unchanged** |

Mean 131.8s. `original_root_unchanged` is a content hash over every file in the
protected tree, not a HEAD comparison — an untracked file written into the
project would leave HEAD identical and this would still catch it.

## What is still NOT measured about either

**Cost and tokens.** Both reported `usage: []`. `usage_extra` is empty for codex
and agy, so `providers/policy.economics` reports `economics_status: unmeasured`
for both, and nothing here says they are cheap. Codex being 3.3x faster in wall
time is a latency measurement and not a cost one.

**Quality beyond three trivial fixtures.** Every task is a known single-file bug
with a pre-written failing test. Nothing here says codex is a better implementer
than claude on real work.

## Two defects this smoke found, both now fixed

### The call recorder was blind to failed calls

The first agy run returned `POLICY_BLOCKED` — exit 1, `permission check failed
for command "Get-ChildItem -Recurse": user denied permission` — and the harness
reported **`calls_total: 0`**. A provider that had launched, spent 19 seconds and
produced nothing was invisible to the counter, and therefore to the Claude quota
that counts through the same recorder.

A cap that counts only successes undercounts precisely the provider that is going
wrong. `providers/run._recorded` now wraps every return path, and a test asserts
a failed call is recorded.

### agy could not use its own tools headlessly

The same failure was agy refusing its own permission prompt with no human to
answer it. The remedy is `--dangerously-skip-permissions`, and it is NOT a
containment control — the four-configuration probe in `providers/catalog.py`
established that years of flags do not contain this CLI.

So it is a new spec field, `isolated_extra`, sent **only** when the scheduler
says the workspace is isolated. Auto-approving a delegate's tools is defensible
exactly where the directory it was launched in is disposable, and nowhere else.
Sending it on the original tree would hand the one provider measured writing
under a read-only mode a free hand in the project.

## A claim I published here and then had to withdraw

This section first said that agy "reported creating something it did not
create". **That was wrong, and the error was mine.**

Probing agy in the repository root with `--mode plan`, it answered that it had
created `pong_plan.md` and linked to it. `git status --porcelain` reported zero
lines, and `find ~ -maxdepth 3 -name pong_plan.md` found nothing — so I wrote
that the file did not exist.

It exists. The link's full path, truncated in the output I first read, is

    ~/.gemini/antigravity-cli/brain/<conversation-id>/pong_plan.md

which is five levels deep and outside the range my search covered. Both probes'
files are there. I searched too shallow and reported the absence of evidence as
evidence of absence — the exact error this repository has a rule about.

**What the probe actually shows, corrected:** agy in `--mode plan` wrote its
artefact into its OWN state directory and left the repository untouched. That is
better behaviour than the 2026-08-04 probe recorded, where agy created a file in
the fresh temp directory it was launched in under all four mode/permission
combinations.

The two observations do not cancel out and neither is retracted. `--mode plan`
is still not a containment control, because the 2026-08-04 result stands: the
directory it is launched in is the only demonstrated boundary. What changes is
that the alarming reading — a provider fabricating a file it never wrote — was
my measurement error, not its behaviour, and the corrected record says so.

The reason `runtime/effects.py` decides rather than the worker's own report is
unaffected, and is now supported by a cleaner example: a provider can truthfully
report writing a file that is nowhere near the tree anyone cared about.

## Rollout acceptance criteria, scored

| criterion | result |
|---|---|
| normal focused patch shows codex selected and claude_calls = 0 | **met** (smokes 1 and 4) |
| agy runs only outside the original root; no isolation means no process | **met** (smoke 2, and the pre-fix run launched nothing when refused) |
| an exhausted Claude cap causes no automatic fallback to Claude | **met, and by the stronger route** — see below |
| existing suites plus the new integration suite pass | see the suite verdict in the ledger |
| the run record separates provider, role and selection basis | **met**: `SCHEDULER_DECISION` carries `provider_role`, `isolated`, `eligible`, `rejected`, `selection_basis`, `claude_cap_remaining` |

## A correction: smoke 3 was pre-dispatch routing, not a runtime fallback

The first draft of this file said "claude cap = 0 held: codex performed the work
instead" and, one table down, "an exhausted Claude cap causes no automatic
fallback to Claude". Read together those contradict each other, and the wording
hid which of two very different things happened.

What happened: **claude was removed at the ELIGIBILITY stage and codex was the
first provider ever dispatched.** No claude process was launched, nothing failed,
and nothing fell back.

    ProviderPlacement.eligible()
      claude  -> rejected: "claude has spent 0/0 calls this session;
                            the cap is the operator's budget and is not a
                            tie-break"
      codex   -> eligible
    selection_basis: subscription_first_static_preference
    calls_by_provider: {"codex": 1}

The distinction matters and is a policy difference, not a phrasing one:

| | what it means | what it costs |
|---|---|---|
| runtime fallback | claude was chosen, launched, failed or was refused, and another provider was tried | a launched call, its latency, and whatever it spent before failing |
| **pre-dispatch routing** | claude was never a candidate; the cap was applied before any process existed | nothing |

A cap enforced only as a runtime fallback would still spend the call it was
meant to prevent. This one is applied in `eligible()`, before scoring and before
dispatch, which is the only place it can cost zero.

## Not done

- Codex and agy usage parsers. Until they exist, neither provider's economics can
  enter a routing score, and the policy ranks by the operator's subscription
  order instead — which is what `subscription_first` means and why it is named
  that rather than `cheapest_first`.
- Anything about these providers on work harder than a one-file fix.

## The standing of the "codex is the default implementer" claim

**Smoke-verified only.** Three known single-file bugs, each with a pre-written
failing test, each fixed in one call. That is the easiest shape of work there is,
and a provider that is fast and accurate on it may still take many more calls on
a multi-file change or an unfamiliar module — which is exactly what the S1–S4
corpus is for.

Until that runs, the accurate phrasing is: *codex is the default implementer by
policy, and has been verified on one-shot single-file fixtures.* Not "codex is
the better implementer".
