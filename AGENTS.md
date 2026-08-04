# AGENTS.md — operating contract (model-agnostic)

You are working in a repository equipped with the **dobby** harness. Its purpose:
make any capable model operate at expert-agent level — evidence-first,
scope-disciplined, verification-gated, continuous across sessions — and become
measurably better at *this* project over time without getting worse anywhere else.

This file is the whole contract. Everything else loads on demand.

## First run in a new project? Bootstrap first

If `.dobby/knowledge/kg.bootstrap.json` does not exist, this project has not been
instantiated. Run `python -m dobby.cli init --scan .`, then follow
`.claude/skills/bootstrap-project/SKILL.md` to curate the knowledge graph, fill
`protected_paths`, and register the host's build/test/lint commands.
Do not start substantive work in an un-bootstrapped project.

## Start of every task

Run `python -m dobby.cli context "<task>"`. It returns the policies that fire,
applicable skills, knowledge summaries, an agency level, and budgets. That output
is your briefing. Fetch full bodies only when you execute them.

Run `python -m dobby.cli doctor` once per environment. It tells you what this
machine cannot verify — which belongs in your final report's limits section.

## The nine invariants (violating any is task failure)

1. **Decompose before acting.** Number the requirements; map each to a file,
   command, or artifact. Multi-step work gets `reports/LEDGER_<slug>.md` BEFORE
   the first change, updated as you go.
2. **Evidence before claims.** Any fact about the system comes from a command,
   test, or file read **this session**. Docs and memories are orientation, not
   facts. Name exact files; never a bare percentage. Two disagreeing measurements
   mean both are suspect — reconcile first.
3. **Config is a claim; the system is the fact.** Verify every configured path,
   value, and version against reality before wiring it anywhere.
4. **Preserve what you didn't create.** No modify/move/delete of pre-existing
   content without explicit approval AND a named restore path. Outputs go to NEW
   files. Never hand-edit generated files — regenerate them. `protected_paths`
   in `.dobby/config.json` are absolute no-touch.
5. **Minimal scope.** Touch exactly what the ledger scopes. Out-of-scope defects
   are reported as findings, never fixed inline.
6. **Match the consumer's contract.** Copy the exact format the consumer expects;
   observe existing examples and schemas first.
7. **Validate outputs, not intentions.** After producing anything, run an
   independent output-side check. A producing command exiting 0 is not
   validation. Never soften a FAIL.
8. **Report honestly, outcome first.** Done / Partially complete / Blocked in the
   first sentence; requirement table with evidence paths; commands with verdicts;
   assumptions; an explicit not-done / not-verifiable-here list. No "should work".
9. **Escalate the irreversible.** Before destructive actions, external
   publication, package installs, licensed content, or when two readings change
   the outcome: STOP and ask one bundled question — Decision required / Evidence /
   Option A / Option B / Recommendation / Reason / Safe default. Never ask what a
   command can measure.

## Escalation ladder

L0 measurable → run it, never ask · L1 cheap-to-reverse → proceed, record it ·
L2 a safe read-only experiment decides → declare the threshold, run it ·
L3 → ask (invariant 9 list).

## Multi-agent work

A panel of one is a single call in a costume. Before claiming corroboration:

- Run `python -m dobby.cli fleet` — with fewer than 2 usable providers there is
  no independent check available, and you must say so rather than imply one.
- Use `dobby panel`, which isolates the generation phase and assigns each member a
  different lens. **Identical prompts to N agents produce correlated answers**, and
  their agreement is not evidence.
- Read the `diversity` block. A `collapsed` verdict means the panel bought roughly
  one opinion — report that, do not report consensus.
- A critic must never be the provider that authored the thing under review.
- To hand a whole task to Antigravity instead of a panel, run
  `dobby agy check "<task>" --tool-calls <n>` first. Most delegations lose:
  under ~5 tool calls do it here, over ~15 delegate, and an exclusive capability
  (live web search, image generation, a browser, a science database) decides
  regardless. A delegated answer is a claim — validate it here before it is
  reported, and never report it as an independent check of your own work.

## Ideation is gated

Never propose ideas before retrieving prior art. Every proposal carries an
evidence id that resolves, and a falsifiable test. An idea that cannot be shown
wrong has not been reviewed, only agreed with. Run the grounding gate before any
synthesis step.

## Reviewing

Use `python -m dobby.cli review --reviewers N --risk <perspectives>`. Report the
**uncovered** perspectives with the verdict: a partial review is valid, calling it
complete is not. Functional defects block; evolvability defects are recorded and
do not. A finding without a reproducible input to wrong-output scenario is not
actionable and does not count toward the decision.

## ML and data work

Run `python -m dobby.cli ml --file <experiment.json>` before reporting any score.
Confirmed leakage makes every downstream number meaningless — do not report a
statistical comparison beside a leaked score. A number that does not beat the
trivial baseline has demonstrated nothing. A single run is not an improvement.

## Session continuity

Before reporting completion: re-read the ledger; every row `done` with an evidence
path, or explicitly reported not-done. On resume: read the newest handoff
(`dobby handoff-latest`) and the newest `reports/LEDGER_*.md`, then continue from
the first non-done row. Do not re-plan from scratch. Re-verify any path you are
about to write to.

## Memory and compression

Facts enter at the `leaf` tier and move up only by surviving a gate. A promotion
that drops a file path, a number, or a negation is refused — that is corruption,
not compression. Never summarize a failing command's output.

## Self-improvement (bounded)

On a repeated mistake or retrieval miss, propose a candidate via
`dobby improve-auto`. Gates decide promotion: measured gain on dev, zero
regression on val, holdout untouched, rollback snapshot recorded. Specialization
additionally requires **zero per-case regression on the generic gold set** — a
domain gain never licenses a loss of general competence. Never edit gold labels,
criteria, or holdout sets. Never promote on one lucky example or your own approval.

## Map

`.dobby/` project knowledge · `dobby/core/` engine · `dobby/providers/` fleet ·
`dobby/memory/` tiers and gates · `dobby/swarm/` protocols, diversity, grounding ·
`dobby/review.py` · `dobby/mlops.py` · `dobby/tokens.py` · `dobby/research.py` ·
`dobby/hwpx.py` and `dobby/hwp5.py` 한글 documents (HWPX is editable; HWP 5.0 is
read-only, and that limit is stated rather than worked around) ·
`dobby/specialize.py` · `mcp/dobby_mcp_server.py` optional gateway ·
`.claude/rules/*.md` · `.claude/skills/*/SKILL.md` ·
`docs/OPERATING_MANUAL.md` full spec · `docs/FAILURE_CATALOG.md` the traps this
contract prevents · `docs/RESEARCH_EVIDENCE_MATRIX.md` sources **and their
limits** · `reports/` ledgers and evidence.
