# dobby

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
```

---

## What is actually in here

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

### Design contract — `DESIGN.md`

YAML token frontmatter plus prose rules, validated:

```bash
python -m dobby.cli design
```

Validation's real target is **tokens declared without the prose that says when to
use them**. An agent that knows the values and not the rules applies them
arbitrarily.

### Research and paper verification — `dobby/research.py`

- **Search plans, not searches.** One query returns *enough* — plausible results
  that stop the search before the contradicting source appears. Plans always
  include a refutation and a limitation query built from the same terms.
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
dobby/providers/   provider fleet + parallel fan-out
dobby/memory/      six-tier memory + gates + compression
dobby/swarm/       protocols, diversity metrics, grounding gate
dobby/specialize.py  generalist → expert, dual-gated
dobby/review.py    PBR review, QA/QC split, priced severity
dobby/mlops.py     leakage / reproducibility / rigor / interpretation
dobby/tokens.py    output condensers, snapshots, blast radius
dobby/research.py  search planning, claim + citation verification
dobby/design.py    DESIGN.md validation

.dobby/            PROJECT DATA — ontology, knowledge, policies, config
.claude/rules/     scoped rules
.claude/skills/    procedures
mcp/               optional MCP gateway: 4 meta-tools, allowlisted, no network
evals/             retrieval gold (dev / val / holdout)
tests/             344 tests
docs/              architecture, operating manual, failure catalog, threat
                   model, research evidence matrix
```

## Verify it yourself

```bash
python -m unittest discover -s tests -q      # 344 tests
python -m dobby.cli slice --scenario SELF-CHECK
python -m dobby.cli doctor
```

## Honest limits

Read these before believing anything above.

- **End-task effect is not measured.** That the harness raises a weaker model's
  performance requires a controlled comparison across ≥2 model families with a
  positive holdout result. That comparison has not been run. Until it has, the
  claim is forbidden in this repository's reports, and it is not made here.
- **Retrieval and diversity are lexical.** No embeddings — stdlib only. Two
  answers saying the same thing in different words score as diverse; two
  differing only by a negation score as similar. These metrics reliably catch
  collapse and wide spread, and are weak in between.
- **Token numbers are estimates**, at 4 chars/token with no tokenizer. Budgeting
  aids, not bills.
- **The ML gates read descriptions, not data.** They check whether the split
  happened before the scaler was fit *as recorded*. They cannot see a leak you
  did not describe.
- **`qwen`, `ollama`, `kimi`, and `dashscope` are declared but unverified here** —
  the authoring machine did not have them. `fleet` reports this per provider
  rather than assuming it works.
- **Model-judgment evaluator criteria are always marked NOT RUN** until a judge
  adapter exists. No simulated model results, ever.

## License

Apache-2.0.
