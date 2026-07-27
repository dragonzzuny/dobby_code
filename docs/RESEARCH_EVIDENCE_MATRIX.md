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

- **AST-level call graph**: `blast_radius` consumes an edge list; there is no
  Tree-sitter parser, so import/call edges must be supplied.
- **Embedding retrieval**: deliberate (ADR-2), and it is the main ceiling on §9.4.
- **Inference-time architecture *search***: `suggest_pipeline` proposes and
  `validate_pipeline` checks, but nothing searches the configuration space the
  way Archon does. Searching requires an objective and a budget to spend on it.
- **`qwen`, `ollama`, `kimi`, `dashscope`**: catalogued from documentation and
  never executed here. Their `verified_on` is empty and must stay empty until a
  run fills it.
- **HWP 5.0 writing** (`dobby/hwp5.py` reads only): the body is compressed
  records inside a compound file whose sector allocation would have to be
  rebuilt. Refused rather than approximated, because a half-correct writer
  corrupts documents in ways that surface only when 한글 opens them.
- **HWPX paragraph insert/delete** (`dobby/hwpx.py` replaces text only):
  both need new markup, which reintroduces the namespace-rewrite problem the
  byte-splice design exists to avoid.
- **Korean `와`/`과` as a multi-requirement signal**: three detection rules were
  measured and all produced 7–9 false positives out of 10 on sentences where the
  syllable sits inside one word (`결과`, `효과`, `성과`, `학과`). Nothing was
  added; see §"Korean requests were routed as if they were trivial".

**This list was stale and is corrected here.** It carried "a model-judge adapter"
and "a driver that runs `search.search` against real providers" as unimplemented
after both had been built and run — `dobby/judge.py` (175 lines, live panel round
recorded below) and `dobby/search_driver.py` (278 lines, driven against real
providers). A known-gaps section that lists closed items is worse than no section:
its whole purpose is to be the thing a reader trusts about what is missing.

Closed since 0.1.0: team topologies (`swarm/topologies.py`), API-kind provider
transport (`providers/api.py`), and sandboxed execution (`dobby/sandbox.py`,
measured at 99.6% of a 5001-line output withheld from context).

### Closed on 2026-07-26 by running the thing instead of describing it

**Live provider execution.** Until this date no provider had ever been invoked.
The gap was not documentation debt — it was hiding a defect that made most of the
fleet unusable. `run_provider` launched the bare binary name with `shell=False`,
and on Windows `shutil.which` consults PATHEXT while `CreateProcess` does not, so
every npm-installed `.CMD` shim resolved and then refused to start. First
`dobby fleet --probe` run, native Windows 11:

| provider | before the fix | after |
|---|---|---|
| claude | ok, replied `DOBBY_OK`, 34.9s | ok, 29.1s |
| agy | ok, replied `DOBBY_OK`, 61.8s | ok, 14.6s |
| codex | `cannot execute 'codex'`, 0.14s | **ok, replied `DOBBY_OK`, 31.0s** |
| gemini | `cannot execute 'gemini'`, 0.14s | launches, 22.2s → `IneligibleTierError` |

`gemini`'s remaining failure is an account condition, not an invocation defect.
Three providers are now verified end-to-end. Before this date, zero were, while
`verified_on=(WIN,)` claimed otherwise for all four.

**A real multi-agent round.** `dobby panel` had only ever run `--dry-run`. First
live round, NGT protocol, 3 members: 2 succeeded, wall 194.9s against 313.3s of
agent time (parallelism 1.61, slowest member 184.4s), diversity computed on real
text rather than fixtures — mean pairwise distance 0.8824, `effective_n` 1.882,
verdict `scattered` with the advice to tighten the task statement before adding
voters.

**The diversity metric did not notice that one member had been half-blinded.**
That first round ran while a `.CMD` shim was truncating multi-line prompts at
their first newline, so one of the two members saw only the prompt's opening line.
The same task was re-run on the same panel after the truncation was fixed:

| | truncated prompt | intact prompt |
|---|---|---|
| mean pairwise distance | 0.8824 | 0.8883 |
| `effective_n` | 1.882 | 1.888 |
| distinct 2-gram | 0.971 | 0.966 |
| coverage tokens | 204 | 197 |
| verdict | `scattered` | `scattered` |

