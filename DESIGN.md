---
aesthetic: utilitarian
colors:
  background: "#0e1116"
  surface: "#161b22"
  surfaceRaised: "#1c2230"
  border: "#2d333b"
  text: "#e6edf3"
  textMuted: "#8b949e"
  primary: "#4c8eda"
  primaryHover: "#6ba3e5"
  verified: "#3fb950"
  unverified: "#d29922"
  blocked: "#f85149"
  skipped: "#6e7681"
typography:
  display:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 600
    lineHeight: "1.25"
    letterSpacing: "-0.02em"
  heading:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: "1.35"
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: "1.6"
    letterSpacing: "0"
  mono:
    fontFamily: "JetBrains Mono, SFMono-Regular, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: "1.55"
    letterSpacing: "0"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: "1.2"
    letterSpacing: "0.06em"
spacing:
  scale: [0, 4, 8, 12, 16, 24, 32, 48, 64]
radius:
  scale: [0, 3, 6, 10, 999]
elevation:
  flat: "none"
  raised: "0 1px 2px rgba(0,0,0,0.4)"
  overlay: "0 8px 24px rgba(0,0,0,0.5)"
components:
  evidenceBadge:
    radius: 999
    paddingX: 8
    paddingY: 2
    typography: "label"
  verdictBanner:
    radius: 6
    padding: 12
    borderLeftWidth: 3
  tierRow:
    radius: 3
    paddingY: 8
    typography: "mono"
---

# DESIGN.md — dobby

## Overview

Everything this harness shows a human is **evidence about a system's state**: a
verdict, a provenance level, a measurement, a gap. The design has exactly one
job, and it is not decoration — it must make the *epistemic status* of a value
visible before the value itself is read.

That leads to one governing rule, from which the rest follows:

> **Confidence is encoded in the interface, never left to the prose.**

A measured number and a model's guess must not be able to look the same. When
they do, a reader averages them, and the harness's central discipline —
evidence before claims — is defeated at the presentation layer, after all the
work of enforcing it was already done.

Two consequences shape every decision below:

- **Absence is content.** "Not measured", "not run", "not verified here" are
  first-class states with their own visual treatment, not empty space. Blank
  space reads as "fine".
- **Density over comfort.** The audience is reading a report to make a decision.
  Generous whitespace that pushes the failure list below the fold is a
  correctness problem, not a taste problem.

## Colors

Four semantic states, and they are the load-bearing part of the palette:

| token | meaning | where it may appear |
|---|---|---|
| `verified` | observed this session by a command | provenance badges, passing checks |
| `unverified` | asserted, plausible, unconfirmed | model assertions, suspicions, provisional results |
| `blocked` | failed, refused, or invalid | failed checks, confirmed leakage, merge blockers |
| `skipped` | deliberately not evaluated | `NOT RUN` criteria, unreached perspectives |

`skipped` is deliberately grey and low-contrast, and this is the one place where
low contrast is correct: a skipped check must not compete for attention with a
failed one. It must still be *legible* — a skipped check that disappears reads as
a passed check, which is the failure mode this whole palette exists to prevent.

`primary` is reserved for actions the user takes. It never indicates status.
Using the action colour for a state is how "informational" gets misread as "good".

### Do not

- Never use `verified` green for "the command completed". Completion is not
  verification — a producing command exiting 0 says nothing about its output.
- Never use red for a *suspicion*. `unverified` amber is for anything the system
  cannot prove; red is reserved for the proven-bad. Crying wolf on suspicions
  costs a real investigation.
- Never rely on hue alone. Every state carries a text label, because roughly 1
  in 12 readers cannot separate the amber/green pair.

## Typography

Five roles, no more. Each has a job:

- `display` — the one verdict at the top of a report. There is only ever one.
- `heading` — section boundaries.
- `body` — explanation and rationale.
- `mono` — **anything a human might retype**: paths, identifiers, commands,
  hashes, numeric measurements. This is a correctness rule dressed as a type
  rule. Proportional digits and ambiguous `l`/`1`/`I` produce transcription
  errors in exactly the values that must be exact.
