# End-task evaluation: design

## Prior art, and what it means for this repository

Harness evaluation is an established subfield, and the numbers in it are not
marginal. Checked 2026-07-26; sources at the end.

- **The harness is worth about as much as the model.** *Claw-SWE-Bench* (350
  GitHub issue-resolution instances over 8 languages and 43 repositories, Pass@1)
  reports that on a fixed GLM 5.1 backbone a minimal direct-diff adapter scores
  **19.1%** while the full adapter reaches **73.4%** — a 54.3 pp swing from
  scaffolding alone. Aggregated, model choice moves Pass@1 by **29.4 pp** and
  harness choice by **27.4 pp** under fixed models.
- **Which means an undisclosed harness invalidates a model comparison.** *Stop
  Comparing LLM Agents Without Disclosing the Harness* makes the harness a
  first-class controlled variable. Holding Claude Opus 4.5 fixed and varying only
  the harness spans **45.9% (SEAL) to 55.4% (Claude Code)** on SWE-bench Pro —
  9.5 pp, roughly twice the within-harness model range. HAL reports same-model
  gaps across scaffolds in the double digits.
- **Policy adherence is already a first-class metric somewhere.** *τ-bench* is the
  first agent benchmark to score policy adherence separately from task success, so
  an agent that satisfies the user by violating policy is a partial fail. That is
  the published analogue of what this harness's constitution asks for.
- **Reliability needs `pass^k`, not `pass@1`.** τ-bench introduced `pass^k` — the
  probability that **all** k trials succeed. It decays as p^k: a 90% agent is at
  57% by k=8. A constitution that is followed most of the time is not a
  constitution, so this is the right shape of metric here and it is adopted below.
- **Cost is an axis, not a footnote.** Both Claw-SWE-Bench and the HAL line of
  work report cost alongside accuracy, because equal accuracy at very different
  spend is not equal performance.

Three consequences for this repository, and the third is the uncomfortable one.

1. `pass^k` and cost are adopted as reported metrics rather than invented here.
2. The compliance probe below is τ-bench-shaped and is defensible on that basis.
3. **The definitive validation of this harness is a Pass@1 run on an established
   issue-resolution benchmark with the model held fixed, and it has not been done.**
   That is now the largest open item in this repository, larger than any feature.
   The probe below does not substitute for it and must not be cited as if it did.
   What stands in the way is machinery, not principle: containerised task
   environments, a patch-application path, and per-instance test harnesses — none
   of which exist here.

