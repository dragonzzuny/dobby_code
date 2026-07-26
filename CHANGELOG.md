# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each entry records what changed **and what the change does not establish**.
A changelog that only lists additions lets unmeasured capability accumulate as
though it were proven.

## [Unreleased]

### Fixed — the provider layer had never been executed, and it did not work

Three defects, all found by running things that had only ever been described.
None of them could have been found by reading, and two of them made the
multi-agent capability — the reason this kit has a provider layer at all —
non-functional on its primary development platform.

- **Most of the fleet could not launch.** `run_provider` resolved the binary and
  discarded the answer, launching the bare name with `shell=False`. On Windows
  `shutil.which` consults PATHEXT and `CreateProcess` appends only `.exe`, so
  every npm-installed `.CMD` shim resolved and then refused to start. Measured:
  `which("codex")` → `codex.CMD`; `run(["codex"])` → WinError 2;
  `run([r"...\codex.CMD"])` → rc 0. `fleet` reported all four CLIs
  `usable: true` while two of them failed in 0.14s without starting a process.
- **A batch shim truncated every multi-line prompt, silently.** Measured with one
  string through both routes: `.CMD` shim → `["line one"]`, direct exe →
  `["line one\nline two\nline three"]`. `%`, `&&`, `^` and `|` all survive; only
  newlines are lost. This was worse than the launch failure, which at least
  errored: a provider returned a fluent answer to the prompt's first line, with
  nothing in the output to indicate the rest had been cut. Multi-line arguments
  now route through the vendor's own `.ps1` shim, and where there is none the
  call is **refused** rather than truncated.
- **The POSIX-shell check trusted a name.** `which("sh") or which("bash")`
  returned true for `C:\Windows\System32\bash.exe`, the WSL launcher, which with
  no distribution installed prints a UTF-16LE error and exits 1. A suite guarded
  by `skipUnless(posix_shell_available(), ...)` therefore did not skip; it ran the
  installer through a shell that executes nothing, and seven assertions failed
  with messages about missing files. The guard existed; the predicate was the bug.
  It now probes for the capability required — resolving a path in the form Python
  hands out — rather than for a greeting.

`-ExecutionPolicy RemoteSigned`, not `Bypass`. An earlier version assumed an
unsigned shim needed `Bypass`; measured, `RemoteSigned` is sufficient and still
refuses an unsigned script carrying the internet-zone marker.

**What this establishes:** three providers (claude, agy, codex) answered a live
probe with the exact expected token, at 29.1s / 14.6s / 31.0s. Before this,
`verified_on=(WIN,)` had claimed executed-here for all four CLIs since it was
written, while execution was impossible for two of them.
**What it does not:** `gemini` launches and is refused by its service for an
account-tier reason; `qwen`, `ollama`, `kimi` and `dashscope` have still never
been executed, and their `verified_on` is still empty.

### Added — model judgment as advisory evidence (`dobby/judge.py`)

- `Evaluator(judge=True)` and `dobby slice --judge` grade `model_judgment`
  criteria with a provider. Every such criterion had been `NOT RUN` since the
  repository existed.
- **Advisory, never verification.** `.dobby/ontology.json` states that a
  `model_assertion` is never `confidence=verified`. `Evaluator.evaluate` had been
  treating every record with a non-`None` `passed` as deterministic, and model
  judgments always returned `None`, so the rule held *by accident*. Wiring a judge
  without splitting that bucket would have made a model opinion weigh exactly as
  much as a test exit code. Judgments are now excluded from the verdict in both
  directions, capped at confidence 0.6, and reported separately with the judging
  provider named.
- Three refusals, each tested: never runs implicitly (it costs money and leaves
  the machine); never grades its own author (`exclude` reaches `resolve_role`);
  never guesses at a reply outside the fixed format — prose containing the word
  "pass" is `UNPARSEABLE`, because lenient parsing is how a judge starts agreeing
  with everything.

**What this establishes:** on two artifacts differing only in honesty, the judge
returned PASS quoting the artifact's own "NOT verified here" section, and FAIL
quoting "Everything works perfectly across every platform … does not state
anything not verified". It discriminates rather than rubber-stamping.
**What it does not:** one judge, one provider, two artifacts. No agreement rate
across providers, and no measurement of how it behaves on artifacts that are
subtly rather than obviously overclaiming.

### Added — CI that explains its own failures (`tools/ci_*.py`)

