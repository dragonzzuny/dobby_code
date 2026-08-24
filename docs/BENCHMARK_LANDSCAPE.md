# Benchmark landscape: what could score this harness

Checked 2026-08-24. Sources at the end; every published figure is attributed to
the work that produced it and none of them was measured here.

`docs/EVAL_DESIGN.md` already argues *why* harness evaluation is the right frame
and cites some of this prior art. This file is the other half: a catalogue of the
specific instruments, what each one's unit of evaluation actually is, and whether
it can be pointed at dobby. It exists because one benchmark was adapted, run, and
found not to discriminate — see "What was run" below — and the cost of that was
paid for want of exactly this table.

## The one question that sorts them

Every benchmark here scores an agent. They differ in **what they hold fixed**, and
that single property decides whether a result says anything about dobby:

| holds fixed | varies | what a difference means |
|---|---|---|
| the turn structure | the model's answer | a claim about the MODEL |
| the model | the harness | a claim about the HARNESS — dobby's claim |
| the harness | the orchestrator model | a claim about MANAGEMENT ability |

dobby changes the turn structure — it decomposes a task across nodes and
providers. A benchmark that fixes the turn structure cannot see it, no matter how
many scenarios are run. That is not a flaw in the benchmark; it is a mismatch of
units, and it is checkable before an adapter is written.

## The catalogue

| name | unit of evaluation | reports cost/tokens | runnable here | fit |
|---|---|---|---|---|
| Claw-SWE-Bench | **harness** (a "claw") | yes, per instance | no — needs Docker | **direct** |
| HAL | model x scaffold x benchmark | yes, accuracy-vs-cost Pareto | not established | framing |
| The Harness Effect | harness (one pair) | yes, decomposed input axes | no public suite | axes + citation |
| SWE-bench Verified | whatever you point at it | you instrument it | **yes, in use** | direct, but self-scored |
| ClawArena-Team | orchestrator **model** | yes, API cost | not established | inverse unit |
| DecisionBench | orchestrator **model** | yes, USD/task | not established | inverse unit |
| OrchestrationBench | one model turn | no (added here) | **yes, adapted** | **none — measured** |
| Stop Comparing... | — (position paper) | — | — | reporting standard |

### Claw-SWE-Bench — the direct fit

Its premise is this repository's problem stated by someone else: "the resolved
rate therefore conflates three causally distinct factors: the evaluated LLM, the
harness that turns the LLM into an agent, and the task instances being solved."

- **Unit**: the harness. Adapters live in `claw_swebench/claws/` and implement
  `BaseClawAdapter` (`container_run_args`, `send_task`, plus optional lifecycle
  hooks), registered in `__init__.py` with defaults in `config.py`. dobby would
  be one more claw.
- **Primary metric**: Pass@1 — fraction of instances whose submitted patch the
  SWE-bench evaluator marks Resolved.
- **Secondary**: total API cost (USD), wall-clock per instance, token counts,
  turn counts, and **Cache Hit Rate** = cache-read / (input + cache-read).
- **Size**: 350 instances, 8 languages, 43 repositories — 300 from
  SWE-bench-Multilingual, 50 from SWE-bench-Verified-Mini. An 80-instance Lite
  subset exists for cheap iteration.
- **Licence**: MIT.

Two published spreads, on different populations — quoted separately because
folding them together would report one number as the other:

- across the five full claws on a fixed GLM 5.1 backbone: **60.9% to 73.4%**
  (12.5 pp);
- adding the minimal direct-diff adapter as a floor: **19.1% to 73.4%**
  (54.3 pp), as cited in `docs/EVAL_DESIGN.md`.

Why it is the best available instrument for dobby's claim: it discriminates
between harnesses at a spread already demonstrated, and **Cache Hit Rate is a
metric this repository did not invent** — which is the specific weakness of every
methodology number produced so far. `evals/orchestration/dobby_model.py` says it
plainly: the harness defines the fields, fills them, and grades itself on them.

### HAL (Holistic Agent Leaderboard) — how to STATE the claim

ICLR 2026. Three-dimensional analysis over models, scaffolds, and benchmarks;
21,730 rollouts over 9 models and 9 benchmarks in coding, web navigation,
science, and customer service, about $40,000 and 2.5B tokens, all agent logs
released.