The numbers are effectively unchanged, and the answers were not. With the prompt
intact, one member cited specific line ranges of the relevant source file and
argued from a caveat that file states about itself; with it truncated, the same
member had produced a plausible causal chain that did not survive checking. A
metric that cannot separate those two situations is not measuring answer quality.

This is the §9.4 limitation with a number attached rather than a new finding —
`swarm/diversity.py` is lexical overlap and says so at every call site — but it is
worth stating concretely: `effective_n` answers "are these answers worded
differently", and it should never be read as "are these answers well grounded".
The grounding gate, not the diversity metric, is what addresses that.

The round also earned its keep as a review. One member identified an unguarded
`os.path.relpath` in the sandbox confinement path — correct — and attributed it to
a live crossing between `providers/fanout.py`'s worktree root and
`sandbox.run`'s `root=`. **That attribution did not survive checking:** no
production call site passes `root=` at all, and the two roots are unrelated. The
exception type was the real defect and is now fixed (`SandboxError`, not a bare
`ValueError` that every `except SandboxError` would miss). Another member
prescribed setting `PYTHONUTF8: "1"` in the workflow, which is the one change
`.github/workflows/ci.yml` documents as forbidden because it masks the entire
class of encoding failure the matrix exists to surface.

Both outcomes are the argument for the grounding gate stated in
`swarm/grounding.py`: a specific, well-cited, confident answer from a strong model
was partly wrong, and the disagreement between members was what the diversity
metric flagged. Panel output is a hypothesis set, never a finding.

**A real tree search.** `search.search` had implemented DRAFT/DEBUG/IMPROVE and
never executed a model call. First live run, `codex`, 3-node budget, objective a
command scoring five static properties of the candidate (max 5):

```
best_score        4.0        provider_calls    3
nodes_evaluated   3          answered          3
actions           2 draft, 1 improve           scored            3
buggy             0          agent_seconds     33.39
stopped_because   budget_exhausted
```

Two things in that result are worth more than the score.

**The best node was a DRAFT, not the IMPROVE.** The improve step did not beat the
first draft. At a 3-node budget that is not evidence against tree search — the
published MLE-Bench comparison this module cites ran far longer — but it is exactly
what `stopped_because` and `selection_bias_warning` exist to surface, and it would
have been easy to report "4.0/5, search worked" without noticing which action
produced it.

**The objective and the prompt contradicted each other.** The prompt said "output
only the code"; the scorer awarded a point for a docstring. The missing point is
not a model failure, it is a defect in the setup — and no amount of searching can
resolve a conflict between the instruction and the metric. Anyone wiring a real
objective should check that the score is reachable under the prompt they are
sending. The candidate itself was correct: stack-based, all three bracket pairs,
early return on mismatch.

The score deliberately comes from **static** properties — `compile()` parses
without executing, and the feature checks are AST and regex — because
`--score-command` normally means running model-generated code and the sandbox is
not wired into that path. That is recorded as an open risk in
`docs/THREAT_MODEL.md` §5 rather than treated as solved.

### The harness's own effect, measured — 2026-07-26

Until this date nothing in the repository measured what the README claims.
`docs/EVAL_DESIGN.md` has the design and the prior art that set the metrics
(`pass^k` from τ-bench, cost accounting from Claw-SWE-Bench and HAL). Read the
framing there first: this is a **compliance** experiment, not a benefit one.

`codex`, 6 dev tasks, 2 repetitions per cell, paired within task, 36 trials, no
failed calls. `pass^k` is the fraction of tasks where the behaviour held in EVERY
repetition.

| behaviour | bare | padded (length control) | harness |
|---|---|---|---|
| names what was not verified | 0.0 | **0.0** | **1.0** |
| scopes to the named files | 0.333 | **0.333** | 0.833 |
| proposes a verification step | 0.667 | 1.0 | 0.833 |
| invents no unsupported figure | 1.0 | 1.0 | 1.0 |
| separates done from not-done | 0.833 | **0.833** | 1.0 |

