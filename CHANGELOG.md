# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each entry records what changed **and what the change does not establish**.
A changelog that only lists additions lets unmeasured capability accumulate as
though it were proven.

## [Unreleased]

### Added — an execution runtime, because every primitive existed and no loop closed over them

`dobby/runtime/` + `dobby runtime run|resume|status|events|list`.

The harness could route a task, fan work out to several providers, isolate
mutating agents in worktrees, judge an output and record a trajectory. What
connected those was a person reading JSON between commands. That is a fine
orchestrator until the work outlasts the person's attention, at which point the
run has no state anybody can resume and no record of what it already did — so
recovery means starting over, paying for the finished half twice and hoping the
side effects were idempotent.

**Why this is a database.** Every other record here is append-only JSONL and
that is right for a record. Two things a resumable state machine needs are not
expressible in an append: `(run_id, node_id, attempt)` recordable exactly once
(a uniqueness constraint, which a file does not have), and leasing a node —
check `READY`, then claim — atomic against a second `dobby` process.
`core/jsonl.py` makes each *append* atomic, which is weaker: two processes can
both read `READY` and both append a lease. `sqlite3` is stdlib, so the
PyYAML-only dependency claim still holds. The event log is the truth and is
never updated; the tables are a projection written in the same transaction, and
`RunStore.rebuild()` recomputes one from the other so that claim is testable
rather than asserted. The JSONL trajectory is untouched — a record you can read
and a state you can resume are different jobs.

**What the tests establish**, and it is the reason this landed before any of the
scheduling work: a real process is killed and a second one resumes it. Three
nodes each append one line to a file; a subprocess runs two and calls
`os._exit(1)`; a second process resumes; the file has **three** lines and every
node has exactly one recorded attempt. A separate test kills a process *while* a
node is running and asserts the next runner closes the open attempt, frees the
lease and says so. Line count is the measurement because work that ran twice
cannot hide from it.

**Artifact contracts.** `PROPOSED → VERIFIED → PROMOTED`, and a node reads only
its dependencies' promoted payloads — enforced where the prompt is built, not
requested inside it. The machine promotion rule is fixed and not configurable at
runtime: schema clean, every acceptance check passed, and no check that failed
to run. A check that could not run *here* blocks promotion, which is stricter
than a linter needs and correct for the case that matters — a machine missing
the test runner would otherwise promote an unverified patch and report it as
verified. One defect this found in its own first run: the artifact file was
written before the promotion transition, so disk said `PROPOSED` while the store
said `PROMOTED`. Two answers to the one question the gate exists to answer.

**Failure classes, not a retry counter.** `retry_count` answers "how many times
has this broken" and never "is trying again the thing that could work". A 429
wants the same provider after a wait; a schema violation wants a different
approach, because resending the identical prompt to the identical model cannot
fix a shape; a failing test wants a repair step holding the failure text; a
missing approval wants a human and consumes no attempts. An *unrecognised*
provider failure is classified `NON_RETRYABLE` — matching on error prose is
fragile, and it fails safe this way: the run stops with the provider's own words
instead of spending three attempts on a permanently broken call. Authentication
is never transient.

**Side effects.** `idempotency_key = sha256(run_id, node_id, effect_version)` —
identity, not content, so a reworded retry collides. Claimed *before* the
effect: a crash in the claim-to-act window leaves a visible unconfirmed claim
that the run reports and a human resolves, where the other ordering leaves an
invisible duplicate. `EXTERNAL_IRREVERSIBLE` needs an explicit approval *and*
budget; `max_irreversible` defaults to 0, because a run acquires that right, it
does not inherit it from having been started.

