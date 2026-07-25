# Research evidence matrix

Every design decision in this repository that came from outside it, with the
source and — importantly — what the source does **not** establish. A citation
that only records the supporting half is how a harness accumulates unfounded
mechanisms that nobody can later question.

Columns: **claim adopted** → **where it lives** → **source** → **limit**.

---

## 1. Harness engineering — the frame

| claim adopted | where | source | limit |
|---|---|---|---|
| The wrapper around a fixed model (what is stored, retrieved, and presented per step) is itself the engineering surface; `Agent = Model + Harness` | the whole repository | *Agent Harness Engineering: A Survey* / `RUCAIBox/awesome-agent-harness` taxonomy (context management · memory · orchestration · tool interface · evaluation · self-improvement) | The survey's taxonomy is descriptive. Reported end-to-end swings from harness changes are *other people's* benchmarks on other systems; none of them measure this harness. See §9. |
| Deterministic pipelines beat free-form agency for well-shaped tasks | `core/router.py` agency ladder 1–7 | Agentless-style pipeline results | Applies to tasks whose shape is known in advance. The ladder therefore *assigns* a level rather than defaulting to the lowest. |
| The agent–computer interface shapes performance as much as the model | `mcp/` 4 meta-tools, fixed command templates | SWE-agent (ACI findings) | Their ACI was tuned for SWE-bench-style tasks. |
| Actions expressed as executable code outperform JSON tool calls | code-mode primary, MCP optional (ADR-3) | *Executable Code Actions Elicit Better LLM Agents* | Measured on tool-use benchmarks, not on repository work. |
| Policies as a retrievable action knowledge base | `.dobby/policies/policies.json` | KnowAgent | — |
| Skills need a gated lifecycle, not free self-addition | `core/skills.py` (candidate → … → active; proposer ≠ approver) | Voyager skill library | Voyager's domain was Minecraft; the gating discipline transfers, the skill format does not. |
| Bounded reflection: cap surfaced lessons | `core/memory.py` (episodic/negative ≤3) | Reflexion | — |
| Paging between working and long-term memory | `memory/tiers.py` TTL gradient | MemGPT | — |
| Untrusted input + secret access + an egress path is the dangerous combination | `mcp/` ships **no** network tool; API providers gated behind `providers.allow_network` | OWASP LLM06 / "lethal trifecta" | Removing the third leg does not make prompt injection harmless — it makes exfiltration structurally unavailable. Injection can still cause wrong *local* actions. |

## 2. Multi-agent orchestration and diversity

| claim adopted | where | source | limit |
|---|---|---|---|
| Interaction *contracts* exploration: dense topologies converge prematurely, authority-shaped roles suppress minority views, group-size scaling has diminishing returns — **structural coupling** → **diversity collapse** | `swarm/diversity.py`, sparse-by-default panels | *Diversity Collapse in Multi-Agent LLM Systems: Structural Coupling and Collective Failure in Open-Ended Idea Generation* (ACL 2026 Findings, arXiv 2604.18005) | **Only the abstract was read.** The paper's own diversity formulas, operational definition of coupling, and intervention effect sizes were **not** obtained. `effective_n` and `coupling_ratio` are this repository's own lexical constructions motivated by the finding, **not** reimplementations of the paper's metrics. Do not cite them as such. |
| Writing independently before discussing avoids the rush to agreement | isolated first phase in every protocol (`swarm/protocols.py`) | same paper (NGT as mitigation) + Diehl & Stroebe (below) | — |
| Multi-agent orchestration needs explicit human opt-in | `allow_multi_agent` in the router | cost, not a paper | — |

## 3. Cognitive psychology of ideation

| claim adopted | where | source | limit |
|---|---|---|---|
| **Production blocking**: face-to-face brainstorming groups generate *fewer* and less creative solutions than the same number working alone, largely because members lose their own ideas while waiting for a turn | isolation is mandatory in phase 1 of every protocol | Diehl & Stroebe (1987), *Productivity Loss in Brainstorming Groups: Toward the Solution of a Riddle*, JPSP 53:497–509 | Human subjects. LLM agents do not "wait for a turn" — the transfer is by analogy to *sequential context contamination*, and is argued, not measured here. |
| **Structured imagination / functional fixedness**: asked to imagine something novel, people import their most accessible existing knowledge and produce variations on the obvious | lenses are **assigned**, not chosen; `distance_from_prior_art` penalizes restatement | Duncker (functional fixedness); Ward (structured imagination); Yagolkovskiy (2020) on semantic priming loosening fixedness, *J. Creative Behavior* | Human cognition. The observed LLM behaviour is analogous; the mechanism may differ entirely. |
| **Geneplore**: creative cognition alternates between *generating* pre-inventive structures and *exploring* their implications, cycling until the structure survives | `swarm/grounding.explore_cycle` returns repairs instead of discarding rejected ideas | Finke, Ward & Smith, *Creative Cognition* | Descriptive model of human cognition, not a validated algorithm. |
| **Perspective-based reading beats checklist-based reading** on cost per defect and inspection time | `review.py` is PBR-primary; the checklist is retained only for mechanical items | Controlled inspection experiments comparing PBR vs CBR (multiple replications; some report near-equal detection with PBR taking less time) | Results vary by artifact type — one UML design study found detection effectively tied (PBR 69% / CBR 70%) with PBR faster. So PBR is chosen for **efficiency**, and the claim here is not that it finds strictly more. |

