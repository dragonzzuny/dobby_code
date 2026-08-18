# dobby

[![ci](https://github.com/dragonzzuny/dobby_code/actions/workflows/ci.yml/badge.svg)](https://github.com/dragonzzuny/dobby_code/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-1448-3fb950)](tests/)
[![python](https://img.shields.io/badge/python-3.10%2B-4c8eda)](https://www.python.org/)
[![deps](https://img.shields.io/badge/dependencies-PyYAML%20only-4c8eda)](#install)
[![platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macos%20%7C%20windows-4c8eda)](.github/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-8b949e)](LICENSE)

A portable agent harness. Drop it into any repository and a capable model starts
behaving like a disciplined engineer: evidence before claims, scope held, outputs
validated, sessions continuous — and it gets better at *that* repository over
time without getting worse anywhere else.

```
Effective Agent = Base Model + Constitution + Context System + Skills
                + Tool Interface + Orchestrator + Memory + Evaluator
                + Improvement Loop
```

The model is the part you cannot change. Everything else is this repository.

Requirements: Python 3.10+ and PyYAML. No other dependencies. No network calls in
the engine. Runs on Linux, macOS, and native Windows.

---

## Install

```bash
git clone https://github.com/dragonzzuny/dobby_code
cd dobby_code

# Linux / macOS
./install.sh /path/to/your-project

# Windows (PowerShell)
.\install.ps1 -Target C:\path\to\your-project
```

Then, inside your project:

```bash
python -m dobby.cli init --scan .     # scan the repo, build its knowledge base
python -m dobby.cli doctor            # what works here, what does not, and why
```

`doctor` is the honest starting point. It reports which providers exist, which
roles can be filled, and which checks fail — with the fix for each.

## Five minutes of actual use

```bash
# 1. Brief yourself before touching anything
python -m dobby.cli context "add rate limiting to the upload endpoint"

# 2. See what the harness thinks the job needs
python -m dobby.cli route "add rate limiting to the upload endpoint"

# 3. Get several genuinely different opinions, not one opinion four times
python -m dobby.cli panel "how should rate limiting be scoped here?" --size 4

# 4. Plan a review that partitions the search space
python -m dobby.cli review --reviewers 4 --risk security,reliability

# 5. Prove it works before you say it works
python -m dobby.cli slice --scenario SELF-CHECK

# 6. Run it durably — and kill the process to see that it resumes
python -m dobby.cli runtime run "add rate limiting to the upload endpoint" \
    --execute "pytest -q tests/test_ratelimit.py" --check "pytest -q"
python -m dobby.cli runtime resume <run_id>

# 7. Work a project across sessions: the loop halts where only you can proceed
python -m dobby.cli project init --smoke "pytest -q" --items items.json
python -m dobby.cli project run --until empty --execute "pytest -q"
```

The contract installs a door for each harness — `CLAUDE.md`, `GEMINI.md`,
`QWEN.md`, and `AGENTS.md` which Codex and opencode read natively. Each harness
reads only the file named for it, so a rule that exists in a form the running
agent cannot find is worse than an absent one.

Seven skills ship with it: `dobby` (the entry protocol), `ledgered-task`,
`bootstrap-project`, `author-evals`, and three for the work this was built
against — `contest-submission` (공모전: rubric, disqualification grounds, prior
art *before* drafting), `prior-art-search` (특허·규제, official registry first,
searched-and-empty kept apart from unsearched), and `paper-draft` (논문: claims
priced, citations resolved, rigor gates, generated-prose signature removed).

Inside Claude Code there is one entry point for all of it: type `/dobby` followed
by what you want, in any language. It compiles the ask, asks the router which
skill and which agency rung apply, fans the work out across independent providers
when the rung calls for it, and verifies the output before reporting. The protocol
is `.claude/skills/dobby/SKILL.md`; the slash command is `.claude/commands/dobby.md`.
Both install into a host project.

---

## What is actually in here

### Runs that survive the process that started them — `dobby/runtime/`

Every primitive below existed before this, and nothing closed a loop over them.
The connective tissue was a person: run a command, read the JSON, decide, run
the next one. That works until the work outlasts the person's attention — at
which point the run has no state anybody can resume and no record of what it
already did.

A `TaskRun` is a DAG of nodes with an append-only event log behind it (SQLite,
standard library, no new dependency). Kill the process at any point and
`runtime resume` continues from what actually happened:

```bash
python -m dobby.cli runtime run "migrate the auth module" --provider claude \
    --execute "pytest -q" --check "pytest -q|ruff check ."
python -m dobby.cli runtime resume 20260816-134052-3bf951
python -m dobby.cli runtime status 20260816-134052-3bf951
```

Four guarantees, each tested against a real killed process rather than asserted:

- **Finished work is not repeated.** `(run_id, node_id, attempt)` is recordable
  exactly once — a uniqueness constraint, which is why this is a database and
  not another JSONL file. The test runs three nodes that each append one line,
  kills the process after two, resumes, and counts **three** lines.
- **An unverified artifact is never an input.** `PROPOSED → VERIFIED →
  PROMOTED`, and a node reads only its dependencies' *promoted* payloads. The
  promotion rule is fixed: schema clean, every acceptance check passed, and no
  check that failed to run. A gate a failing run can lower is not a gate.
- **An external effect happens at most once.** The idempotency key is derived
  from identity, not content, and is claimed *before* the effect — so a
  reworded retry collides, and a crash leaves a visible unconfirmed claim rather
  than an invisible duplicate. `EXTERNAL_IRREVERSIBLE` nodes need an explicit
  approval and a budget that allows them; `max_irreversible` defaults to 0.
- **A failure is classified before it is retried.** `retry_count` answers the
  wrong question. A 429 wants the same provider after a wait; a schema violation
  wants a different approach, because resending the identical prompt to the
  identical model cannot fix a shape; a failing test wants a repair step holding
  the failure text; a missing approval wants a human and costs no attempts.

Above that sits one correlated trace per run, and a placement policy that reads
it:

```bash
python -m dobby.cli runtime trace <run_id>     # a waterfall of where time went
python -m dobby.cli runtime metrics            # and what it says about health
python -m dobby.cli runtime scorecard          # per provider, per node kind
python -m dobby.cli runtime harvest --write    # failures that recur -> candidates
python -m dobby.cli runtime bench --corpus t.json
```

- **Placement is measured, not configured.** `U = wq·quality − wc·cost −
  wl·p95 − wr·recent-failures`, where *quality* is the share of that provider's
  attempts on that kind of node that survived the **verifier** — not that exited
  zero. Optimising for exit codes selects for providers that answer fast and
  wrongly. A provider with no record competes on an optimistic prior, which is
  the entire exploration policy: there is no bandit, because a policy fitted to
  zero samples is a random policy with a formula in front of it.
- **Metrics say `None`, never 0, when nothing was measured.** Zero is a
  measurement; absence is not, and collapsing them is how a dashboard reports
  0% success for a system nobody has run. Cost per verified task is reported as
  *unmeasurable here* with the reason, because CLI providers do not report token
  usage and a tier is not a price.
- **Recurring failures become candidates, never golden tasks.** A repeated
  failure is evidence that something recurs — a real defect, a broken check and
  a task nobody should have asked for all look identical. A human promotes; the
  file merges, so that decision survives the next harvest.
- **The benchmark ships no corpus and refuses to rank fewer than eight paired
  tasks.** A benchmark whose tasks come with the tool measures its authors'
  imagination.

What it deliberately does not do — the hedge is decided and never raced, no cost
accounting, no benchmark result, one advisory judge rather than a panel — is
listed with reasons in [docs/RUNTIME.md](docs/RUNTIME.md).

### Projects that outlive the run — `dobby/project/`

A run is the wrong unit for work that outlives an afternoon. It ends, and the
next session opens a repository it has never seen, re-derives what the test
command is, and sometimes re-implements what was finished on Tuesday.

The project kernel is the unit above it: a **manifest** (what this project is
and how it is checked, frozen), a **baseline** (whether the tree was sound, and
against which code), a **portfolio** of work items whose acceptance is
*commands* rather than sentences, and a **session envelope** — the minimum a
fresh worker needs, which is deliberately not a transcript.

- **The run decides what is done, not the worker.** An item becomes `DONE` only
  when a runtime run ended `SUCCEEDED`, promoted at least one artifact, and left
  no unconfirmed external effect. The store cannot express "the agent said it
  finished".
- **Selection is arithmetic.** The same portfolio in the same state yields the
  same next item, so an interrupted session continues rather than reconsiders.
  The one judgement left to a model is not *which* item but whether an item is
  gradeable at all — reported as `needs_architect`, never decided.
- **It stops at boundaries and names them.** `dobby project run --until empty`
  drains the portfolio and halts on one of ten declared reasons —
  `baseline_failed`, `needs_architect`, `needs_reconciliation`, `item_blocked`,
  and so on — because a caller deciding whether to fetch a human cannot parse a
  sentence. A blocked item is a stop, not a skip.
- **An architect may widen the gate, never lower it.** `--architect` asks a
  read-only model to make an ungradeable item gradeable, and it may only choose
  acceptance commands the project already declares. Dropping a check is refused
  outright; an invented one, a destructive one or a raised side-effect class
  stops for a person. The request is recorded before the model is called, and
  the plan, the decision and the portfolio change land in one transaction.
- **It re-baselines between items,** since the item that just succeeded changed
  the tree. If the project's own smoke checks then fail, everything stops and the
  failure is attributed to the item that caused it.

Six invariants, each enforced in one place and each with a test that fails when
it does not hold. Writing those tests found three defects the docstrings had
asserted and the code had not — including a dirty-tree refusal that had never
once fired. Design, and what it deliberately does not do:
[docs/PROJECT.md](docs/PROJECT.md).

### Multi-provider fleet — `dobby/providers/`

Drives **claude**, **codex**, **gemini**, **agy** (Antigravity), **qwen**,
**ollama** (local llama/qwen/mistral), and OpenAI-compatible APIs
(**kimi**/Moonshot, **DashScope**) as child processes. Roles route by cost:
breadth roles go cheap and many, decision roles go expensive and few.

Availability is always **measured, never assumed**, and reports three states,
because the fix differs for each — `available`, `absent` (install it), `blocked`
(a config or key problem). API providers are off unless
`providers.allow_network` is set, because they reintroduce a network egress path
the MCP gateway deliberately does not have.

```bash
python -m dobby.cli fleet            # who is here
python -m dobby.cli fleet --probe    # prove it with one cheap real call each
```

### Delegating to Antigravity — `dobby/agy.py`, `dobby agy`

A fleet member you can hand a whole task to, with a gate in front of it. Ported
from [claude-code-agy-CLI-skill](https://github.com/SafeMantella/claude-code-agy-CLI-skill),
whose delegation policy is right and whose flag list was re-measured here rather
than copied — `reports/AGY_FLAG_SURFACE.md` is the verbatim `agy --help` this
lane is built on.

```bash
python -m dobby.cli agy check "search the web for Flask 3.x CVEs"   # spends nothing
python -m dobby.cli agy prompt "review the launch path" --template review \
      --file dobby/providers/run.py                                 # spends nothing
python -m dobby.cli agy run "..." --template research --file f.py \
      --skip-permissions --yes                                      # one real call
```

Most delegations should not happen, so the decision is a command of its own:
under ~5 tool calls do it here, over ~15 delegate, and an exclusive capability
(live web search, image generation, a browser, a science database) outranks the
arithmetic. `check` returns the verdict **and its basis**; `basis: unknown`
means no estimate was given, which is a missing decision rather than a default.

Four things the lane encodes because each one cost real time:

- **exit 0 with no output is a permission auto-deny, not a harness bug.**
  `--print` mode cannot prompt, so any delegation that must touch the tree is
  denied and exits successfully with nothing. Measured 2026-08-04 (agy 1.1.8):
  0 characters in 18.6s; with permission granted, 334 characters and the right
  answer. `run_provider` now reports the child's stderr instead of guessing
  about output formats, and the lane warns *before* the call, not after.
- **the print timeout must expire before the process ceiling**, or the harness
  reaps a healthy call and blames interactive mode. Both come from one number.
- **`--mode plan` is not containment, and the catalog used to say it was.**
  Measured four ways — plan and accept-edits, each with and without
  `--dangerously-skip-permissions`, each in a fresh temp directory, prompt
  "create hello.txt" — the file appeared **four times out of four**. What
  actually bounds a delegate is the working directory it is launched in, the
  worktree isolation in `fanout.py`, and the instruction in the prompt. The flag
  is still sent, exactly once, as a statement of intent.
- **absolute paths, always.** A relative path in a delegated prompt resolves
  against the delegate's cwd and produces a confident answer about another tree.

### Multi-agent that actually disagrees — `dobby/swarm/`

Running N agents does not give N opinions. Dense interaction makes panels
converge before the minority view is stated — **structural coupling** — and the
orchestrator then reads that collapse as consensus, and therefore as confidence.

So decorrelation is a mechanism here, not a hope:

- **Isolated generation first.** Every protocol has a phase where members cannot
  see each other. This is the Nominal Group Technique, and it is the intervention
  — group brainstorming produces *fewer* and less creative ideas than the same
  people working alone.
- **Assigned lenses.** SCAMPER, Six Thinking Hats, dialectic roles, and
  per-failure-mode critic lenses. Agents that pick their own angle pick the
  obvious one, which is the one their peers pick.
- **Measured spread.** `effective_n` answers "how many independent opinions did I
  actually buy?" Six identical answers report as ~1.0.

```bash
python -m dobby.cli panel "redesign the retrieval layer" --size 4 --protocol double_diamond
```

### No fluent nonsense — the grounding gate

Unconstrained ideation produces ideas rated *more* novel than expert ideas and
*less* feasible — and they score worse when executed. Optimizing for novelty
optimizes for that failure. So:

- ideation is **blocked before prior art is retrieved**;
- every idea must anchor to a real evidence id (fabricated ids are caught by
  resolution, not by style);
- every idea must carry a **falsifiable test** — an idea that cannot be wrong
  survives review by being unfalsifiable rather than by being good;
- an idea that merely restates its own evidence is rejected as paraphrase.

Rejections come back as a **histogram**, because a panel failing on
"no test" needs a different fix than one failing on "fabricated citation".

### Six-tier memory — `dobby/memory/`

```
nation → mountain → forest → tree → branch → leaf
```

Five hops from root to any leaf. Each tier **remembers differently**, because one
mechanism is wrong at both ends of an abstraction gradient:

| tier | scope | mechanism |
|---|---|---|
| nation | the domain | fixed vocabulary + counts — never expires, because routing from a moving root is unstable |
| mountain | a subsystem | prototype centroid — one cheap similarity test per candidate |
| forest | a module cluster | co-occurrence adjacency — mid-level knowledge *is* relationships |
| tree | one artifact | slotted card, with missing slots reported not invented |
| branch | one episode | verified-first with recency decay |
| leaf | one raw event | append-only, first to be compressed away |

Retrieval routes layer by layer over child indexes, so the system never scans a
whole tier. Promotion is gated and **audited for leakage**: a summary that drops a
file path or a negation is refused, because that is corruption, not compression.

On "is it an LSTM": no, and it does not claim to be. What a gated recurrent cell
actually contributes is a per-item forget/admit/expose decision, and that is
implemented explicitly in `memory/gates.py` — inspectable, which a learned weight
is not.

### Generalist → domain expert — `dobby/specialize.py`

The cheapest route to domain performance is overfitting, and an overfitted
harness is worse than a generic one on the next task. So every specialization
step must clear **two** gates at once:

1. the project's own gold set improves, and
2. the shipped generic gold set does not regress **on any single case**.

Per-case is the point: an average hides a trade where five generic cases gain a
little and two collapse. Mastery level is derived from counted evidence — never
from elapsed sessions, because a harness that has run fifty sessions and learned
nothing is not an expert.

### Code review from QA/QC evidence — `dobby/review.py`

**Perspective-based reading**, not a checklist — PBR reports lower cost per defect
and less inspection time, for the same reason decorrelated panels beat identical
ones. Perspectives are the ISO/IEC 25010 quality characteristics, so coverage is
a defensible claim rather than an invented one.

- **QA and QC stay separate.** Was the process followed, versus does the output
  behave. Merging them is how a green CI run gets reported as quality.
- **Severity is priced, not guessed.** Ordering is `severity × containment cost`,
  anchored on the published ratios — a defect caught in development costs ~6×
  less than one in production and ~100× less than one causing an incident.
- **Two defect families, two lists.** Functional defects block a merge;
  evolvability defects never do. Listing a naming nit beside a race condition
  trains the author to skim.
- A finding with no reproducible scenario is reported as **not actionable** and
  excluded from the decision.

### ML and data analysis — `dobby/mlops.py`

A coding agent asks "does it run and pass". Both can be true while an ML result
is worthless, and the failure is silent: a leaked target gives 0.99 AUC that
*reports as success*. Four gates, in the order failures actually happen:

1. **Leakage** — fit-before-split (proven from step ordering, not guessed),
   target-derived features, group spillover, temporal shuffling, holdout reuse.
2. **Reproducibility** — seed, data version, pinned environment, exact command.
3. **Statistical rigor** — a single run's delta is not an improvement; does it
   even beat predicting the majority class; what did trying 60 configurations
   cost in selection bias.
4. **Interpretation** — where a correct number becomes a wrong claim (causal
   language from a predictive design, unbounded generalization).

Confirmed leakage **short-circuits everything downstream**: reporting a
statistical comparison next to a leaked score would lend it the appearance of
rigor.

```bash
python -m dobby.cli ml --file experiment.json
```

### Token efficiency, honestly accounted — `dobby/tokens.py`

Three mechanisms, adopted; the overstated arithmetic that usually travels with
them, not adopted.

- **Per-command output condensers.** Generic compression cannot know that in
  pytest output the failures matter and the 200 passing dots do not.
- **Priority-tiered snapshots** inside a hard byte budget. P1 working state
  outlives P4 trivia, and everything dropped is *named* — a snapshot that
  silently omits half the blockers reads as "there were no blockers".
- **Blast radius** over the knowledge graph: read what depends on the change
  instead of reading the tree. Avoiding a read beats shrinking one.

Every number is labelled: condensing tool output cuts **input** tokens on later
turns; a brevity instruction cuts **output** tokens and costs resident input
tokens *every* turn; neither touches reasoning cost. `estimate_savings` will tell
you when a style constraint is a **net loss** — because it often is.

**Failing commands are never condensed.** The detail that explains a failure is
the first thing a summarizer drops.

### Sandboxed execution — `dobby/sandbox.py`

Compression makes a large output smaller. This stops it entering context at all.

```bash
dobby sandbox run --command "{python} build.py"
# → exit_code, a 241-byte preview, and a handle. Measured on a 5001-line run:
#   63,935 bytes produced · 241 bytes into context · 99.6% withheld

dobby sandbox extract --handle <h> --pattern ERROR
# → matched 1 of 5001 source lines
```

Output is **withheld unless asked for**, which inverts the usual default.
Compressing 320 KB to 10% still costs 32 KB of context; extracting three
matching lines costs 200 bytes.

**Failing commands are never condensed**, extractions are bounded twice (lines
*and* characters, because either alone is escapable), and a command that exceeds
its size cap is *killed* rather than truncated — a run whose tail is missing
looks complete, and the tail is where failures print.

The isolation is honest about its limits. `Result.network_blocked` is always
`False`: proxy variables are cleared and offline hints set, but a determined
binary can still open a socket, and real isolation needs a namespace or a
container. It is a boundary against **accidents**, not hostile code.

### Time, progress, and where it went — `dobby/progress.py`, `dobby/spend.py`

```bash
dobby panel "..." --size 4 --progress     # live bar on stderr
dobby spend                               # per-provider breakdown
dobby spend --line                        # one line, for a host status bar
```

```
panel:adversarial: [#########---------------] 3/8 ~2m left  (1 failed)

running 2: codex:security 1m13s, claude:correctness 51s | eta ~1m02s
  | agents 8 · 4m19s spent · 2.29x parallel · 1 failed | top claude 2m22s
```

Most progress bars lie, because they assume every remaining unit costs what the
average finished one cost — false when provider calls vary by an order of
magnitude. So this one:

- **refuses to estimate below 3 completions** — one sample measures one thing,
  not a rate;
- reports a **range** from observed spread, widening when work is erratic;
- extrapolates parallel work on **waves, not items** — a round finishes when its
  slowest member does, so six agents in two waves cost two round-trips;
- reports **agent time and wall time separately**. Agent time is what was
  *bought*; wall time is what was *waited*. Their ratio is the parallelism
  actually achieved, and either number alone misleads in the opposite direction.

To put it in Claude Code's status bar, set `statusLine` in `settings.json`:

```json
{ "statusLine": { "type": "command",
                  "command": "python -m dobby.cli spend --line" } }
```

### Casual request → executable prompt — `dobby/prompt.py`

```bash
dobby prompt "이거 좀 개선해줘"
# → ask: What does '이거' refer to? Name it explicitly.  (3 rounds at risk)
#   gaps: context, objective, acceptance, scope
```

"Efficient prompting" is used to mean two things that pull opposite ways — fewer
tokens, and better results. A well-specified prompt is almost always **longer**.
The cost that actually dominates is neither: it is **retries**. A prompt missing
its acceptance criterion produces a plausible wrong answer, costing the round
that produced it, the round that reviewed it, and the round that fixed it.

So this compiles for specification, reports the token cost of doing so, and
never claims the result is shorter. And it **does not guess**: an unresolved
ambiguity becomes a listed question, because a compiler that picks a file has
not specified the task, it has changed it.

One question is returned, not five — ordered by retry cost, because a caller
handed five questions answers them partially and a caller handed the expensive
one answers it.

**On translating Korean prompts to English: don't.** Translation inserts a guess
into a system built to avoid guessing, and the real problem was never the
language — it was that the tokenizer could not read Hangul (fixed; see below).
Identifiers, paths, and commands stay verbatim in every language.

### The generated-prose signature — `dobby/style.py`

```bash
dobby style --file draft.md
dobby style --text "..." --rewritten edited.md   # scores the change budget
```

The obvious approach — ban a word list — fails, because the words are not the
tell. Human writing contains "however" too. What marks generated prose is
**uniformity**: sentences clustered at one length, a comma after every
connective, hedges stacked until nothing is asserted, lists arriving in threes
because three sounds complete.

So the primary signals are distributional. Mean sentence length says little;
the **standard deviation** says a great deal. Run on this session's own prose it
returns `uniform_sentence_length` at S1, which is the correct answer.

Three severities, because one occurrence is sometimes enough and sometimes not:
**S1** deterministic (a comma after a Korean connective ending is a translation
artifact, not a choice), **S2** at three or more, **S3** only in overlap.

Rewrites are bounded: ≤30% target, **abort above 50%** — past that a
"humanizing" pass has replaced the author's writing with the rewriter's. The
module detects and instructs; it does not rewrite, because that needs a model
and a heuristic paraphraser would be the lossy step the discipline exists to
prevent. Taxonomy adapted from the MIT-licensed `im-not-ai` / Humanize-KR
project.

### Design contract — `DESIGN.md`

YAML token frontmatter plus prose rules, validated:

```bash
python -m dobby.cli design
```

Validation's real target is **tokens declared without the prose that says when to
use them**. An agent that knows the values and not the rules applies them
arbitrarily.

### 한글 documents — `dobby/hwpx.py`, `dobby/hwp5.py`

Korean proposals, papers and government forms arrive as `.hwp` or `.hwpx`, and a
harness that cannot open them cannot help with the work.

- **Read both, edit HWPX.** `dobby hwp text|info|paragraphs|tables|find` works on
  either format. `replace` and `set` write HWPX only. HWP 5.0 writing is not
  implemented and is refused with the reason: its body is compressed records
  inside a compound file whose sector allocation would have to be rebuilt, and a
  half-correct writer corrupts documents in ways that only surface when 한글
  opens them.
- **The edit is a byte splice.** Only the character data being changed moves;
  every other byte of the section is identical afterwards. Re-serialising the XML
  would rewrite all fourteen namespace declarations, and whether 한글 still opens
  that is not a question to answer with somebody's submission. Measured: 12 of 12
  real documents are byte-identical after a no-op save.
- **Character data, not elements.** Of 4151 `<hp:t>` elements in the real corpus,
  65 contain child elements — 26 of 48 in one paper summary. Treating the element
  as a string would have destroyed inline markup in exactly the file being worked
  on.
- **Refusals say why.** A replacement that crosses two runs is declined, not
  silently skipped, and reports the runs — because returning zero reads as "the
  text is not there", which is the opposite of the truth. Writes never overwrite
  the source; `--out` is required.
- **No new dependency.** The compound-file reader is stdlib. Where `olefile` is
  installed the tests use it as an ORACLE: both readers agreed on every stream of
  all 12 real `.hwp` documents.

```bash
python -m dobby.cli hwp info    "제안서.hwpx"
python -m dobby.cli hwp tables  "제안서.hwpx"          # a form is its cells
python -m dobby.cli hwp replace "제안서.hwpx" --text "기존" --with "새로" \
                                --out "제안서_수정.hwpx"
python -m dobby.cli hwp text    "논문.hwp"             # legacy binary, read-only
```

### Research and paper verification — `dobby/research.py`

- **Plans, and then the search.** One query returns *enough* — plausible results
  that stop the search before the contradicting source appears. Plans always
  include a refutation and a limitation query built from the same terms.
  `dobby research run "<need>" --yes` executes the plan against a provider that
  declares a `web` capability; anything else would answer from memory and the
  output would be indistinguishable from a search. **An empty result is reported
  as `NOTHING RETRIEVED`, never as "no prior art exists"** — for a contest whose
  rules disqualify an idea already in force, a false absence is the most expensive
  output the system can produce. Every source comes back as a CLAIM, unresolved.
- **Claim strength drives the evidence bar.** "may help" needs an example;
  "improves 40%" needs the measurement; "always" needs the proof.
- **Citations resolve or they don't.** Fabricated references are stylistically
  perfect, so detection is resolution against an independently retrieved corpus,
  reported at three severities. An empty corpus reports `NOT CHECKED` — never a
  clean bill of health.

---

## Layout

```
AGENTS.md          the operating contract — read this first
CLAUDE.md          Claude adapter → AGENTS.md
DESIGN.md          the design system, machine-readable

dobby/core/        proven engine: knowledge graph, router, policies, skills,
                   evaluator, trajectory, optimizer, improvement loop, evolution
dobby/runtime/     durable execution: task graph, event-log store, artifact
                   contracts, verifier gate, failure classes, node leases, resume
dobby/project/     the unit above a run: manifest, baseline, portfolio, session
                   envelope, and the loop that carries one verified item at a time
dobby/providers/   provider fleet + parallel fan-out
dobby/agy.py       Antigravity delegation lane: gate, templates, flag guards
dobby/memory/      six-tier memory + gates + compression
dobby/swarm/       protocols, diversity metrics, grounding gate
dobby/specialize.py  generalist → expert, dual-gated
dobby/review.py    PBR review, QA/QC split, priced severity
dobby/mlops.py     leakage / reproducibility / rigor / interpretation
dobby/tokens.py    output condensers, snapshots, blast radius
dobby/research.py  search planning, claim + citation verification
dobby/research_runner.py  runs the plan; absence, failure and refusal kept apart
dobby/hwpx.py      HWPX read + edit; the edit is a byte splice, not a rewrite
dobby/hwp5.py      HWP 5.0 read; compound file parsed with the stdlib alone
dobby/design.py    DESIGN.md validation, aesthetics, contrast
dobby/search.py    solution-tree search, layer composition, case bank
dobby/search_driver.py  the search, driven by real providers and a real objective
dobby/judge.py     model judgment as ADVISORY evidence, never as verification
dobby/sandbox.py   execution whose output never enters context
dobby/progress.py  ETA that refuses to guess
dobby/spend.py     where the session's agent time went
dobby/prompt.py    casual request -> executable prompt, gaps named not guessed
dobby/style.py     the generated-prose signature (English + Korean)

.dobby/            PROJECT DATA — ontology, knowledge, policies, config
.claude/rules/     scoped rules
.claude/skills/    procedures
mcp/               optional MCP gateway: 4 meta-tools, allowlisted, no network
evals/             retrieval gold (dev / val / holdout)
tests/             1448 tests
docs/              architecture, project kernel, operating manual, failure
                   catalog, threat model, research evidence matrix
```

## Verify it yourself

```bash
python -m unittest discover -s tests -q      # 1448 tests
python -m dobby.cli slice --scenario SELF-CHECK
python -m dobby.cli doctor
```

## Honest limits

Read these before believing anything above.

- **End-task effect is still not measured. Compliance now is.** That the harness
  raises a weaker model's *performance* requires a controlled comparison on an
  established issue-resolution benchmark with the model held fixed. That has not
  been run, the claim stays forbidden in this repository's reports, and it is not
  made here.

  What has been measured is narrower and worth stating precisely. `dobby endtask`
  asks whether the preamble changes output in the direction it specifies —
  compliance, not benefit. `codex`, 6 tasks x 2 reps, 36 trials, `pass^k`:

  | behaviour | bare | padded (length control) | harness |
  |---|---|---|---|
  | names what was not verified | 0.0 | **0.0** | **1.0** |
  | scopes to the named files | 0.333 | **0.333** | 0.833 |
  | separates done from not-done | 0.833 | **0.833** | 1.0 |

  | comparison | mean delta | 95% CI |
  |---|---|---|
  | bare → harness | +1.500 | [1.00, 2.00] |
  | bare → padded | +0.333 | [−0.08, 0.67] — no measurable effect |
  | **padded → harness** | **+1.167** | **[0.58, 1.75]** |

  The third row is the result. `padded` replaces the preamble with filler matched
  character for character — 6127 prompt characters in both conditions against 185
  bare — and length alone moves nothing, landing on *exactly* the bare value for
  three behaviours. The content moves all three. Five of six tasks improved, one
  tied, none regressed. Cost: a 33x longer prompt, and agent time did not rise.

  An earlier 2-task run of this reported a *regression* in one behaviour as a
  headline finding. It did not survive six tasks and is retracted in
  `docs/RESEARCH_EVIDENCE_MATRIX.md` — the run's own degenerate-interval caveat was
  correct.

  Still: one provider, the two holdout tasks untouched, and compliance is not
  benefit. Design and prior art: `docs/EVAL_DESIGN.md`.

  For scale on why the unmeasured claim matters: on a fixed backbone,
  [Claw-SWE-Bench](https://arxiv.org/abs/2606.12344) reports 19.1% Pass@1 with a
  minimal adapter against 73.4% with a full one. The harness is worth about as much
  as the model, which is exactly why asserting an effect without measuring it would
  be indefensible.
- **Retrieval and diversity are lexical.** No embeddings — stdlib only. Two
  answers saying the same thing in different words score as diverse; two
  differing only by a negation score as similar. These metrics reliably catch
  collapse and wide spread, and are weak in between.

  There is now a number on how weak. A live panel ran while a bug was truncating
  one member's prompt at its first newline, so that member answered from the
  opening line alone. Re-running the same task on the same panel afterwards moved
  mean pairwise distance from 0.8824 to 0.8883 and `effective_n` from 1.882 to
  1.888 — while the answers went from a plausible causal chain that did not
  survive checking to specific line-range citations of the relevant file.
  `effective_n` answers *are these worded differently*. It is not evidence that
  anything is well grounded; that is the grounding gate's job.
- **Token numbers are estimates**, at 4 chars/token with no tokenizer. Budgeting
  aids, not bills.
- **The ML gates read descriptions, not data.** They check whether the split
  happened before the scaler was fit *as recorded*. They cannot see a leak you
  did not describe.
- **`qwen`, `ollama`, `kimi`, and `dashscope` have never been executed** — the
  authoring machine does not have them, and their `verified_on` is empty. `fleet`
  reports this per provider rather than assuming it works.

  `claude`, `agy` and `codex` *have* now answered a live probe with the exact
  expected token (29.1s / 14.6s / 31.0s). Before that probe was first run, two of
  them could not start a process at all while `fleet` called them `usable: true` —
  the launcher used the bare binary name, and on Windows `shutil.which` consults
  PATHEXT while `CreateProcess` does not, so every npm `.CMD` shim resolved and
  then refused. `gemini` launches and is refused by its own service for an
  account-tier reason, which is a condition of the account rather than of the
  invocation.
- **A model judgment never counts as verification.** `dobby slice --judge` and
  `Evaluator(judge=True)` do grade `model_judgment` criteria now, but the result
  is `advisory`: excluded from the PASS/FAIL verdict in both directions, capped at
  confidence 0.6, and reported separately with the judging provider named.
  `.dobby/ontology.json` forbids a `model_assertion` from being `verified`, and
  `evaluate()` previously satisfied that rule only by accident — it counted every
  non-`None` result as deterministic, and judgments always returned `None`.
  Judging is opt-in because it costs money and leaves the machine. No simulated
  model results, ever.
- **A tree search needs a real objective, and will not invent one.**
  `dobby search` scores candidates by running `--score-command`, never by asking a
  model to rate its own output — that maximises self-assessment and reports steady
  improvement while producing nothing better. With no score command every
  candidate is uncomparable, no node is viable, and the output says so instead of
  substituting a default number.

## License

Apache-2.0.
