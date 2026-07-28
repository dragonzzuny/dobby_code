---
name: prior-art-search
description: Search patents, regulations, and published work for prior art, and keep a reproducible log of what was searched. Use for 특허 조사, 선행기술, regulation checks before a proposal, or any question of the form "has this been done already". Distinguishes a searched-and-empty result from an unsearched one, because reporting the second as the first is the expensive error.
---

# prior-art-search

The failure mode this exists to prevent is a single sentence: **"nothing came up,
so it doesn't exist."** For a patent filing that is a rejected application; for a
public competition it is a disqualified entry; for a paper it is a reviewer
finding the thing you missed.

*The source-selection and reproducible-log framing here is adapted from the
`scientific-db-uspto-database` skill in [ECC](https://github.com/affaan-m/ECC)
(MIT). The verdict handling and the refusal to treat an empty result as absence
are dobby's own.*

## 1. Official registries first, aggregators second

Search the authoritative source before the convenient one, and record which you
used. An aggregator's coverage gap is invisible from inside the aggregator.

| domain | official first | secondary |
|---|---|---|
| 한국 특허 | KIPRIS (kipris.or.kr) | Google Patents, Lens.org |
| US patents | USPTO Patent Public Search, ODP | Google Patents, Lens.org |
| 한국 법령 | 국가법령정보센터 (law.go.kr) | ministry 보도자료 |
| 산업안전 | KOSHA, 고용노동부 | 뉴스, 학회지 |
| papers | the publisher / DOI | Semantic Scholar, Scholar |

A secondary source disagreeing with the official one is a finding, not noise.

## 2. Run the search — do not stop at a plan

```
python -m dobby.cli research plan "<need>"          # decomposes, searches nothing
python -m dobby.cli research run  "<need>" --yes    # actually searches
```

`run` makes one provider call per query shape and costs money, hence `--yes`.
Only providers declaring a `web` capability are accepted; anything else would
answer from memory and produce output indistinguishable from retrieval.

The plan's shapes are deliberate. The **refutation** and **limitation** queries
are the ones that find the thing that kills the idea; the canonical query only
confirms the premise. Stop when refutation and limitation stop returning
anything NEW — not when the canonical query looks satisfying.

## 3. Read the verdict literally

- `PRIOR ART CLAIMED` — sources came back. Every one is a CLAIM, unresolved.
  **Open the URLs.** A model can produce citations that look exactly like
  retrieved ones.
- `NOTHING RETRIEVED` — every shape searched and returned nothing. This is
  evidence about the QUERIES, not about the world. Vary the wording, search the
  official registry by hand, then decide.
- `INCOMPLETE` — a call failed or a provider refused. Coverage is unknown.

Never compress these three into "no prior art found".

## 4. Verify the citations you intend to rely on

```
python -m dobby.cli research citations <refs.txt> --corpus <retrieved.json>
```

With an empty corpus this reports `NOT CHECKED`, not a clean bill of health —
"nothing to check against" and "everything checks out" are opposite situations.
The `awaiting_resolution` count says how many are still unverified.

## 5. Log it so someone else can repeat it

For each query: the exact string, the source searched, the date, the result count,
and what was opened. A prior-art claim that cannot be re-run is an opinion.

Record the negative results too. "Searched KIPRIS for X on 2026-07-28, 0 hits" is
a finding; silence is not.

## 6. Report

State what was searched, what was found, what was opened and confirmed, and —
first — what was NOT searched. The boundary of the search is the boundary of the
claim.
