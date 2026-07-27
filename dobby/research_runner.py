"""Actually run the search plan, instead of only producing one.

`research.plan_queries` decomposes an information need into query shapes and stops
there. That is a planner nobody asked for twice: it emits an artifact that looks
like research and contains no findings, which is the same failure this repository
keeps naming — existence is not a measurement, and a plan is not a search.

WHAT THIS CAN AND CANNOT ESTABLISH

The engine has no network. Web access comes from a PROVIDER that has it, so this
dispatches each query shape to one and collects what comes back. That buys real
retrieval and it buys a new hazard: a model can answer a search question from
memory and produce citations that look exactly like retrieved ones.

So nothing here is reported as verified. Every reference is returned as a CLAIM of
a source, and `research.verify_citations` against an empty corpus reports
`NOT CHECKED` rather than "clean" — that distinction already exists in this
repository and this module leans on it rather than inventing a softer word.

**An empty result is never "no prior art exists."** For the contest this was built
for, "이미 국가 차원에서 시행되고 있거나 기본 구상이 매우 유사한 경우" is a
disqualification, so a false "nothing found" is the most expensive possible output.
A shape that returns nothing is reported as `no_results`, with the query that
produced it, so the reader can tell a searched-and-empty from a not-searched.

COST

One provider call per query shape — five by default, six with a year hint. Opt-in
via `--yes`, the same stance `fleet --probe` takes about the only other paid path.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Sequence

from .research import QueryPlan, plan_queries, verify_citations

#: A shape's answer must carry sources, not a summary. The instruction is explicit
#: about wanting URLs because a reference without a locator cannot be resolved by
#: anyone, which turns the whole result into an unfalsifiable assertion.
SEARCH_PROMPT = """Search the web for this, then report what you actually found.

QUERY: {query}
WHY THIS SHAPE: {rationale}

Report, in this order:

FOUND: one line per source, as `- <title> | <publisher or site> | <URL>`
  Only sources you actually retrieved. If you did not retrieve any, write
  `FOUND: none`.
NOT FOUND: what you looked for and could not locate.
ASSESSMENT: two sentences at most, on what the sources say about the query.

Rules:
- Do not list a source you did not open. A plausible-looking citation is worse
  than none, because it cannot be told apart from a real one.