The first fourteen runs of this pipeline were red while the local suite was green,
for three separate reasons, and none of them could be read. Measured:
`GET /actions/jobs/{id}/logs` → `403 "Must have admin rights to Repository."`;
`GET /check-runs/{id}/annotations` → `200`.

- `ci_step.py` wraps each step and emits the failing `unittest` blocks as
  annotations, with platform, interpreter, stdout encoding, UTF-8 mode, cwd and
  tempdir. Not a bash wrapper: that would mean `shell: bash` on the Windows jobs,
  and swapping pwsh for MSYS bash changes the environment under investigation.
- `ci_env_report.py` records those values on every run, green or red.
- `ci_local.py` runs the mirrored pipeline in a fresh clone, on every interpreter
  installed, with `PYTHONUTF8` and `PYTHONIOENCODING` **removed** — every local
  run in this project had been made from a shell that exported them, which is why
  the encoding class of failure could not appear locally.
- The reporter downgrades characters the console cannot encode instead of dying
  on them. cp949 and cp1252 are complementary: cp949 encodes Korean but not an em
  dash, cp1252 the reverse. A reporter that a Korean string can kill is not a
  reporter.

### Added — solution search (`dobby/search.py`)

- Tree search over candidate solutions with a hard-coded DRAFT → DEBUG → IMPROVE
  policy. Published MLE-Bench results put a tree-search agent at 16.9% medals
  against 4.4% for the strongest linear agent at the same model tier.
- Inference-time layer composition (generate / rank / fuse / critique / revise /
  verify) with a **static validator** that catches paid no-ops before any
  inference is spent — ranking one candidate, fusing after a collapse, revising
  with no preceding critique. Each produces a pipeline that runs and returns a
  plausible answer, so nothing else surfaces them.
- Case-based reasoning that stores the **approach**, never the answer. Failed
  cases are retrievable but returned in a separate `avoid` list.
- `yield_report`, because published autonomous-research loops report
  single-digit-percent hit rates and a loop that reports only its successes
  implies a rate it does not have.

### Added — team topologies (`dobby/swarm/topologies.py`)

- Six shapes: independent, pipeline, fan-out-in, supervisor, hierarchical, mesh.
  `mesh` is selectable but never recommended — omitting it would not stop anyone
  wiring one, only stop them being told what it does.
- **`framing_depth` replaced connectivity as the diversity metric.** At six
  agents a pipeline and a fan-out-in have identical connectivity (0.167) and
  opposite diversity properties: the pipeline chains one framing through five
  reinterpretations (depth 5, 1 independent agent) while the fan-out keeps five
  agents reading the raw task (depth 1, 5 independent). Connectivity cannot see
  that difference; the documented claim that it tracked diversity was wrong and
  a test caught it.
- Plans are data. `waves()` schedules them, `cost()` separates wall-clock
  (waves) from token cost (agents), and a cyclic plan raises instead of looping.

### Added — API provider transport (`dobby/providers/api.py`)

- OpenAI-compatible transport for `kimi` and `dashscope`, stdlib `urllib` only.
- `allow_network` is a **required keyword with no default**, so egress cannot be
  enabled by a refactor nobody reviewed as a security change. Missing keys and
  disallowed calls raise rather than degrading into a failed result.
- Prompts are redacted **before** transmission; redacting a response is theatre.
- A 512 KB request ceiling, and an audit record of bytes actually sent.

### Added — ML leakage classes that pipeline checks cannot see

- **External-source leakage**: the labels exist somewhere public, so an agent can
  look them up. The split is clean and the score is still meaningless.
- **Rule violations** (read test labels, trained on test, leaderboard probing,
  copied solution, modified metric) — CONFIRMED and explicitly unfixable by
  caveat.
- **Holdout ordering**: a holdout carved out after the run has already been seen
  by whatever produced the candidates.

### Added — design (`dobby/design.py`)

- Six **aesthetic** presets, each committing to a density and a contrast
  strategy. Tokens say what values exist; an aesthetic says what the interface is
  trying to be, and without it two products with identical tokens still diverge.
- Named layout-section variants, so an agent stops inventing a structure per
  screen.
- WCAG contrast checking on text-on-surface pairs. Only those pairs — testing
  every colour against every other produces a report nobody reads.

### Fixed

- `search`: bounding debug **depth** did not bound debug **work**. A shallow
  buggy node stayed eligible forever, so the policy kept returning to it and
  drained the whole budget into retries of one broken draft. One repair attempt
  per node plus the depth cap makes the broken region finite — an all-buggy run
  now stops after 3 nodes instead of consuming a 20-node budget.