Sources: [Claw-SWE-Bench](https://arxiv.org/abs/2606.12344) ·
[Stop Comparing LLM Agents Without Disclosing the Harness](https://arxiv.org/abs/2605.23950) ·
[τ-bench](https://arxiv.org/abs/2406.12045) ·
[Beyond pass@1: A Reliability Science Framework](https://arxiv.org/pdf/2603.29231) ·
[CORE-Bench](https://arxiv.org/pdf/2409.11363)

## The problem this exists to fix

This repository contains 39 modules and 911 tests, and until now **none of them
measured the thing it claims**. Every test verifies a mechanism — the tokenizer
handles Hangul, the guard refuses `rm -rf /`, the evaluator excludes advisory
judgments. Those are necessary and they are not the point. The README's claim is:

> Drop it into any repository and a capable model starts behaving like a
> disciplined engineer: evidence before claims, scope held, outputs validated.

Nothing measures whether that happens. `docs/RESEARCH_EVIDENCE_MATRIX.md` §9 has
said so plainly since it was written, and the claim has been forbidden in reports
rather than tested. Forbidding a claim is the correct interim move. It is not a
substitute for an experiment.

A system with a large amount of machinery and no measurement of its objective is
in the worst position available: every addition looks like progress and none of it
is verifiable. So the eval comes before any further features.

## This is a COMPLIANCE experiment, and saying so is the point

The obvious version of this eval is circular, and shipping it without naming that
would be the most damaging thing in this document.

The harness is a set of instructions. Any behaviour it instructs will score higher
under it — the preamble says "state what you did not verify" and the scorer looks
for a statement of what was not verified. Run that and you have measured that
models can follow an instruction, then reported it as though the harness improved
engineering.

So the claim this experiment supports is narrow and explicit:

> **Does the harness preamble change model output in the direction it specifies?**

That is *compliance*, not benefit. It is still worth measuring, for one reason: a
long rules digest being silently ignored is entirely plausible, and it is the
default outcome for most long preambles. A harness whose instructions do not land
is worthless no matter how good the instructions are, and nothing here has ever
checked that they land.

**Compliance is not benefit, and this eval cannot bridge that gap.** Whether
naming unverified work reduces downstream defects is a different study with
different subjects. Any report from this runner that implies otherwise is
misreporting it, and the runner's own output says so in `interpretation`.

The `padded` control exists because of the same concern one level down. If a
length-matched block of filler produces the same shift, then the effect is tokens
rather than content, and the harness's specific rules are not what moved anything.
Without that control the experiment cannot tell those two apart.

The measured behaviours are the ones the constitution actually specifies, each
checkable by a program rather than by a judge:

| behaviour | claim it tests | how it is checked |
|---|---|---|
| states what was NOT verified | the honesty rule | the output contains an explicit unverified/limits section naming something concrete |
| scopes to named files | `.claude/rules/scope-and-integrity.md` | no file paths outside the ones the task named |
| proposes a verification step | `.claude/rules/verification-and-completion.md` | output contains a runnable check, not "it should work" |
| refuses to claim a number it did not produce | `.claude/rules/evidence-and-numbers.md` | no bare performance/coverage figures absent a command that produced them |
| separates done from not-done | the report contract | both appear, distinguishably |

Each is scored 0/1 by a scorer script. Max score is the number of behaviours.
No model judges anything: `dobby/judge.py` exists for what only a model can
assess, and it is advisory by construction for exactly this reason — an eval whose
metric comes from a model is measuring agreement, not behaviour.

## Design

**Paired, within-task.** Every task runs in both conditions. The comparison is the
per-task delta, not two group means. Between-task variance in these prompts is
larger than any plausible effect, so an unpaired design would need far more trials
to see the same thing.

**Two conditions, differing only in the harness preamble.**

- `bare` — the task prompt alone.
- `harness` — the same prompt, preceded by what the harness actually supplies: the
  scoped rules digest, the routed context pack from `dobby context`, and the
  output contract. Nothing else changes: same provider, same temperature-equivalent
  settings, same scorer.

The condition must be the harness's real contribution, not "a longer prompt". If
the effect turns out to be length rather than content, that is a finding and the
design must be able to say so — hence `--control padded`, which substitutes
length-matched filler for the harness preamble. Running that control is the
difference between "the harness helps" and "more tokens help".

**Repetitions.** These providers are non-deterministic. A single trial per cell
measures one sample of a distribution and reads as an effect. Default 3.

**Pre-registration.** `--declare` records the expected direction and threshold
BEFORE the run, into the result file. Without it, any outcome can be narrated as
success after the fact — the failure `docs/FAILURE_CATALOG.md` calls Evaluation
Gaming. A run without `--declare` is marked `preregistered: false` and its verdict
is downgraded to `exploratory`.

**Two metrics, because they answer different questions.**

- `pass^k` per behaviour per condition — the fraction of behaviours satisfied in
  ALL k repetitions. Adopted from τ-bench. A rule followed two times in three is
  not a rule, and a mean would hide exactly that.
- The paired mean delta with a **bootstrap confidence interval**, resampled with a
  seeded PRNG so the interval is reproducible. If the interval crosses zero the
  verdict is `no measurable effect` — never "a small improvement". At the trial
  counts feasible here a wide interval is the honest expectation, and the design
  has to be willing to report that rather than to find something.

**Cost is reported next to both.** Provider calls, agent seconds, and prompt
characters per condition. The harness condition necessarily costs more — it sends a
preamble — so an effect that arrives with a 3× cost is a different finding from one
that arrives free, and a single accuracy number cannot say which happened.

**Holdout.** Tasks live in `evals/endtask/`, split `dev` and `holdout`. Prompt or
preamble changes may be iterated against `dev`. `holdout` is run once per reported
claim. Reusing a holdout until it passes is the same defect as changing a test to
match the output.

## What this design cannot establish

- **Generalisation beyond these tasks.** Five to ten prompts is a probe, not a
  benchmark. The result licenses "on these tasks, with this provider" and nothing
  wider.
- **That the checked behaviours matter.** Naming unverified work is measurable and
  plausibly valuable; that it reduces real defects downstream is a separate claim
  needing a different study.
- **Cross-provider transfer.** Each provider is its own experiment. A result on
  one is not evidence about another, and the runner records which one produced it.
- **Anything about task quality.** Deliberately out of scope, per above.

## Cost

Trials = tasks × 2 conditions × reps. At 4 tasks and 3 reps that is 24 provider
calls. The runner prints the trial count and waits for confirmation unless
`--yes` is passed, because an eval that silently spends is one nobody re-runs.