- Do not answer from memory. If you cannot search, say so on the first line.
"""

#: The FOUND header, but not the NOT FOUND one that contains it as a substring.
_FOUND_RE = re.compile(r"(?<!NOT )(?<!NOT_)FOUND:")

#: `- title | publisher | url` lines from the FOUND block.
_SOURCE_RE = re.compile(r"^\s*[-*]\s*(?P<body>.+?)\s*$", re.M)

#: Providers answer in Markdown whether or not the prompt asks for it. Measured on
#: a real call: the reply used `**FOUND:**` and `**NOT FOUND:**`, and the bullet
#: pattern above then captured two entries whose whole body was `*` — emphasis
#: markers counted as sources, inflating `sources_claimed` from 6 to 8. A count
#: that includes punctuation is worse than a smaller honest one, because
#: `sources_claimed` is what a reader uses to judge coverage.
_EMPHASIS = re.compile(r"^[\s*_#>`]+|[\s*_`]+$")

#: A real source names something. At least two word characters — Latin, Hangul or
#: CJK — must survive, or the line was formatting.
_HAS_CONTENT = re.compile(r"[\w㄰-㆏가-힣一-鿿]{2}")
_URL_RE = re.compile(r"https?://[^\s|)>\]]+")

#: A provider saying it cannot search. Detected so the run reports "not searched"
#: rather than folding it into "found nothing".
_CANNOT_SEARCH = re.compile(
    r"(?i)\b(?:cannot|can't|unable to|no ability to|do not have)\b[^.\n]{0,40}"
    r"\b(?:search|browse|access the (?:web|internet))\b")


class ResearchError(RuntimeError):
    """A precondition this module refuses to work around."""


def web_provider(provider_id: str | None = None) -> str:
    """A provider that can actually reach the web, or a refusal.

    Checked against the catalog's declared `web` capability rather than assumed. A
    provider without it would answer from memory, and the output would be
    indistinguishable from a search — which is the one failure mode this module
    exists to avoid.
    """
    from .providers import registry, report

    reg = registry()
    if provider_id:
        spec = reg.get(provider_id)
        if "web" not in spec.capabilities:
            raise ResearchError(
                f"{provider_id} does not declare a `web` capability, so it would "
                f"answer from memory and the result would look exactly like a "
                f"search. Pick one that does, or add the capability to the "
                f"catalog once it has been established.")
        return provider_id

    usable = {name for name, info in report()["providers"].items()
              if info.get("usable")}
    for name in reg.ids():
        if name in usable and "web" in reg.get(name).capabilities:
            return name
    raise ResearchError(
        "no usable provider declares a `web` capability. `dobby fleet` lists what "
        "is here; searching needs one of them to have web access.")


def parse_answer(text: str) -> dict:
    """Split one shape's answer into sources, gaps, and whether it searched."""
    if not text or not text.strip():
        return {"searched": False, "sources": [], "not_found": "",
                "assessment": "", "refusal": "empty reply"}
    if _CANNOT_SEARCH.search(text):
        return {"searched": False, "sources": [], "not_found": "",
                "assessment": "", "refusal": text.strip()[:200]}

    upper = text.upper()
    # `NOT FOUND:` contains `FOUND:`, so a plain substring search locates the
    # wrong block and reads the gap list as the source list. The lookbehind is
    # the whole reason this is a regex.
    found_at = _FOUND_RE.search(upper)
    if found_at is None:
        # An answer with no FOUND block did not report a search result at all. It
        # must NOT be folded into "searched and found nothing": that is the
        # difference between an empty search and an unreadable reply, and merging
        # them is how a false "no prior art exists" gets manufactured.
        return {"searched": False, "sources": [], "not_found": "",
                "assessment": text.strip()[:600],
                "refusal": ("reply carried no FOUND block, so whether a search "
                            "happened cannot be told from it")}

    start = found_at.end()
    end = len(text)
    for marker in ("NOT FOUND:", "ASSESSMENT:"):
        if marker in upper[start:]:
            end = min(end, start + upper[start:].index(marker))
    found_block = text[start:end]

    sources = []
    if "NONE" not in found_block.strip().upper()[:8]:
        for match in _SOURCE_RE.finditer(found_block):
            body = _EMPHASIS.sub("", match.group("body")).strip()
            if not _HAS_CONTENT.search(body):
                continue
            url = _URL_RE.search(body)
            sources.append({
                "raw": body[:300],
                "url": url.group(0) if url else None,
                # Stated, not inferred: nothing here resolved this.
                "status": "CLAIMED, not resolved",
            })

    def section(name: str) -> str:
        if name not in upper:
            return ""
        start = upper.index(name) + len(name)
        rest = text[start:]
        for marker in ("NOT FOUND:", "ASSESSMENT:"):
            if marker in rest.upper() and marker != name:
                rest = rest[:rest.upper().index(marker)]
        # `**NOT FOUND:**` leaves its closing `**` at the head of the section.
        return _EMPHASIS.sub("", rest).strip()[:600]

    return {"searched": True, "sources": sources,
            "not_found": section("NOT FOUND:"),
            "assessment": section("ASSESSMENT:"), "refusal": None}