## 4. Grounded ideation and paper verification

| claim adopted | where | source | limit |
|---|---|---|---|
| Directly prompted LLM ideas are rated **more novel but less feasible** than expert ideas, and score **significantly lower after execution** — the gap is operational, not stylistic | the gate optimizes grounding + feasibility, never novelty | *Can LLMs Generate Novel Research Ideas?*; *Measuring the Gap Between Human and LLM Research Ideas* (arXiv 2607.01233); *LLMs for Scientific Idea Generation: A Creativity-Centered Survey* | Measured on research ideation, not software design. |
| Grounding generation in retrieved literature both reduces hallucination and *increases* useful novelty | prior-art gate precedes ideation | *ResearchStudio-Idea* (arXiv 2607.04439); Graph2Idea (arXiv 2606.09105); SciMON; ResearchAgent | — |
| Fabricated citations are stylistically indistinguishable from real ones; detection must be **resolution** against an independent corpus, at graded severity | `research.verify_citations` (exact / metadata_mismatch / unresolvable) | *CiteCheck* (arXiv 2605.27700) — three-class severity: exact, minor metadata corruption, major fabrication; *DeepSciVerify* (arXiv 2605.27710) | Resolution is only as good as the corpus. An empty corpus therefore reports `NOT CHECKED`, never "clean". |
| Claim–evidence alignment needs escalating evidence, and a reproducibility checklist is per-claim | `research.reproducibility_report` derives required artifacts from claim strength | DeepSciVerify; SEVA (arXiv 2606.29713); ICML-2026-template reproducibility checklists | — |

## 5. Memory and context compression

| claim adopted | where | source | limit |
|---|---|---|---|
| Organize memory by **degree of semantic abstraction**, give each node an index pointing at semantically related children, and route **layer by layer** instead of computing similarity over everything | `memory/tiers.py` — six tiers, five hops, `children` index, `route()` beam walk | **H-MEM** — Sun et al., *H-MEM: Hierarchical Memory for High-Efficiency Long-Term Reasoning in LLM Agents*, EACL 2026 (aclanthology 2026.eacl-long.15) | H-MEM's layers are **embedding** vectors with positional index encoding; this implementation is lexical and its tier count/mechanisms are chosen here, not taken from the paper. H-MEM reports gains over five baselines on LoCoMo — **that result does not transfer to this implementation** and is not claimed. |
| Multi-tier stores (short / mid / long) with selective expansion under a budget | TTL gradient + promotion gating | MemoryOS (Kang et al. 2025); RAPTOR; GraphRAG; SimpleMem | — |
| Memories as dynamically linked notes | `contradicts` edges resolved by Decision nodes | A-Mem (NeurIPS 2025) | — |
| Non-parametric continual learning over a graph | `core/kg.py` merged graph | HippoRAG | — |
| **Optimize the compression GUIDELINE in natural language from paired trajectories where full context succeeds and compressed context fails** — model-agnostic, no parameter updates | `memory/gates.CompressionGuideline.learn_from_failure` | **ACON** — *Optimizing Context Compression for Long-horizon LLM Agents* (arXiv 2510.00615). Reports 26–54% peak-token reduction, >95% accuracy retained when distilled, up to 46% improvement for smaller LMs | Those numbers are ACON's, on AppWorld / OfficeBench / Multi-objective QA, using model-based compressors. This repository implements the *guideline-learning loop* only; it performs no compression itself and claims none of those figures. |
| Prompt compression is a real lever but a lossy one | leakage audit refuses >5% load-bearing loss | LLMLingua (EMNLP 2023) | LLMLingua is model-based; the audit here is a deterministic pre/post check, not a compressor. |
| Long contexts are used unevenly — position matters | progressive disclosure; summaries before bodies | *Lost in the Middle* (TACL 2024) | — |