Its value here is the **reporting form, not the run**: accuracy-versus-cost
Pareto frontiers. "Uses fewer tokens" is a weak claim on its own — see
ClawArena-Team below for the published result that breaks it. "Same or better
quality at lower spend" is a point on a Pareto frontier, and that is the shape
this repository's claim has to take.

NOT VERIFIED: whether a public harness exists for submitting an external agent.
The abstract does not say and the full paper was not read.

### The Harness Effect — the token axes, already published

Not a benchmark: the 22-task evaluation is proprietary and no public suite is
released. It matters for two reasons.

First, its input-token decomposition is the same shape as
`dobby/providers/usage_axes.py`:

    T_in = S [system] + H [history] + G [tool schemas] + R [retrieval] + U [user]

with cache hits versus fresh reads, history replay at O(k^2) under naive replay
against linear under compaction, and tool-schema broadcast overhead all tracked
separately. The four axes used here — `prefix_write`, `prefix_reread`,
`fresh_input`, `generated` — are not an invention of this repository.

Second, it supplies a comparable published figure: swapping the orchestration
layer with models held constant cut token intensity **38% at quality parity**.
It also names the failure mode "token maxing" — buying quality with monotonically
growing token spend at declining marginal returns.

Its own stated limits: one baseline against one harness, vendor-specific cache
behaviour, and Table 1's architectural comparison is architectural rather than
measured.

### SWE-bench Verified — what is in use, and its one weakness

`evals/swebench/` drives real instances with gold-patch calibration
(`local_resolve.py`), a billing mode that refuses to invent rates
(`billing.py`), and method metrics (`method_metrics.py`). It is the only
instrument here that measures dobby doing the thing it exists to do: real
repository, real file effects, ground truth in the gold patch, and the turn
structure left open for the harness to decide.

Its weakness is circularity — the scoring is this repository's own. Claw-SWE-Bench
is the same task family with a third party holding the rubric, which is the
upgrade path.

### ClawArena-Team — inverse unit, and one finding that constrains the claim

Measures a single LLM's management ability as a leader directing subagents:
Subagent-Management Score = task correctness x a least-privilege and
modality-routing factor. The harness is fixed and the orchestrator model varies —
the inverse of dobby's experiment — so it cannot score this harness.

It is in this table anyway for one published result:

> cost and management quality are decoupled (API cost spans over 100 times while
> the overall score spans under 4 times)

**A cost reduction is therefore not by itself evidence of better orchestration.**
Any claim made from this repository has to be a joint one — quality at parity or
better AND lower spend — which is why HAL's Pareto form is the right frame.

Its least-privilege term does have a counterpart here: `dobby/runtime/workers.py`
derives tool scope from `node.contract.side_effect_class` in `tools_for(node)`.
Whether that scores well is unmeasured.

### DecisionBench — inverse unit, useful process metrics

Emergent delegation in long-horizon workflows. The orchestrating model is the unit
under test; it delegates through a fixed `call_model` / `read_profile` interface
to an 11-model peer pool. Reference sweep n=23,375 task instances over GAIA
(133 Stage-2), tau-bench (132), BFCL (160) per model. Artifacts on HuggingFace,
CC BY 4.0.

Not our unit, but its **process-level** metrics are worth borrowing:
delegation fidelity@k (did the delegation pick the best-qualified peer),
vendor self-preference (does an orchestrator over-delegate to same-vendor peers),
and a counterfactual ceiling (unrealised improvement). The second one is directly
testable here and this repository has a stake in it —
`dobby/swarm/diversity.py` exists to catch correlated second opinions.

### Stop Comparing LLM Agents Without Disclosing the Harness — the standard

A position paper, not a benchmark: no dataset, no public artifacts found. It
argues that "performance variance is governed more by harness configuration than
by model choice, and current evaluation protocols therefore systematically
misattribute harness-level gains to model improvements", and proposes a
disclosure standard plus a variance-decomposition protocol. Already cited in
`docs/EVAL_DESIGN.md`. Relevant here as the rule every row above should be
reported under.

