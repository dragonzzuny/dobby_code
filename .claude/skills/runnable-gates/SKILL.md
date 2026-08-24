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

- **The id is one token, immediately followed by the colon.** `G5:` yes;
  `G5 (manual):` NO - the space ends the id and the row stops being a
  gate. Characters: a letter or digit, then letters, digits, `.`, `_`,
  `-`. Anything you want to say about the gate goes AFTER the colon, in
  the title. This is worth stating because the failure is not local: one
  malformed title makes the whole ledger unparseable, so every other gate
  in it stops being graded too. Measured - an agent wrote
  `- [ ] G5 (manual, unscored): ...` and its four correct gates went
  ungraded with it.
- `CHECK:` a command. `EXPECT:` a literal substring, or `/pattern/flags` for a
  regex — `EXPECT: .*` is the three characters, not a wildcard.
- **Met = box ticked AND exit 0 AND EXPECT matched.** Any one alone is the
  failure `verification-and-completion.md` (1) names.
- `CWD:` overrides the working directory for that gate.
- A gate no command can decide carries no `CHECK:` and is reported as manual,
  never scored. Do not invent a command to make one look automated.
- **Who ticks the box, and when.** You do, and only when you BELIEVE the gate
  now holds — after doing the work it describes, before running `verify`. The
  box is your claim; the command and the match are the check on it. An unticked
  gate is never met however cleanly it runs, so a green command against an
  unticked box means you have not yet claimed it. Never tick a box to make a
  run go green: that inverts the whole mechanism, and `verify` cannot tell the
  difference — this is the one part of the contract only you can keep.

- **Do not gate the gate file on its own contents.** A CHECK that reads
  `GATES.md` and asserts something about the strings in it is almost always
  self-defeating, because the assertion has to be WRITTEN in that file to be
  made. Measured: an agent wrote a gate asserting that no `CHECK:` line still
  carried the literal `{python}` placeholder, and its own CHECK line carried
  that placeholder in order to test for it. Four sound gates passed; that one
  could not, and the agent had ticked its box. Gate the WORK, not the paperwork.

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
