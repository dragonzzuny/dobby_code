---
name: dobby
description: Front door for a free-text request in any language. Compiles the ask, lets the router pick the skill and the agency level, fans the work out to several independent AI providers as subprocesses, then verifies the output. Use when the user types /dobby followed by free text, or opens a task with a one-line ask that names no acceptance criterion.
---

# dobby

`/dobby 포스터 만들어줘` is a direction, not a task. This skill turns it into
something executable, asks the harness which rung it belongs on, and — when the
rung calls for it — runs several independent providers as subprocesses instead of
thinking about it alone. Doing that by feel is the failure mode; every step below
is a command whose output decides the next one.

All commands are `python -m dobby.cli <...>`. In an installed host project
`.\dobby <...>` (PowerShell, cmd.exe) and `./dobby.sh <...>` (sh, bash, zsh) are
the same thing. The module form is used here because it is a real executable and
carries multi-line arguments; a `.cmd` shim truncates an argument at its first
newline, measured.

## 1. Compile the request

```
python -m dobby.cli prompt "<the user's text, verbatim>"
```

`specified: true` — the ask names an end state and a way to check it. Use the
returned `prompt` as the brief.

`specified: false` — `gaps` lists what is missing, each with a `question` and a
`retry_cost` in rounds likely wasted if you guess. Ask the SINGLE highest-cost
question and nothing else, and only if the answer changes what you would do. If
it does not, proceed and record the assumption.

Do not fill a gap by inference from the repository. `prompt` already looked; an
empty slot means the request did not say.

## 2. Ask the router which skill and which rung

```
python -m dobby.cli route "<the compiled objective>"
```

Two fields drive everything after this.

**`skills`** — the harness's own checklists that apply. Read the named
`.claude/skills/<name>/SKILL.md` and follow it; that is the "find the right skill"
step, and it is a lookup, not a judgement call. `ledgered-task` in particular is
selected structurally from the router's own multi-requirement verdict, so when it
appears the task really does have more than one requirement.

**`level`** — the agency rung, and therefore the orchestration shape:

| level | what it means | what to do |
|---|---|---|
| 1 | a deterministic script covers it | run the script; no model needed |
| 2 | simple response | answer directly |
| 3 | producing, single requirement | one agent, structured, then verify |
| 5 | producing with multiple requirements or a critical policy | plan → execute → evaluate, with the evaluation done by a DIFFERENT provider |

Level 5 is where the fan-out belongs. Levels 1–3 do not buy anything by adding
providers, and spending three provider calls on a one-line edit is its own defect.

## 3. Fan out to independent providers, as subprocesses

```
python -m dobby.cli fleet                  # what is installed and usable here
python -m dobby.cli panel "<task>" --protocol ngt --size 3 --dry-run
python -m dobby.cli panel "<task>" --protocol ngt --size 3
```

`panel` launches each provider as its own subprocess, in isolation, and returns
their answers separately. Protocols: `ngt` (silent generation first, so nobody
anchors on anybody), `double_diamond`, `six_hats`, `dialectic`, `adversarial`.
Pick `adversarial` when the question is "is this actually right", `ngt` when it is
"what are the options", `dialectic` when two positions are already on the table.

`--dry-run` shows the panel and the prompts without spending anything. Use it
first when the task is long or the panel is large.

**Your own second pass is not a second opinion.** It is correlated with your
first, and reporting it as independent corroboration is the specific failure
`dobby/swarm/diversity.py` exists to catch. When you need corroboration, it comes
from a different provider or it does not count.

Splitting the work is separate from splitting the opinion. When a task has
genuinely independent parts, give each part to a different provider and say in
the report which provider produced which part — a merged answer with no
attribution cannot be audited.

## 4. Research asks: the plan is not the finding

```
python -m dobby.cli research plan "<need>"          # decomposes, searches nothing
python -m dobby.cli research run  "<need>" --yes    # actually searches
```

`run` makes one real provider call per query shape and costs money, which is why
`--yes` is required; without it the command prints the calls it would make. Only
providers that declare a `web` capability are accepted — anything else would
answer from memory and the output would be indistinguishable from a search.

Read `prior_art_verdict` exactly as written. `NOTHING RETRIEVED` means these
queries surfaced nothing; it never means nothing exists. Every source returns as a
CLAIM of a source, unresolved — open the URLs before relying on one.

## 5. Verify the output, then report

A producing command exiting 0 says nothing about what it produced. Check the
artifact itself at the strongest level available here, and state which levels were
not available on this machine.

Report in the structure `.claude/rules/escalation-and-reporting.md` fixes: outcome
first with failures in the opening paragraph, a requirement→evidence table, the
commands run with their verdicts, assumptions with L-levels, and what could not be
verified. A number appears only if a command produced it this session.

## When not to use this

A single-command question, a one-line lookup, an ask that already names its own
acceptance check. Running a five-step protocol and a three-provider panel over
"what does this function return" is the same category of error as skipping them on
real work.