- `search`: `higher_is_better` was applied to final selection but not to the
  IMPROVE policy, so a loss-metric search spent every remaining call refining
  the worst candidate while still reporting the right answer.
- `search`: patience fired on runs with zero viable nodes, reporting "converged"
  for a search that never started climbing.

### Planned

- A driver wiring `search.search` to real providers — the policy is tested, but
  it has never executed a model call.
- A model-judge adapter so `model_judgment` criteria stop reporting `NOT RUN`.
- Sandboxed execution so large tool payloads never enter context at all.
- An AST call graph so `blast_radius` builds its own edges.

## [0.1.0] — 2026-07-26

First release.

### Added — engine

- Knowledge graph with mandatory provenance. A `model_assertion` can never be
  marked `verified`; this is enforced in code and tested.
- Minimum-sufficient-agency router (levels 1–7), with levels 6–7 gated behind
  explicit human opt-in.
- Policy book, gated skill lifecycle (proposer ≠ approver), sha256-pinned
  evaluator criteria, JSONL trajectory with structured handoff.
- Bounded improvement loop: propose → validate (dev gain, no val regression,
  holdout untouched) → promote or reject, with a rollback snapshot.
- Cross-project evolution with a genericity filter, so domain knowledge
  structurally cannot enter the shared kit, and federation regression, so a
  lesson from one project cannot degrade another.
- Optional MCP gateway exposing exactly four meta-tools, allowlisted command
  templates, output caps, secret redaction, and an audit log. **No network tool
  exists**, which removes the exfiltration leg of the lethal trifecta.

### Added — multi-provider fleet (`dobby/providers/`)

- Adapters for `claude`, `codex`, `gemini`, `agy`, `qwen`, `ollama`, and
  OpenAI-compatible `kimi` / `dashscope`.
- Availability is **measured, never assumed**, and reported in three states —
  `available`, `absent`, `blocked` — because the remedy differs for each.
- Role routing by cost: breadth roles cheap and many, decision roles expensive
  and few. `synthesize` and `adjudicate` never route to an API provider, since
  they see the whole aggregated context.
- Bounded-concurrency fan-out. One provider failing never loses the round.
  Concurrent file-writing providers each get a detached `git worktree`.
- Read-only (`plan`) permission mode is the default for every file-capable CLI.

### Added — decorrelated multi-agent (`dobby/swarm/`)

- Five protocols (NGT, Double Diamond, Six Thinking Hats, dialectic,
  perspective-diverse adversarial). Every one isolates its generation phase.
- Assigned lenses: SCAMPER operators, de Bono hats, dialectic roles, and
  per-failure-mode critic lenses. Lens reuse beyond the catalogue is reported,
  never silent.
- `effective_n`: six identical answers report as ~1 opinion. `coupling_ratio`
  measures how much a sharing round contracted the panel.
- Grounding gate: no ideation before prior art; every idea needs an evidence id
  that resolves and a falsifiable test; rejections return as a histogram.

### Added — six-tier memory (`dobby/memory/`)

- `nation → mountain → forest → tree → branch → leaf`, five hops maximum,
  index-based layer-by-layer routing.
- A **different mechanism per tier**: fixed vocabulary, prototype centroid,
  co-occurrence adjacency, slotted card, verified-first decay, append-only log.
- Explicit forget / admit / expose gates, each decision carrying its reason.
- Promotion audited for leakage: a summary that drops a file path, a number, or
  a negation is **refused**, because that is corruption rather than compression.
- Compression guideline learned from paired full-succeeds / compressed-fails
  failures, versioned and diffable.

### Added — specialization (`dobby/specialize.py`)

- Dual gate: the project's gold must gain **and** the generic gold must not
  regress on any single case. Per-case, because an average hides the trade.
- Mastery level derived from counted evidence, never from elapsed sessions.
- Rejections recorded as negative memory so a losing change is not re-proposed
  every session.

### Added — review (`dobby/review.py`)

- Perspective-based reading over ISO/IEC 25010 characteristics, with risk areas
  assigned first so a small panel cannot silently drop `security`.
- QA (was the process followed) and QC (does the output behave) computed and
  reported separately. "No checks were run" is a QC **failure**, not a pass.
- Severity priced by containment cost, so findings order by expected cost
  avoided rather than by how alarming they sound.
- Functional and evolvability findings in separate lists. Functional defects
  block a merge; evolvability defects never do.
- A finding with no reproducible scenario is reported as not actionable and
  excluded from the decision.

