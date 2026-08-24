---
name: runnable-gates
description: Write acceptance gates as runnable commands BEFORE the work, then let a script decide whether they passed. Use for any producing task whose completion you would otherwise assert in prose, and whenever a report is about to say "done". Complements ledgered-task, which tracks requirements; this decides them.
---

# runnable-gates

**Trigger:** any producing task; any moment you are about to write `done`,
`fixed`, `passing`, or `verified`.
**Non-trigger:** read-only questions that produce no artifact.

`ledgered-task` gives a requirement a row and an evidence path. Nothing checks
that the path supports the claim, because the same agent writes both. This adds
the part a script can fail you on. `dobby/gates.py` grades it; you do not.

## The contract

A gate is a markdown checkbox row with indented attributes:

```
- [ ] G1: the suite is green
  CHECK: python -m unittest discover -s tests
  EXPECT: /OK$/m
  EVIDENCE:
```

- `CHECK:` a command. `EXPECT:` a literal substring, or `/pattern/flags` for a
  regex — `EXPECT: .*` is the three characters, not a wildcard.
- **Met = box ticked AND exit 0 AND EXPECT matched.** Any one alone is the
  failure `verification-and-completion.md` (1) names.
- `CWD:` overrides the working directory for that gate.
- A gate no command can decide carries no `CHECK:` and is reported as manual,
  never scored. Do not invent a command to make one look automated.

## Steps

1. **Write the gates first**, into `GATES.md`, before the first change. Every
   requirement in the ledger that produces something gets one. A gate written
   after the output exists is a description of what happened.
   ✓ `python -m dobby.cli gates status --file GATES.md` exits 0.
2. **Read the commands, then approve.** `status` parses and never executes, so
   this is the point at which you see what is about to run.
   ✓ `python -m dobby.cli gates approve --file GATES.md` lists each record path.
   An approval covers that exact command, expectation, cwd, shell, platform and
   PATH. Change any of them and it is void — that is the mechanism, not a bug.
3. **Do the work.**
4. **Verify.** ✓ `python -m dobby.cli gates verify --file GATES.md
   --write-evidence` — exit 0 only when every runnable gate is met, 1 for an
   unmet or unapproved gate or a run that checked nothing, 2 for a ledger that
   does not parse.
5. **A gate that turns out impossible is ABANDONED, never deleted.**
   `ABANDON: G3 no GPU on this host` on its own line, with a reason. Deleting it
   instead is Evaluation Gaming (`docs/FAILURE_CATALOG.md`) with the evidence
   removed.
6. **Report.** Quote the summary. `ok: false` is reported as Blocked or
   Partially complete per `escalation-and-reporting.md`, never softened.
   ✓ every unmet gate named in the report by id.

## What this does not do

- It does not check that a gate is the RIGHT gate. `EXPECT: ` matching an empty
  string passes on any output; a checker cannot tell you that you asked it
  nothing. Gates are reviewed like tests, in the diff.
- It does not make manual gates true.
- `--no-approval` exists for CI where the ledger is already reviewed in a diff.
  It is not a way to skip reading a command before running it.