## 6. Token efficiency (from the tooling ecosystem, not papers)

These are engineering artifacts, not peer-reviewed results. Their **mechanisms**
are adopted; their **accounting** is corrected.

| claim adopted | where | source | limit |
|---|---|---|---|
| Per-command output handlers beat generic compression: only a pytest-aware handler knows the failures matter and the passing dots do not | `tokens.HANDLERS` (pytest, unittest, git status/push/diff, listings) | `rtk-ai/rtk` — CLI proxy with 100+ command handlers, reports 60–90% bash-output reduction | RTK's own docs state the savings measure **bash output**, not billing, and that tokens are estimated as bytes÷4 with no tokenizer. Both caveats are reproduced in `estimate_savings`. |
| Preserve full output on failure so it can be re-read | `tokens.condense` passthrough + `_preserve` to disk | RTK's tee mechanism | — |
| **Priority-tiered compaction snapshot** inside a small hard byte budget; lower tiers drop first | `tokens.SNAPSHOT_TIERS` (P1–P4), `build_snapshot` | `mksglu/context-mode` — P1–P4 tiers, ≤2 KB snapshot, PreCompact/SessionStart hooks, reports 315 KB → 5.4 KB on tool payloads | context-mode achieves its largest reduction through **sandboxed execution** (raw data never enters context). That is not implemented here; only the tiered-snapshot mechanism is. |
| Style constraints cut output tokens but cost resident input tokens every turn, and can be a **net loss** | `estimate_savings` reports the net and can return a negative number | `JuliusBrussee/caveman` (65% avg output reduction over 10 tasks, 22–87% range; states the skill itself adds ~1–1.5k input tokens/turn); `drona23/claude-token-efficient` (465→170 words, −17.4% cost over 3 tasks; states low-usage sessions may cost *more*) | Both are the authors' own benchmarks on small task sets. The mechanism is adopted as a *policy option*; no percentage from either is claimed. |
| **Query the structure for a change's blast radius instead of reading files** — avoiding a read beats shrinking one | `tokens.blast_radius` (backward traversal, bounded, truncation reported) | `tirth8205/code-review-graph` — Tree-sitter AST graph, blast radius, risk scoring, reports ~200K → ~2.5K review context | That system uses a real AST call graph plus Leiden community detection and FTS5 hybrid search. This implementation traverses whatever edge list it is handed (the knowledge graph), which is coarser. The 38–528× figures are theirs, not this repository's. |

## 7. Setup, distribution, and team patterns