### Added — ML and data analysis (`dobby/mlops.py`)

- Leakage detection: fit-before-split (proven from step ordering), features
  equal to or derived from the target, group spillover, temporal shuffling,
  duplicate rows, holdout reuse. `confirmed` only when ordering proves it.
- Reproducibility gaps reported with the consequence of each.
- Statistical rigor: run-to-run variance, trivial-baseline anchoring,
  multiple-comparison cost of a hyperparameter search.
- Interpretation: causal language from a predictive design, unbounded
  generalization, numbers not from an untouched holdout.
- **Confirmed leakage short-circuits every downstream check**, so a leaked
  score cannot be dressed in statistics.

### Added — token efficiency (`dobby/tokens.py`)

- Per-command output condensers (pytest, unittest, git status/push/diff,
  listings) with consecutive-duplicate collapsing.
- **Failing commands are never condensed** and their full output is preserved
  to disk for re-reading.
- Priority-tiered snapshots (P1–P4) inside a hard byte budget, measured against
  the rendered artifact so the bound cannot be overshot. Everything dropped is
  named.
- Blast radius over an edge list, traversed backward, with truncation reported
  as a lower bound.
- Savings labelled as estimates that separate input from output tokens, and can
  report a **net loss** when a style instruction costs more than it saves.

### Added — research (`dobby/research.py`)

- Search plans that always include a refutation and a limitation query built
  from the same terms.
- Claim strength classification driving the required-artifact list.
- Citation resolution at three severities. An empty corpus reports
  `NOT CHECKED`, never a clean bill of health.

### Added — design (`dobby/design.py`, `DESIGN.md`)

- YAML token frontmatter plus the eight prescribed prose sections.
- Validation at three severities whose primary target is **tokens declared
  without the prose that says when to use them**.

### Fixed — cross-platform defects

The ported engine failed 5 of 84 tests on native Windows. All four root causes
were real portability defects, not test artifacts:

- `python3` hard-coded in data-defined command templates. It does not exist on
  a default Windows install; `cmd.exe` returned exit 9009. Replaced with a
  `{python}` placeholder resolving to `sys.executable`, quoted when the path
  contains spaces.
- Standard streams used the locale codec. On this Korean Windows install that is
  `cp949`, which cannot encode an em dash — the MCP server died mid-protocol on
  any non-ASCII knowledge summary. Streams are now pinned to UTF-8.
- Child processes re-derived their own encodings, so captured output arrived
  mojibake'd or raised `UnicodeDecodeError` in the parent. Children now inherit
  `PYTHONUTF8` and `PYTHONIOENCODING`.
- Repository inventory paths used the native separator, so `.github/workflows`
  never matched on Windows and CI, rules, and skills read as *absent* rather
  than as undetected. Paths are normalized to POSIX form, which also keeps the
  knowledge graph comparable across machines for federation.

Two further defects were found by the new tests and fixed:

- Claim-strength markers matched as substrings, so `improves` contained
  `proves` and a quantified claim was classified as absolute — sending the
  reader after a proof instead of a measurement. Now matched on word
  boundaries.
- `GroupKFold` contains `kfold`, so the correct group-aware splitter was
  flagged as the group-spillover defect. Group-aware detection now
  short-circuits first.
- Effect size divided by a float-comparison-to-zero variance, producing an
  effect of ~1e15 for identical runs. Degenerate variance is now a tolerance.

### Not established

Enumerated in full in `docs/RESEARCH_EVIDENCE_MATRIX.md` §9. The load-bearing
ones:

- **No end-task effect has been measured.** That this harness raises a weaker
  model's performance requires a controlled comparison across ≥2 model families
  with a positive holdout result. That has not been run, and the claim is
  forbidden in this repository's reports.
- **No published metric from any cited system is claimed as a property of this
  one.** Mechanisms were adopted at lower fidelity (lexical rather than
  embedding; edge lists rather than an AST call graph) and measured
  independently — which is to say, not yet measured at all.
- **Retrieval, diversity, and claim support are lexical.** Paraphrase reads as
  diversity; a negation reads as similarity.
- **`qwen`, `ollama`, `kimi`, `dashscope` were never executed here.**
  `verified_on` is empty for each and `dobby fleet` reports it.
- **The ML gates read a described setup, not data.** A leak that is not
  described is invisible to them.

[Unreleased]: https://github.com/dragonzzuny/dobby_code/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dragonzzuny/dobby_code/releases/tag/v0.1.0