| comparison | mean paired delta | 95% CI | verdict |
|---|---|---|---|
| bare → harness | +1.500 | [1.00, 2.00] | compliance increased |
| bare → padded | +0.333 | [−0.08, 0.67] | **no measurable effect** |
| **padded → harness** | **+1.167** | **[0.58, 1.75]** | **compliance increased** |

**The third row is the result.** `padded` is the harness preamble replaced by filler
matched to it character for character — 6127 prompt characters in both conditions
against 185 bare. Length alone moves nothing measurable, and on three behaviours it
lands on *exactly* the bare value: 0.0, 0.333, 0.833. The preamble's content moves
all three. That comparison is what the control was built to make possible, and
without it the effect and the token count are indistinguishable.

Per-task, harness minus padded: +2.0, +2.0, +1.5, +1.0, +0.5, 0.0. Five of six
improved, one tied, none regressed.

**A retraction.** An earlier run of this experiment used 2 tasks and reported, as a
headline finding, that the harness made one behaviour *worse* — "proposes a
verification step" falling from 1.0 to 0.5. **It did not survive six tasks:** the
same behaviour is 0.667 → 0.833 here, an improvement. The n=2 result was noise, the
run's own caveat said the interval was degenerate and the effect roughly twice the
observed baseline variance, and that caveat was correct. The instruction-dilution
explanation offered for it should be discarded along with it. Reporting the
regression prominently was right; leaving it standing would not be.

The degenerate-interval flag is also gone at this sample size — [1.00, 2.00] has
width because the six per-task deltas genuinely differ, which is the difference
between an interval and an artefact.

Cost, reported because this literature reports it: 6127 prompt characters against
185, a 33× increase, for +1.5 behaviours out of 5. Agent time did not rise — 788s
harness against 825s bare and 880s padded, over twelve calls each.

What this still does NOT establish:

- **Compliance is not benefit.** That a stated limitation reduces downstream defects
  is a separate study with different subjects. This measures whether the
  instructions land, which was genuinely unknown and is the default failure of long
  preambles.
- **Circularity is inherent.** The preamble asks for the behaviours the checks look
  for. The informative outcome was always the null one; the control is what makes
  the non-null result mean *content* rather than *instruction-following in general*.
- **One provider.** Each provider is its own experiment.
- **The holdout is untouched.** Two `holdout` tasks remain unrun, for one reported
  claim, once.
- **Six tasks is a probe.** It licenses "on these tasks, with this provider".

The definitive validation remains a Pass@1 run on an established issue-resolution
benchmark with the model held fixed. It has not been run, and this does not
substitute for it.

**And the eval earned its keep before producing a valid number.** Its first run died
in under a second on all four harness calls, because the preamble contains this
repository's own rule text — `"3 failures" without the three names is not a finding`
— and the double quote broke the PowerShell launch route added two commits earlier.
That route had been tested against newlines and percent signs and never against a
quote. The corrected three-route measurement is in
`dobby/core/platform.py::npm_shim_target`.

A second defect surfaced the same way: one cell was lost to a 120s timeout tighter
than the provider's observed 108s, and `deduplicate` kept whichever trial was
recorded first — so re-running the lost cell appended a line the summary ignored. It
now prefers a successful trial over a failed one for the same cell, which is what
makes `--trials-out` / `--from-trials` able to repair a pool rather than just grow
it.

Still open from the list above: AST call graphs and inference-time architecture
search. `qwen`, `ollama`, `kimi` and `dashscope` remain unexecuted, and their
`verified_on` is still empty. The model-judge adapter and the `search.search`
driver are now closed, both with live measurements above.

---

### The search plan became a search — 2026-07-27

`research plan` decomposed an information need into six query shapes and searched
nothing. The artifact looked like research and contained no findings.

`dobby research run "<need>" --yes` now dispatches each shape to a provider that
declares a `web` capability — measured: `claude` and `gemini` do, `codex`, `agy`,
`qwen`, `ollama`, `kimi` and `dashscope` do not, and any of them would have
answered from memory in a form indistinguishable from a search.