| claim adopted | where | source | limit |
|---|---|---|---|
| Idempotent `install` / `update`; re-running is safe | `install.sh` / `install.ps1` | `workdd/my_claude_code_setting` | — |
| Credential isolation: keys in env, never committed; scrub absolute paths before commit | `.gitignore`, `core/security.redact_secrets` | same | — |
| Hooks degrade gracefully when a binary is absent — no hard errors | `providers/detect.py` three-state reporting | same | — |
| Document and pin external components rather than vendoring them | provider catalog declares install commands instead of bundling | same | — |
| Named team-architecture patterns (pipeline, fan-out/fan-in, expert pool, generate-validate, supervisor, hierarchical delegation) | partially covered: fan-out/fan-in (`providers/fanout.py`), generate-validate (`swarm/protocols.ADVERSARIAL`), expert pool (role routing) | `revfactory/harness` (L3 team-architecture factory; reports +60% quality on n=15, authors' own measurement, third-party replication pending) | **Pipeline, supervisor, and hierarchical delegation are not implemented.** See §10. |
| Capture the delta between designed and deployed architecture and feed it back | `core/evolve.py` harvest with genericity filter + federation regression | same (`/harness:evolve`) | Pre-existing in the engine; the source corroborates the pattern. |

## 8. QA/QC and ML

| claim adopted | where | source | limit |
|---|---|---|---|
| **QA is process, QC is output inspection** — a programme needs both, and merging them turns a green CI run into a quality claim | `review.qa_findings` / `review.qc_findings`, computed and reported separately | standard QA/QC practice literature (2026 practitioner surveys) | — |
| Defect containment cost grows by stage: ~6× from development to production, ~100× to a live incident | `review.CONTAINMENT_COST`, used directly as priority weights | IBM Systems Sciences Institute containment data as reported in 2026 shift-left practice guides | These ratios are widely repeated and weakly sourced to a primary study. They are used as an **ordering** heuristic, where the relative shape matters and the absolute values do not. |
| Split defects into **functional** vs **evolvability** families | `review.DEFECT_TYPES`, reported in separate lists | *What Types of Defects Are Really Discovered in Code Reviews?*; *A Systematic Literature Review and Taxonomy of Modern Code Review* (arXiv 2103.08777) | — |
| Defect escape rate and change failure rate are the metrics that matter | `review.escape_metrics` | DORA / Accelerate State of DevOps | DORA measures organizations; a single review's numbers are not a DORA measurement. |
| Product quality decomposes into named characteristics | `review.PERSPECTIVES` uses ISO/IEC 25010 characteristics as reviewer perspectives | ISO/IEC 25010 | Using the characteristic set as *perspectives* is this repository's application, not part of the standard. |
| ML agents need capabilities coding agents lack: experiment tracking and reproducibility, data validation and **leakage detection**, statistical rigor, benchmarking against a real baseline | `mlops.py` four gates | `OpenJobsAI/awesome-ai-agents-for-ml` (category survey); MLE-Bench / MLAgentBench framing | A curated list, not a study. The specific leak patterns encoded (fit-before-split, group spillover, temporal shuffle, holdout reuse) are standard practice knowledge. |
| Hyperparameter search is a multiple-comparison procedure and is almost never treated as one | `mlops.multiple_comparison_note` | standard statistics | The family-wise figure assumes independent configurations, which a search over a structured space violates — so it is an upper bound, reported as such. |

## 8b. ML-agent ecosystem survey (52 projects)

Surveyed the full `awesome-ai-agents-for-ml` inventory — 52 projects across 11
categories. Most are frameworks or benchmarks whose *mechanisms* do not transfer
to a stdlib harness. Four did.

| claim adopted | where | source | limit |
|---|---|---|---|
| **Trial-and-error framed as a TREE search over candidate solutions beats a linear improve-the-current-thing agent.** Node = a solution; a hard-coded policy drafts until there are enough initial candidates, debugs while a buggy node is within a bounded depth, then improves the best non-buggy node. A summarization operator keeps only metrics, hyperparameters, and debug hints so the context does not saturate | `dobby/search.py` | **AIDE** — *AI-Driven Exploration in the Space of Code* (arXiv 2502.13138), WecoAI. MLE-Bench: AIDE+o1-preview **16.9%** medal rate, AIDE+GPT-4o **8.7%**, vs OpenHands+GPT-4o **4.4%**. Splits a holdout **before** the agent runs | Those medal rates are AIDE's, with a real LLM on Kaggle tasks. This implementation is the *policy and its bounds*, driven by an injected callable; it has been tested for correctness and has never been run against a model. No performance claim is made or implied. |
| **Compose inference-time layers** — generate → rank → fuse → critique → verify — rather than making one call. A composed stack of open-weight models can beat a single frontier call | `dobby/search.Layer`, `validate_pipeline`, `suggest_pipeline` | **Archon** (arXiv; ScalingIntelligence), Inference-Time Architecture Search. Reports 11–15% over GPT-4o for searched configurations | Archon *searches* the configuration space; this module validates and suggests, and does not search. The static validator is this repository's addition — it catches paid no-ops (ranking one candidate, fusing after a collapse, revising with no critique) that a running pipeline hides. |
| **Case-based reasoning over past tasks**: retrieve a similar prior case, adapt its approach, retain the outcome | `dobby/search.Case`, `retrieve_cases`, `retain_case` | **DS-Agent** (ICML 2024, arXiv 2402.17453) — case bank built from curated Kaggle human-insight cases | DS-Agent's bank is human-curated; this one accumulates from the harness's own runs, so it starts empty and is only as good as what has been retained. Storing the *approach* rather than the answer, and separating failed cases into an `avoid` list, are this repository's additions. |
| **Report the yield of an autonomous improvement loop.** Roughly 20 genuine improvements from about 700 experiments | `dobby/search.yield_report` | **autoresearch** (karpathy) — 5-minute training windows, keep-or-discard on validation bits-per-byte | **The README does not document how it separates a genuine improvement from run-to-run noise** — no statistical test, confidence interval, or false-positive control is described. That gap is why `mlops.compare_runs` refuses to call a single-run delta an improvement. The ~2.9% figure is used only as a *calibration expectation*, never as a target. |

**The most valuable finding was a benchmark's own honesty.** MLE-bench's "Known
Issues" catalogues leakage *in its own task set*: a dog-breed task drawn from a
publicly labelled dataset an agent can look up, a task whose public test-span
files leak the answer, and one containing a field that permits trivially perfect
prediction. It ships a **rule-violation detector** and a **plagiarism detector**
alongside the tasks. That is the evidence for `LEAKAGE_EXTERNAL_SOURCE` and
`RULE_VIOLATIONS` in `dobby/mlops.py`: a benchmark maintained by a frontier lab
found that clean-looking pipelines were not the binding constraint on trustworthy
results, and said so in public.

Categories surveyed and **not** adopted, with the reason: agent frameworks
(OpenHands, MetaGPT, AutoGen, CrewAI, LangGraph, CAMEL) are runtimes this harness
sits above rather than inside; RL training stacks (rllm, RLinf, Agent-R1,
AgentGym-RL, MARTI) require a training loop the kit does not have; MLOps
platforms (MLflow, Weave, Opik, Metaflow, ZenML) are services, and their
*reproducibility fields* were already covered by `check_reproducibility`;
AI-for-science agents (virtual-lab, ChemCrow, SciToolAgent) are domain tool
integrations.

## 8c. Design systems for agents

| claim adopted | where | source | limit |
|---|---|---|---|
| **Tokens alone produce generic output.** An explicit *aesthetic* — density, contrast strategy, what to avoid — is the missing half; two products with identical tokens still diverge without it. Named layout-section variants stop an agent inventing a structure per screen | `dobby/design.AESTHETICS`, `LAYOUT_SECTIONS`, and the `aesthetic` frontmatter check | `bergside/typeui` — 80+ design systems in DESIGN.md/SKILL.md form, 20+ layout categories, served over MCP | typeui serves a curated registry; this is six presets and eight section families, chosen to cover the range rather than to be exhaustive. No registry is fetched — the kit makes no network calls. |
| Generated palettes must meet WCAG contrast floors | `dobby/design.check_contrast` | WCAG 2.x (4.5:1 body, 3:1 large/UI) | Only text-on-surface pairs are checked. Testing every colour against every other produces a wall of irrelevant failures, and a report nobody reads is the same as no report. |

## 9. What is NOT established

The load-bearing negative section. Each item is a claim this repository is
**forbidden** from making until the named work is done.

1. **"This raises a weaker model to a stronger model's level."** Requires a
   controlled comparison across ≥2 model families with a positive holdout result.
   **Not run.** Enforced by `P-REPORT`.
2. **Any published metric from §5 or §6 as a property of this repository.** H-MEM's
   LoCoMo gains, ACON's 26–54%, RTK's 60–90%, caveman's 65%, code-review-graph's
   38–528× — all belong to those systems. This repository implements *mechanisms*
   from them, at lower fidelity (lexical instead of embedding; edge-list instead
   of AST), and measures none of them.
3. **The diversity metrics are not the ACL paper's metrics.** Only that paper's
   abstract was read. `effective_n`, `coupling_ratio`, `COLLAPSE_MPD`, and
   `SCATTER_MPD` are local constructions motivated by its finding.
4. **Semantic quality of retrieval, diversity, and claim support.** Everything is
   lexical overlap. Paraphrase reads as diversity; negation reads as similarity.
   Stated at every call site.
5. **Provider behaviour for `qwen`, `ollama`, `kimi`, `dashscope`.** Declared from
   documentation; never executed on the authoring machine. `verified_on` is empty
   for each, and `fleet` reports it.
6. **That the psychology results transfer to LLMs.** Diehl & Stroebe, Duncker,
   Ward, and Geneplore are human-subject findings. The transfer argument is stated
   in `swarm/grounding.py` and is an argument, not a measurement.
7. **That the ML gates detect real leakage.** They inspect a *described* setup. A
   leak that is not described is invisible to them.

## 10. Not implemented (known gaps)

Recorded so they are not mistaken for oversights.

- **Sandboxed execution** (context-mode's largest lever): raw payloads still enter
  context when a caller reads them directly.
- **AST-level call graph**: `blast_radius` consumes an edge list; there is no
  Tree-sitter parser, so import/call edges must be supplied.
- **Embedding retrieval**: deliberate (ADR-2), and it is the main ceiling on §9.4.
- **A model-judge adapter**: every `model_judgment` criterion is `NOT RUN`.
- **Inference-time architecture *search***: `suggest_pipeline` proposes and
  `validate_pipeline` checks, but nothing searches the configuration space the
  way Archon does. Searching requires an objective and a budget to spend on it.
- **A driver that runs `search.search` against real providers.** The policy,
  bounds, and honesty checks are implemented and tested; wiring the `expand`
  callable to `providers/fanout.py` is not done, so the tree search has never
  executed a model call.

Closed since 0.1.0: team topologies (`swarm/topologies.py`) and API-kind provider
transport (`providers/api.py`).

---

## How to extend this file

A new mechanism gets a row **before** it gets code, with its limit column filled
in first. If the limit column is hard to write, the mechanism is not understood
well enough to implement — that difficulty is the signal, not an obstacle.
