---
description: Run a free-text request through the dobby entry protocol (compile → brief → route → execute → verify)
argument-hint: <what you want, in any language>
---

The user's request, verbatim:

$ARGUMENTS

Handle it by following `.claude/skills/dobby/SKILL.md` in this repository, in
order. Do not begin work from the sentence above as written — step 1 exists
because a casual ask usually names an activity rather than an end state, and
implementing a guessed requirement correctly is the most expensive failure this
harness has.

If `$ARGUMENTS` is empty, do not guess a task. Say what `/dobby` expects and show
`python -m dobby.cli doctor` output so the user can see what works here.
