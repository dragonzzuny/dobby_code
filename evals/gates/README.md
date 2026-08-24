# Promotion evidence for `runnable-gates` and `ledgered-task`

The registry refuses to let an author approve their own skill
(`dobby/core/skills.py`), and `AGENTS.md` says a second opinion must come from a
different provider. So both skills were proposed here and approved by **codex**,
against the evidence in this directory. It took six rounds and codex rejected
`ledgered-task` in five of them; every rejection is kept.

## What decides what

| file | what it holds |
|---|---|
| `scenarios.json` | the deterministic scenarios, their claims, and the trap each exists to catch |
| `run_scenarios.py` | runs them against fixtures; exit code is the verdict |
| `run_behavioral.py` | the behavioral suite: a provider gets a real sandbox and a real task, and what it leaves on disk is graded |
| `additivity.py` | did the revision LOSE anything — derived from the arms, not from the document |
| `regrade_heldout.py` | scores the held-out suite against artefacts already on disk, so no provider call is repeated to obtain it |
| `request_approval.py` | sends the packet to a different provider and records the verdict |

Results: `RESULTS.json` (deterministic), `RESULTS_behavioral.json`,
`HELDOUT.json` (reward-hacking gap), `ADDITIVITY.json`.

## The arms, and why each gets what it gets

Each arm is handed exactly what its own protocol tells it to load. Giving the
control the treatment's materials would not be a fairer comparison, it would be
a different experiment.

| arm | protocol | `runnable-gates` supplied | route to dobby's CLI |
|---|---|---|---|
| `treatment` | v1.1 (names the skill and the CLI) | yes | yes |
| `control` | v1.0 (names neither) | no | yes |
| `controlnoenv` | v1.0 | no | **no** |
| `controlref` | v1.0 | yes | yes |

`controlnoenv` exists because `control` turned out to be contaminated, and that
was measured rather than argued. On claude the control produced gates,
approvals, intact fingerprints and machine evidence — everything the revision is
credited with — while running a protocol that mentions none of it. Its own
report said why: it had gone and read the repository, which the harness had
pointed it at with `PYTHONPATH`. Remove the pointer, change nothing else, and it
produces nothing.

So the claim is narrower than "the revision causes the behaviour":

- **sufficient** on both providers;
- **necessary** on codex, which did not go looking even with the repo reachable;
- on claude, necessary only when the skill is not otherwise reachable, because
  claude will find it if it can.

`controlref` is the other half of that: the old protocol WITH the gates skill
supplied produced gates. The skill in context is enough on its own; what the
revision contributes is that an agent reaches for it.

## Held-out grading

The agent writes the gates it is graded on, which is the reward-hacking setup
[SpecBench](https://arxiv.org/abs/2605.21384) measures. Each run is therefore
also graded against a suite it never sees, composing the feature rather than
restating it, and the gap between the two is reported. Self-tested: a `calc.py`
that hardcodes `return 7` passes its own gates and scores 0.43 held-out.

`LEDGER-APPROVES-BEFORE-VERIFYING` grades the protocol rather than the
calculator: it reads each approval record, confirms it names a gate the ledger
declares, recomputes the oracle fingerprint from the gate as written, and
requires the EVIDENCE lines to be the ones the tool writes. Self-tested against
three tampered sandboxes — no-approval, edited-after-approval, and
hand-written-evidence are each caught.

## The rejections, kept

`APPROVAL_codex_round1.json` .. `round5.json`, then `APPROVAL_codex.json`.
A verdict is never overwritten by a later one, and a FAILED call is written to
`APPROVAL_codex_failed.json` instead of over the standing record — that file is
the evidence for the Windows command-line limit, which the harness had been
misreporting as a broken shim.

Superseded rather than deleted:

- `LEDGER-GATES-REFERENCE` and `LEDGER-PROTOCOL-INTACT` in `scenarios.json` are
  substring checks. Round 2's rejection is the reason the behavioral suite
  exists, so they are marked `superseded` and kept with it.
- `runs_confounded/` and `artefacts_confounded/` are the behavioral runs made
  before round 3, when the harness prompt still said "Write `GATES.md` and the
  ledger in THIS directory" — so it was measuring its own instruction. Kept
  because the comparison with the de-confounded runs is what shows the
  difference the prompt was making.

## What this cannot establish

- A prior obligation that no check here measures is invisible to `additivity.py`,
  exactly as it was to the grep it replaces.
- `LEDGER-RESUME-CONTINUES` is `controlled: false`. The "On resume" clause is
  identical in both versions, so no control can isolate the revision there; it
  is evidence the protocol works, not evidence the revision helped.
- Two executors is two. Nothing here licenses a claim about a provider that was
  not run.
