# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each entry records what changed **and what the change does not establish**.
A changelog that only lists additions lets unmeasured capability accumulate as
though it were proven.

## [Unreleased]

### Planned

- Deep survey of the ML/data-science agent ecosystem, feeding concrete gates
  into `dobby/mlops.py` (experiment tracking, notebook handling, AutoML search
  hygiene, RL-specific evaluation traps).
- Team-architecture patterns not yet implemented: pipeline, supervisor,
  hierarchical delegation (`docs/RESEARCH_EVIDENCE_MATRIX.md` §10).
- API-kind provider transport for `kimi` / `dashscope` — currently declared in
  the catalog and refused by `run_provider`.
- A model-judge adapter so `model_judgment` criteria stop reporting `NOT RUN`.
- Sandboxed execution so large tool payloads never enter context at all.

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