**One live call, `claude`, 266.7s**, on `산업안전보건법 산업단지 전기차 화재 대응
규정`: 8 bullet entries returned, **6 real sources with URLs** (소방청 press
release, 정책브리핑 ×2, 매일노동뉴스, 한국화재보험협회 KFS-1130, 국가법령정보센터),
and the reply's own note that the law.go.kr page returned a navigation shell rather
than article text. Verdict `PRIOR ART CLAIMED`; citation report `NOT CHECKED` with
`awaiting_resolution: 8`.

Three defects that only a real call could surface:

1. **The plan was searching one word.** `plan_queries` filtered terms with
   `len(t) > 3` — a threshold calibrated for Latin function words, while Korean
   content words are two or three characters. Measured across four needs: 13→1,
   11→1, 7→1 tokens kept for Korean, 6→6 for English. Every Korean search ran on
   the single longest surviving word. `swarm/diversity.py` had already solved this
   with `_MIN_CJK_TOKEN_LEN = 2` and a comment naming the exact hazard; the new
   `research.query_terms` reuses that predicate instead of restating it.
2. **Markdown emphasis counted as sources.** The reply used `**FOUND:**` and
   bullet lines whose whole body was `*`, inflating `sources_claimed` from 6 to 8.
3. **`NOT FOUND:` contains `FOUND:`.** A substring search would have read the gap
   list as the source list — absence reported as evidence.

The reporting rule this module exists for: an empty result is `NOTHING RETRIEVED`
with the queries that produced it, never "no prior art exists". Provider failure
and provider refusal are separate verdicts (`INCOMPLETE`) from a search that ran
and found nothing, because all three produce zero sources and merging them is how
a false absence is manufactured.

Still not established here: nothing resolves a returned URL, so every source is a
CLAIM. Resolution needs an independently retrieved corpus, which this machine does
not have.

### Korean requests were routed as if they were trivial — 2026-07-27

Matched-pair method: the same request in both languages, level and tier compared.
**7 of 12 pairs diverged**, every one an authoring request. `논문 초안 작성` routed
level 2 / tier small / "simple response task"; `write the paper draft` routed level
3 / medium. `작성`, `만들`, `제작`, `수정`, `개선`, `설계`, `번역` were all absent
from `PRODUCING_KW`, while the destructive stems `삭제` and `배포` were present and
fired — which is why the list looked bilingual.

The pairing then found the reverse: adding the Korean stems made `보고서 만들어줘`
produce while `make the report` did not, because `make` and `design` were missing
from the English half.

Both directions were fixed under a measured guard. `make`, `design`, `update`,
`설계`, `수정`, `개선` are ordinary nouns as well as verbs, so they are producing
signals only when nothing in the sentence asks a question — otherwise `check the
design of this module` and `개선 사항 확인` buy a higher rung and a larger model for
a read-only task. After the change: 1 of 11 pairs diverges, 0 of 15 read-only
sentences over-escalate, 0 of 14 producing sentences under-route.

**Left open, measured rather than guessed:** Korean joins nouns with `와`/`과`, so
`포스터와 제안서 작성` is two deliverables the router cannot see. Three detection
rules were tested against sentences where the syllable sits inside one word
(`결과 보여줘`, `효과 분석`, `성과 지표`, `이 학과 자료`): the bare particles gave 9
false positives out of 10, and `[가-힣][와과]\s+[가-힣]` still gave 7, because
`결과 보` matches it. Substring matching cannot separate the particle from the
syllable without morphology, so nothing was added.

`applicable_when` in the skill registry held two kinds of entry and only one was
text. `>1 requirement` and `>~10 expected tool calls` describe router state and
were substring-matched against the task, so no sentence could satisfy them and
`ledgered-task` was unreachable by any input — while `create|build evals` split on
the pipe into the bare token `create`, surfacing `author-evals` for `create the
report`. Structural conditions are now evaluated against what the router computed;
`first run in a new repository` stays dead and is recorded as such, because the
router has no repository-freshness signal and inventing one would be a guess.

---

## How to extend this file

A new mechanism gets a row **before** it gets code, with its limit column filled
in first. If the limit column is hard to write, the mechanism is not understood
well enough to implement — that difficulty is the signal, not an obstacle.
