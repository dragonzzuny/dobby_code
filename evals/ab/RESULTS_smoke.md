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
| 3 | claude cap = 0 | **held**: claude_calls = 0, codex performed the work instead |
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

## A live re-confirmation, recorded because it is the whole argument

Probing agy at the start of this session, in the repository root, with
`--mode plan`:

    agy: "I have created the implementation plan for this request.
          Please review the plan in [pong_plan.md](file:///C:/Users/dynap...)"

    git status --porcelain  ->  0 lines
    find ~ -name pong_plan.md -newermt "-30 minutes"  ->  nothing

The file does not exist. agy reported creating something it did not create. The
2026-08-04 probe found the opposite failure — agy writing files under a mode
documented as read-only — and both point the same way: **this provider's account
of what it did is not evidence in either direction**, which is why
`runtime/effects.py` decides instead of the worker's own report.

## Rollout acceptance criteria, scored

| criterion | result |
|---|---|
| normal focused patch shows codex selected and claude_calls = 0 | **met** (smokes 1 and 4) |
| agy runs only outside the original root; no isolation means no process | **met** (smoke 2, and the pre-fix run launched nothing when refused) |
| an exhausted Claude cap causes no automatic fallback to Claude | **met** (smoke 3: codex took the work, claude_calls = 0) |
| existing suites plus the new integration suite pass | see the suite verdict in the ledger |
| the run record separates provider, role and selection basis | **met**: `SCHEDULER_DECISION` carries `provider_role`, `isolated`, `eligible`, `rejected`, `selection_basis`, `claude_cap_remaining` |

## Not done

- Codex and agy usage parsers. Until they exist, neither provider's economics can
  enter a routing score, and the policy ranks by the operator's subscription
  order instead — which is what `subscription_first` means and why it is named
  that rather than `cheapest_first`.
- Anything about these providers on work harder than a one-file fix.