**What this does NOT establish.** It does not make any run better. Nothing here
was compared against the previous hand-driven sequence on any task corpus, and
no such comparison is claimed. There is no provider scoring, no hedging, no
parallel node execution and no cost accounting — a selection policy fitted to no
outcome data is a random policy with a formula in front of it, so the store
records what one will need and nothing consumes it yet. Only the linear
`plan → execute → verify → report` graph is assembled by anything; `TaskGraph`
is a general DAG and the parallel implement/merge shape is expressible and
unbuilt. The verifier has its deterministic and grounded layers only; model
judgment stays advisory and stays an ordinary node, so it costs a visible
provider call. Recorded in `docs/RUNTIME.md` and in the known-gaps section of
`docs/RESEARCH_EVIDENCE_MATRIX.md`.

### Fixed — a re-pin that the commit which changed the body did not make

`ca624ea` revised `.claude/skills/paper-draft/SKILL.md` and left
`.dobby/registry/skills.json` at the previous content hash, so five tests failed
on `main` with "body hash differs from pinned origin". The change was reviewed
and legitimate; the pin is now what a review of it produced, and the skill is at
`1.1`. Nothing about the pin mechanism changed — this is the mechanism working
and the commit not finishing.

### Added — a delegation lane for Antigravity, and the fact that made it necessary