### OrchestrationBench — adapted, run, does not discriminate

kakao/OrchestrationBench, Apache 2.0. 219 EN and 222 KO scenarios over 17 domains
with ~100 virtual tools; Plan scored by networkx graph edit distance, Call
Rejection by confusion matrix, FC by name/key/value F1. `--skip-llm-eval` gives a
judge-free path. Adapter at `evals/orchestration/`.

**Measured 2026-08-24, scenarios 1-10, both arms, artifacts in the run directory
alongside `COMPARISON.json`:**

| arm | Plan | CallRej | FC* | Avg | scen | calls | tokens |
|---|---|---|---|---|---|---|---|
| dobby_loop | 86.64 | 70.24 | 52.38 | 69.75 | 10 | 36 | 741,298 |
| dobby_claude | 89.14 | 70.24 | 43.28 | 67.55 | 10 | 36 | 741,344 |

FC* excludes the LLM-judged argument-value component and is not the published FC.

The two arms are the same execution. Call counts are identical on all ten
scenarios, per-scenario token deltas are within +/-3.5%, and dobby's ledger
records 72 claude calls on one model with **zero calls to codex or agy** — the
contract gate never fired, because Claude produced a readable workflow on all 36
turns. The metric differences are run-to-run variance of one configuration.

What that bought, and it is not nothing:

- **A noise floor.** Two runs of one configuration differ by Plan +/-2.50 pp,
  FC +/-9.10 pp, Call Rejection +/-0.00 pp, Average +/-2.20 pp. On this benchmark
  a smaller difference than that is not a signal. n=2, so this is a lower bound
  on the variance, not an estimate of it.
- **A sanity check.** Plan 86.64/89.14 sits near the published 83.42 for
  claude-opus-4-7, so the adapter is not mangling answers. n=10 against 219.
- **A defensible negative.** "Adapted a third-party rubric, it does not
  discriminate, here is why" is a stronger line than never having tried.

Why it could not work, statable in advance: the benchmark's unit is one model
turn against a gold DAG, and the only part of dobby that fits such a unit is the
contract retry gate — a failure-recovery device, on scenarios where the primary
does not fail. Running all 219 would not change this.

## What this machine cannot do

Measured 2026-08-24 on the host this repository is checked out on:

    docker    command not found
    uv        command not found
    python    3.11.9        (Claw-SWE-Bench specifies a standalone 3.12)
    disk C:   30G free of 222G (87% used)

Claw-SWE-Bench needs Docker with prebuilt SWE-bench instance images, the official
SWE-bench harness in a separate venv, and per-claw runtimes. **It cannot run here
today.** Installing Docker and a second Python is a package install and therefore
an escalation, not a decision to be taken inside a task. 30 GB of free space is
tight even for the 80-instance Lite subset unless images are pulled and dropped
per instance.

Its cost recording is also conditional: usage metadata lands in each instance's
`metadata.json` "where available", and provider support varies. dobby's own
ledger (`dobby/spend.py`) already keeps the vendor's figure and a null where a
subscription provider reports none, which covers that gap.

## Where this leaves the evidence

The claim being argued — decompose, delegate to sub-models, verify, and get equal
or better output for fewer tokens — currently rests on `evals/swebench/`, which
this repository both runs and scores. Claw-SWE-Bench is the same family of task
with the rubric held by someone else, and it is the single highest-value
unblocking item in this file. What stands between here and it is Docker and disk,
not design.

Sources: [Claw-SWE-Bench](https://arxiv.org/abs/2606.12344) ·
[claw-swe-bench (MIT)](https://github.com/opensquilla/claw-swe-bench) ·
[Holistic Agent Leaderboard](https://arxiv.org/abs/2510.11977) ·
[The Harness Effect](https://arxiv.org/html/2607.06906v1) ·
[ClawArena-Team](https://arxiv.org/abs/2606.31174) ·
[DecisionBench](https://arxiv.org/html/2605.19099) ·
[Stop Comparing LLM Agents Without Disclosing the Harness](https://arxiv.org/abs/2605.23950) ·
[kakao/OrchestrationBench](https://github.com/kakao/OrchestrationBench)