- `label` — uppercase state badges. Tracked wide (`0.06em`) because uppercase at
  11px without extra letter-spacing loses word shape.

Numbers that will be compared against each other are tabular and
right-aligned. A column of scores that cannot be scanned vertically is a table
that has to be read twice.

## Layout

`spacing.scale` is a 4px base with a deliberate jump from 32 to 48: mid-range
steps (36, 40) invite arbitrary choices, and the gap forces a decision between
"related" and "separate".

Reports follow a fixed order, and the order is the contract:

1. **Verdict** — one line, first, always. Never below the evidence it summarizes.
2. **Failures and gaps** — before successes. A reader who stops after the first
   screen must have seen the bad news.
3. **Evidence table** — requirement → status → evidence path.
4. **Method** — what was run, with verdicts.
5. **Limits** — what was not checked, and what could not be checked here.

Section 5 is never optional and never collapsed by default. A report whose
limitations are behind a disclosure triangle is a report that overstates itself
to anyone who does not click.

Measurement text wraps; **paths and commands never wrap** — they scroll inside
their own container. A wrapped path is a path that gets copied wrong.

## Elevation & Depth

Three levels only, and depth means *containment*, never importance:

- `flat` — the page.
- `raised` — a grouped result (one provider's answer, one tier's contents).
- `overlay` — something that interrupts, i.e. a required decision.

Elevation is not used to emphasize. A critical finding is critical because of its
colour, label, and position — if a shadow were doing that work, the same finding
would become invisible the moment it appeared in a flat context like a terminal
or a plain-text ledger, which is where most of this output actually lands.

## Shapes

`radius.scale` runs `[0, 3, 6, 10, 999]`. Assignments:

- `0` — tables and code blocks. Sharp corners align with monospace grids.
- `3` — inline chips, tier rows.
- `6` — cards, verdict banners.
- `10` — modals.
- `999` — pill badges (provenance, state) only.

The pill radius is reserved for state badges specifically, so a pill shape
carries meaning on its own: **if it is a pill, it is telling you the confidence
level of something.** Spending that shape on a decorative tag would remove a
signal that works even in grayscale.

## Components

### evidenceBadge

A pill stating provenance. Required next to any claim about system state.

- Content is always `LABEL` + optional source, e.g. `VERIFIED · tests/test_kg.py`.
- Never renders empty. Unknown provenance renders `UNVERIFIED`, because a missing
  badge reads as verified.

### verdictBanner

One per report. A 3px left border in the state colour, `display` type, and the
verdict sentence.

- Must state the outcome in its **first four words**: `Done`, `Partially
  complete`, `Blocked`, `INVALID`, `NOT_ESTABLISHED`.
- May not contain hedging. If the outcome is uncertain, the outcome is
  "Partially complete" and the uncertainty belongs in the limits section.

### tierRow

One row per memory tier or panel member. `mono` type, `raised` on hover only.

- Shows the tier's own mechanism, not just its name. A row reading `forest` tells
  the reader nothing; `forest · co-occurrence adjacency · 12 items` tells them
  why the retrieval behaved as it did.

## Do's and Don'ts

**Do**

- Put the verdict first, the failures second, and the limits last but visible.
- Attach an `evidenceBadge` to every state claim, including the good ones.
- Use `mono` for every value a human might retype.
- Render "not measured" as a visible `skipped` state with a reason.
- Keep the palette's four semantic states doing only their assigned jobs.

**Don't**

- Don't let a failure be summarized as "mostly fine" — the banner takes the
  worst state present, not the average.
- Don't collapse the limits section, and don't move it above the evidence.
- Don't introduce a fifth type role or a sixth colour state. Both are signs that
  a distinction belongs in the *data model* rather than in the styling — that is
  where it can be tested.
- Don't animate a state change. These interfaces are read to make decisions;
  motion draws the eye to whatever moved last rather than to whatever is worst.
- Don't use `primary` blue for status, or `verified` green for completion.
