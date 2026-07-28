---
name: contest-submission
description: Prepare a 공모전 / grant / call-for-proposals submission. Read the announcement, extract the judging criteria and the disqualification grounds, check the idea against prior art BEFORE writing, map every proposal section to a criterion, and verify the form is actually filled. Use when the task involves 공고문, 제안서, 심사기준, or any competitive submission with a deadline and a rubric.
---

# contest-submission

A proposal is not judged on how good it is. It is judged on a rubric, by someone
reading dozens of them, after a screening step that throws entries out for
reasons that have nothing to do with quality. This orders the work so the cheap
disqualifying checks happen before the expensive writing.

The order matters more than any single step: **prior art before drafting**. An
idea already in force nationally is a disqualification in most Korean public
competitions ("이미 국가 차원에서 시행되고 있거나 기본 구상이 매우 유사한 경우"),
and discovering that after the proposal is written costs the whole draft.

## 1. Read the announcement as data, not as prose

```
python -m dobby.cli hwp info    "<공고문>"      # .hwp and .hwpx both
python -m dobby.cli hwp tables  "<공고문>"      # criteria live in tables
python -m dobby.cli hwp text    "<공고문>"
```

Extract, verbatim and with the source location:

- **심사기준** — every criterion and its weight. A 100-point rubric with five
  rows is five different documents to write, weighted.
- **제외사유 / 자격요건** — who is eligible, and what gets an entry thrown out.
- **제출물** — every required file, its format, and its page or character limit.
- **마감** — the date, the time, and the submission channel.

Quote these into the ledger. Paraphrasing a rubric is how a criterion quietly
disappears.

## 2. Check the idea against prior art BEFORE writing

```
python -m dobby.cli research run "<the idea, in its own words>" --yes
```

Read `prior_art_verdict` exactly as written:

- `PRIOR ART CLAIMED` — open every URL. If the idea is already implemented at
  national scale, the entry is disqualified and the honest move is to change the
  idea now, not to write around it.
- `NOTHING RETRIEVED` — **these queries surfaced nothing. It does not mean
  nothing exists.** Vary the wording and search the primary registries directly
  (법령정보센터, the ministry's own 보도자료, KOSHA/KISTA and equivalents) before
  concluding anything.
- `INCOMPLETE` — a call failed or a provider refused. Coverage is unknown, so
  neither presence nor absence is established. Re-run before relying on it.

Every returned source is a CLAIM of a source, unresolved. Open it.

## 3. Generate options before committing to one

```
python -m dobby.cli panel "<the problem the contest is about>" \
    --protocol ngt --size 3
```

`ngt` has each provider answer alone before seeing the others, so three
independent framings arrive instead of one framing agreed to three times. Use
`--protocol dialectic` when two approaches are already on the table.

Your own second pass is not a second opinion — it is correlated with your first.

## 4. Map every section to a criterion, then write

Build the map before drafting: **criterion → weight → which section answers it →
what evidence that section carries**. A section that answers no criterion is
length the judge is not paid to read; a criterion with no section is points
conceded.

Write the numbers in only if a source supports them. A proposal is the one
document where an unsupported figure is both the most tempting and the easiest
for a domain judge to catch.

## 5. Fill the form, and verify it is filled

Korean submission forms are tables. The content IS the cells.

```
python -m dobby.cli hwp tables  "<제안서>"                     # what is empty
python -m dobby.cli hwp replace "<제안서>" --text "<기존>" \
       --with "<새 내용>" --out "<제안서_v2>"
```

`--out` is required and the source is never modified — the original form is
usually the only clean copy.

Two limits worth knowing before you promise a client anything: a `.hwp` (legacy
binary) can be READ but not written; save it as HWPX in 한글 first. And a
replacement crossing two runs is refused with the runs listed — that means split
the edit, not that the text is missing.

Then check the OUTPUT, not the fact that the command exited 0:

```
python -m dobby.cli hwp tables "<제안서_v2>"
```

Every required cell non-empty, every limit respected, every criterion covered.

## 6. Report against the rubric, not against effort

State: each criterion, the section that answers it, and the evidence. Then state
what is NOT covered and what could not be verified — including whether the prior
art search was `NOTHING RETRIEVED` rather than clean, because that distinction is
the difference between a checked entry and an unchecked one.