def run_plan(plan: QueryPlan, *, provider_id: str | None = None,
             timeout_s: int = 300, cwd: str | None = None,
             on_shape: Callable[[dict], None] | None = None) -> dict:
    """Dispatch every query in `plan` and collect what came back.

    Never raises on a provider failure: a failed call is recorded as such, because
    an error is not evidence of absence and must not be counted as one.
    """
    from .providers import run_by_id

    chosen = web_provider(provider_id)
    results = []
    for query in plan.queries:
        started = time.monotonic()
        prompt = SEARCH_PROMPT.format(query=query["query"],
                                      rationale=query["rationale"])
        outcome = run_by_id(chosen, prompt, timeout_s=timeout_s, cwd=cwd)
        record = {"shape": query["shape"], "query": query["query"],
                  "provider": chosen,
                  "duration_s": round(time.monotonic() - started, 2),
                  "ok": bool(outcome.ok)}
        if not outcome.ok:
            record.update(error=outcome.error, searched=False, sources=[])
        else:
            record.update(parse_answer(outcome.text))
        results.append(record)
        if on_shape:
            on_shape(record)
    return summarize(plan, results)


def summarize(plan: QueryPlan, results: Sequence[dict]) -> dict:
    """The report, with absence and failure kept apart."""
    searched = [r for r in results if r.get("searched")]
    refused = [r for r in results if r.get("ok") and not r.get("searched")]
    failed = [r for r in results if not r.get("ok")]
    all_sources = [s for r in searched for s in r.get("sources", [])]
    empty = [r["shape"] for r in searched if not r.get("sources")]

    # `verify_citations` short-circuits on an empty corpus and returns
    # `checked: 0` no matter how many references were handed to it — which, next
    # to `sources_claimed: 12`, reads as "there was nothing to check" when the
    # truth is "twelve are waiting and nothing here can resolve them". So its
    # NOT CHECKED verdict is kept for the standard of proof, and the count that
    # matters is stated separately.
    citation_report = dict(verify_citations(
        [s["raw"] for s in all_sources], corpus=[]))
    citation_report["awaiting_resolution"] = len(all_sources)
    citation_report["with_url"] = sum(1 for s in all_sources if s["url"])
    citation_report["without_url"] = sum(1 for s in all_sources if not s["url"])

    return {
        "need": plan.need,
        "shapes_planned": len(plan.queries),
        "shapes_searched": len(searched),
        "shapes_refused": [{"shape": r["shape"], "why": r.get("refusal")}
                           for r in refused],
        "shapes_failed": [{"shape": r["shape"], "error": (r.get("error") or "")[:200]}
                          for r in failed],
        "sources_claimed": len(all_sources),
        "shapes_with_no_results": empty,
        "results": results,
        "citations": citation_report,
        "prior_art_verdict": _verdict(len(all_sources), searched, refused, failed),
        "interpretation": (
            "Every source above is a CLAIM of a source. Nothing here resolved one: "
            "the checker ran against an empty corpus and reports NOT CHECKED, not "
            "clean. A model can answer a search question from memory and produce "
            "citations indistinguishable from retrieved ones, so open the URLs "
            "before relying on any of it."),
    }


def _verdict(source_count: int, searched, refused, failed) -> dict:
    """What the run does and does not license saying about prior art.

    The expensive error is a false "nothing exists": for the disqualification rule
    this was built against — an idea already implemented nationally is rejected —
    an empty search reported as absence is worse than no search at all.
    """
    if failed or refused:
        return {"claim": "INCOMPLETE",
                "why": (f"{len(failed)} call(s) failed and {len(refused)} "
                        f"provider refusal(s); coverage is unknown, so neither "
                        f"presence nor absence of prior art is established")}
    if source_count == 0:
        return {"claim": "NOTHING RETRIEVED",
                "why": ("every shape searched and returned no sources. That is "
                        "not evidence that none exist - it is evidence these "
                        "queries did not surface any. Vary the wording, search "
                        "the primary registries directly, then decide")}
    return {"claim": "PRIOR ART CLAIMED",
            "why": (f"{source_count} claimed source(s) across "
                    f"{len(searched)} shape(s). Open them before treating any as "
                    f"established; none were resolved here")}


def research(need: str, *, year_hint: str | None = None, **kwargs) -> dict:
    """Plan and then actually run it — the whole point of this module."""
    return run_plan(plan_queries(need, year_hint=year_hint), **kwargs)