`dobby/agy.py` + `dobby agy` port
[claude-code-agy-CLI-skill](https://github.com/SafeMantella/claude-code-agy-CLI-skill).
What was taken is the judgment: delegate on an exclusive capability or on volume
(under ~5 tool calls do it here, over ~15 delegate), phrase the prompt for a
process that has no shared context, and validate what comes back. What was NOT
taken is its flag list or its `/Users/pedroarfux/.local/bin/agy` path — the flag
surface was re-measured from `agy --help` 1.1.8 on this machine and recorded
verbatim in `reports/AGY_FLAG_SURFACE.md`. Copying a documented flag list would
have shipped `--effort`, `--output-format`, `--json-schema` and `--mode` as
absent when all four exist, and this kit's own catalog docstring as "agy: text
only" when 1.1.8 speaks JSON.

**The measurement that changed the design.** The upstream skill passes
`--dangerously-skip-permissions` in most examples and never says why. It reads
as carelessness. Running the first real delegation without it, prompt = "state
in one sentence what dobby/agy.py is for":

    rc=0   stdout=0 chars   18.6s
    stderr: no output produced — a tool required the "command" permission that
            headless mode cannot prompt for, so it was auto-denied.

With permission granted: rc=0, 334 characters, a correct answer citing the file.
So in `--print` mode every delegation that must touch the tree returns a
**successful exit and nothing else** until permission is granted. The signature
looks exactly like a harness defect, and `run.py` was making that worse — its
exit-0-empty branch threw stderr away and guessed "try its explicit
output-format flag". It now reports what the child actually said, for every
provider, and `delegate()` states the remedy before the call rather than after
a wasted timeout.

Three defects the port exposed in code that was already here:

- `agy` appeared in the `implement` role routing with **no `write_extra`**,
  whose docstring defines empty as a refusal, so `swebench` would not run it as
  an implementer at all. Now `("--mode", "accept-edits")` — the spelling `agy
  --help` uses, not claude's `acceptEdits` — earned by execution, below.
- `_agy` pinned `--mode plan` and let callers append a second `--mode`. The
  default is now dropped when the caller supplies one, so exactly one `--mode`
  reaches the process and the argv does not contradict itself.
- A `--print-timeout` shorter than the harness's process ceiling makes the
  harness reap a healthy call and report "the tool may have fallen back to
  interactive mode". Both ceilings now come from one number, with the process one
  strictly larger, so a slow answer is explained by the tool that knows why.

**What this does not establish.** The upstream capability matrix — Google Search
grounding, `generate_image`, `codebase_investigator`, Chrome DevTools, 40+
science databases — is reproduced under `declared_upstream_not_verified_here`
and is NOT measured by this repository. `dobby agy caps` keeps the two apart.
One delegation was run end to end (research template, one file, permission
granted, 13.8s, correct answer with line citations); no template other than
`research` has been exercised against the live tool, and no `--write` delegation
has been run at all.

### Fixed — `--mode plan` was never read-only, and three places said it was

Filling `write_extra` needed the standard codex's carries: an executed run, not a
line of `--help`. The probe had a control arm — confirm `plan` PREVENTS a write —
with the threshold declared first. The control arm is what fired.

    prompt: create hello.txt containing DOBBY_WRITE_OK   (fresh temp dir each run)

    --mode plan          --dangerously-skip-permissions   FILE CREATED
    --mode accept-edits  --dangerously-skip-permissions   FILE CREATED
    --mode plan          (no permission flag)             FILE CREATED
    --mode accept-edits  (no permission flag)             FILE CREATED

Four for four, agy 1.1.8 / win32. `--mode` does not gate file writes on this
build. `catalog._agy` called `plan` "the same read-only default" as claude's;
`test_providers` asserted it under the name *"a scout must not silently edit the
tree"* and passed the whole time, because it checked that the string `plan`
appeared in the argv — true, and irrelevant to the property it was named for.

The flag is still sent, once, as a statement of intent. What replaced it as an
actual boundary was already in the repository: explicit `cwd` on every call, the
worktree isolation in `fanout.py`, and — new — an unconditional `Do NOT modify`
line in every read-only prompt `dobby/agy.py` builds, which is now the only
instruction-level control rather than a second layer over the mode.

The misleading assertion in `test_providers` was narrowed to the providers where
it holds, and the measured behaviour pinned in a test of its own. A check that
keeps passing while its own name is untrue is worse than no check: it is where
the next reader stops looking.

**Not established:** whether `--sandbox` changes any of it (untested), or whether
other builds of agy behave the same. One version, one platform.

### Added — `.hwp` became editable, by asking 한글 to do it instead of parsing it

`hwp5.py` reads HWP 5.0 and refuses to write it, for a reason that has not
changed: the body is compressed records inside a compound file whose sector
allocation would have to be rebuilt, and a half-correct writer produces damage
that only appears when 한글 opens the document. `dobby/hwpcom.py` takes the
other route — it drives an installed 한글 over COM, then reads the saved file
back with `hwp5.py` to confirm the edit is there. Writer and verifier are
different implementations; that is the only reason the confirmation is worth
anything.

New: `dobby hwp pages | export | shapes | edit`, and a Python API
(`page_count`, `export`, `paragraph_shapes`, `replace`, `available`).
`paragraph_shapes` reports font, size, weight, ratio, alignment and line
spacing per paragraph, which is what makes "does this document match the
template it was supposed to follow" a measurement rather than an opinion.

Five failure modes are encoded as behaviour, each found by losing time to it:

- `AllReplace` returns False and changes nothing on a build where `RepeatFind`
  finds the same string, so replacement is manual — select, read back, delete,
  insert.
- Retyping a paragraph flattens its inline runs, so a bold lead-in disappears.
  Edits are substring-precise and touch nothing outside the match.
- 한글's internal character offset runs ahead of the offset computed on
  extracted text, by an amount that grows with inline runs. The selection is
  probed and accepted only on a byte-equal read-back; a 28-occurrence replace
  lost 2 at a window of 8 and none at 40, which is now the default.
- Some characters — the en dash among them — cannot be found through COM text
  search at all, in strings `hwp5.py` locates without trouble. Patterns
  containing them are refused at the boundary with `split_at_unmatchable()`
  named, rather than reported as absent.
- A replacement whose new text contains its old text makes a find-from-start
  loop rediscover its own output forever. The scan advances past what it wrote.

**What this does not establish.** It was measured on one machine, one 한글
(2018, COM `10, 0, 0, 14454`), against one 15-page manuscript and its template:
roughly 40 substring replacements across body text, a title table and a page
header, each verified by reopening the saved file. It has not been run on
another 한글 version, on documents with tracked changes, footnotes or embedded
objects near the edit site, or on anything larger. The refusals are tested
everywhere; the editing path is tested only where 한글 is drivable and a
document is present, and skips — as a skip — otherwise. It also requires a
security module registered under `HKCU\Software\HNC\HwpAutomation\Modules`,
which `available()` detects and deliberately does not install.

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
