---
name: paper-draft
description: Draft or revise an academic paper — Korean or English — with the claims priced, the citations resolved, the rigor gates run, and the generated-prose signature removed before submission. Use for 논문 작성, 초록, 심사 대응, or any manuscript going to a journal or conference.
---

# paper-draft

Three things sink a manuscript that a careful author can catch first: a claim
stronger than its evidence, a citation that does not resolve, and prose that
reads as machine-generated. Each has a command here.

## 1. Price every claim before writing the section that carries it

```
python -m dobby.cli research claims --file <draft.md>
```

Claim strength sets the evidence bar: "may help" needs an example, "improves 40%"
needs the measurement, "always" needs the proof. A claim whose bar you cannot
meet gets weakened now — weakening it in review costs a round trip and the
reviewer's trust.

## 2. Resolve the citations, and know when you have not

```
python -m dobby.cli research citations <refs.txt> --corpus <retrieved.json>
```

Three severities, because three different things go wrong: `exact`,
`metadata_mismatch` (the work exists, a field is wrong — correctable, NOT
fabrication), and `unresolvable` (nothing matches; any claim resting on it
currently has no support).

With an empty corpus this reports **`NOT CHECKED`, never clean**. Fabricated
references are stylistically perfect, so the only detection is resolution against
independently retrieved records. Retrieve them:

```
python -m dobby.cli research run "<the literature question>" --yes
```

`NOTHING RETRIEVED` means these queries surfaced nothing — not that the
literature is empty. See `.claude/skills/prior-art-search/SKILL.md`.

## 3. Run the rigor gates on the experiment, not on the prose

```
python -m dobby.cli ml --file <experiment.json>
```

Leakage, reproducibility, and interpretation gates. A result that fails a leakage
check is not a writing problem and no amount of rewriting fixes it — this is why
it runs before the drafting, not after.

## 4. Draft — then remove the signature

```
python -m dobby.cli style --file <draft.md>
python -m dobby.cli style --file <draft.md> --rewritten <revised.md>
```

`style` detects the generated-prose signature in **Korean and English**: measured
on this machine, `본 연구는 … 이를 통해 다양한 시사점을 도출하고자 한다` fires
`phrase:를 통해` at S3. Uniform sentence length, comma density, hedge stacking,
and bullet-heavy structure are the other signals.

`--rewritten` scores a candidate revision against a change budget, so the fix
does not become a rewrite of a paper that was already yours.

The rule underneath: lead with the concrete thing — the measurement, the figure,
the counterexample — and explain after it. Adjectives are what you write when the
number is missing.

## 5. Korean submission formats are tables

Most Korean journals and conferences supply a 서식 as `.hwp` or `.hwpx`.

```
python -m dobby.cli hwp tables "<서식>"                      # what must be filled
python -m dobby.cli hwp replace "<서식>" --text "<기존>" \
       --with "<내용>" --out "<제출본>"
python -m dobby.cli hwp tables "<제출본>"                    # verify the OUTPUT
```

`--out` is required — the blank 서식 is usually the only clean copy.

Legacy `.hwp` is not read-only. `text`, `paragraphs`, `tables` and `find` read it
directly; `pages`, `shapes`, `export` and `edit` drive 한글 itself over COM, which
writes the binary format the library cannot. Two preconditions, both measured:

- **한글 must not be running.** COM attaches to the live instance, so `Open` adds
  a document to the session the user is looking at and the edits land in whichever
  one is active. Symptoms are baffling — a document that reads back as two
  characters, replacements that appear in the wrong file. Check first, refuse
  rather than proceed.
- **The security module must be registered**, or 한글 refuses file access from
  automation. `python -c "from dobby import hwpcom, json; print(hwpcom.available())"`
  names what is missing.

Table cells are separate text lists, not part of the body. `--list 0` is the body;
`hwp shapes --list <n>` walks the rest, which is how you find the cell to write
into and how you confirm a caption is a real table rather than shrunken body text.

## 6. 심사 대응 — the answer letter is its own artifact, with its own failures

A revision round is almost entirely *addition*, which is where its defects come
from. Four things, in this order.

**Classify each reviewer item before drafting.** Two properties decide the
answer. First, does this field want the reviewer's words transcribed or your
summary? Response forms usually reproduce the 심사의견 verbatim beside the reply,
and summarising there reads as editing the reviewer. Second, which way does the
request run — strengthen, soften, correct, or confirm? A reviewer asking that a
finding be given *more* weight, answered with its limitations, has been refused
without being told so. Write the direction down before writing the reply.

**Answer the item, not around it.** An aside that says a point is *not* a problem
wants acknowledgement, not a defence. Volunteered scope is what turns a minor
revision into a new round.

**Check every claim in the letter against the shipped file.** "We added X to 5.2"
is a claim about the submission, not about the markdown you edited — and a
generator that rewrites the body from the chapter-1 heading onward leaves the
abstract and front matter untouched, silently.

```
python -m dobby.cli hwp text "<제출본>"     # then grep the letter's claims against this
python -m dobby.cli hwp pages "<제출본>"    # length limits are enforced by page, and cost money
```

**Re-derive any number the reviewer quotes.** If a fresh run disagrees with the
reported value, the fresh run wins, the manuscript is corrected, and the letter
says so in one clause — a reviewer who quoted `RMSEA(0.092)` will notice `0.091`
arriving unannounced. Reported statistics should each have had a reproduction
command *before* review; deriving one under review is how a stale digit survives
to print.

## 7. Report what is not established

State the claims that remain unsupported, the citations still `unresolvable` or
`NOT CHECKED`, and the gates that did not run. A limitations section written from
that list is both honest and the one reviewers read for whether you know your own
work's boundary.
