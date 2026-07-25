# Operating manual (generic)

`AGENTS.md` is the always-loaded summary; this file is the full specification.
Machine-readable mirror: `.harness/policies/policies.json` (prose here is
canonical; on conflict fix here first, then the JSON, then record a
`contradicts` edge until synced).

## Task lifecycle

```
1. RESTATE  requirement ledger (below); resolve every name in the request to a
            concrete path/artifact before anything else.
2. BRIEF    `harness.cli context` — fired policies, skills, knowledge, budgets.
3. OBSERVE  run the read-only evidence steps the fired policies require.
4. DECIDE   apply decision rules; if a P-ESCALATE trigger fires, stop and ask.
5. ACT      smallest increment that satisfies the requirement; outputs to new
            locations; project left runnable (or breakage isolated + stated).
6. VERIFY   independent output-side check. No passing check = not done.
7. REPORT   completion protocol (below), then handoff if the session may end.
```

## Requirement ledger

Trigger: >1 requirement, >~10 expected tool calls, every producing task.
`reports/LEDGER_<slug>.md`: requirements verbatim+numbered · assumptions with
L-levels · status table (`todo/doing/done/blocked` + evidence path). Never
pre-fill `done`. This file is the context-recovery checkpoint: on resume, read
the newest ledger + handoff and continue from the first non-done row.

## Hypothesis ledger (investigations)

For any "why is X failing/wrong" task: table
`| hypothesis | discriminating check | result | verdict |`. A hypothesis is
confirmed only by a check whose outcome differs depending on truth. Stop when
confirmed AND the fix location is identified at file level. 3 consecutive
eliminating-nothing checks → re-derive hypotheses from the knowledge graph
instead of inventing more.

## Policy schema

Every policy (prose or JSON) carries: Trigger / Required actions / Forbidden
shortcut / Evidence of completion / Recovery / Escalation. The nine universal
policies are in `.harness/policies/policies.json`; host projects add domain
policies with the same schema. Recall-biased triggers are intentional: a policy
firing needlessly costs a little context; one failing to fire costs an invariant.

## Validation ladder (generic)

| level | check | typical command |
|---|---|---|
| 0 | paths/resources exist; environment capable | ls / which / import test |
| 1 | inventory: counts, sizes, layout | project census script or find/wc |
| 2 | structure: syntax, schema, pairing, contract shape | linters, schema validators, project validators |
| 3 | semantics across artifacts: collisions, consistency, prediction-vs-outcome | project cross-checks, before/after comparisons |
| 4 | end-consumer behavior: build passes, tests green, app serves | build/test/e2e |

Fill the artifact→required-level mapping for the host project during
bootstrap (quality-gate table): each artifact type this project produces gets
measurable pass conditions and a verification COMMAND, never a judgment call.
Levels unavailable in this environment are stated as NOT AVAILABLE — never
claimed.

## Escalation matrix

L0 measurable → run it, never ask. L1 cheap-to-reverse assumption → proceed +
record. L2 safe experiment distinguishes options → declare threshold, run.
L3 ask (fixed format in `.claude/rules/escalation-and-reporting.md`):
destructive/irreversible · external publication · installs · licensed/sensitive
content · >30 min operations · contradictory evidence · no acceptance
criterion · scope growth beyond the ledger. Bundle related questions into ONE
escalation.

## Completion protocol

DONE = every ledger row done-with-evidence · required ladder level passed ON
OUTPUTS · zero writes outside scope · report per P-REPORT. Otherwise open with
"Partially complete" or "Blocked" and say why in sentence one.

## Memory discipline

Six kinds (harness/memory.py). Write episodic lessons after failures (bounded;
the store caps surfacing at 3). Semantic facts only with provenance; a newer
unverified assertion never supersedes an older verified fact. Negative memory:
record rejected approaches with the conditions under which they fail — check it
before re-proposing.

## Maintenance / self-improvement routing

Same mistake twice → fix the harness, not just the instance: missing fact →
knowledge graph · matchable rule → policies.json (+ prose rule) · repeated
procedure → skill (starts as `candidate`; promotion needs ≥2 distinct scenario
passes + a non-proposer approver) · deterministic check → capability/criteria ·
dangerous action → protected_paths / enforcement proposal · tunable behavior →
config via `improve-auto` gates only. Every change: changelog row + (if
behavioral) an eval scenario.
