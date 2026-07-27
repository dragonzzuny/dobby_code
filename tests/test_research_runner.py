"""The search runner: does it search, and does it lie about what it found?

Two classes of assertion here, and the second matters more.

The first is mechanical — the FOUND block is parsed, URLs are extracted, a
non-web provider is refused.

The second is about what the report LICENSES. This module's whole reason to exist
is that a plan is not a search; its whole reason to be dangerous is that a model
can answer a search question from memory and emit citations indistinguishable
from retrieved ones. So the tests pin the distinctions that keep an empty search
from being reported as absence:

    searched-and-empty   != unreadable-reply  != call-failed  != provider-refused

Every one of those produces zero sources. Collapsing them is how a false "no
prior art exists" gets manufactured, and for the disqualification rule this was
built against — an idea already in force nationally is rejected — that is the
most expensive output the system can produce.

NO TEST HERE SPENDS MONEY. `dobby.providers.run_by_id` is refused outright in
setUp, because an earlier module in this repository patched too late and made
three real paid calls before anyone noticed.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dobby.research import plan_queries  # noqa: E402
from dobby.research_runner import (ResearchError, parse_answer, run_plan,  # noqa: E402
                                   summarize, web_provider)


class _NoPaidCalls(unittest.TestCase):
    """Bind the refusal before any test body runs, not inside one."""

    def setUp(self):
        def _refuse(*args, **kwargs):
            raise AssertionError(
                "a test reached the real provider layer; the mock did not take "
                "effect where it was expected")

        patcher = mock.patch("dobby.providers.run_by_id", side_effect=_refuse)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.plan = plan_queries("전기차 화재 감지 산업단지 안전 규제",
                                 year_hint="2026")


class ParsesWhatAProviderActuallyReturns(_NoPaidCalls):

    def test_sources_and_urls_come_out_of_the_found_block(self):
        got = parse_answer(
            "FOUND:\n"
            "- KOSHA Guide | 안전보건공단 | https://kosha.or.kr/a\n"
            "- 산업안전보건법 시행령 | 국가법령정보센터 | https://law.go.kr/b\n"
            "NOT FOUND: the 2026 amendment text.\n"
            "ASSESSMENT: partly in force already.")
        self.assertTrue(got["searched"])
        self.assertEqual([s["url"] for s in got["sources"]],
                         ["https://kosha.or.kr/a", "https://law.go.kr/b"])
        self.assertIn("2026", got["not_found"])
        self.assertIn("force", got["assessment"])

    def test_a_gap_list_is_never_read_as_a_source_list(self):
        """`NOT FOUND:` contains `FOUND:`.

        A substring search locates the header inside `NOT FOUND:` and then parses
        the things the provider could NOT find as the things it found — the worst
        possible direction for this error, since it turns absence into evidence.
        """
        got = parse_answer("NOT FOUND:\n"
                           "- 2026 개정안 | (none) | n/a\n"
                           "ASSESSMENT: nothing located.")
        self.assertEqual(got["sources"], [])
        self.assertFalse(got["searched"])

    def test_explicit_none_is_a_search_that_returned_nothing(self):
        got = parse_answer("FOUND: none\nNOT FOUND: all of it\n"
                           "ASSESSMENT: nothing.")
        self.assertTrue(got["searched"], "the provider did report a result")
        self.assertEqual(got["sources"], [])

    def test_a_reply_with_no_found_block_is_not_a_search(self):
        """The distinction that keeps 'unreadable' out of 'nothing exists'."""
        got = parse_answer("Here is a summary of what I know about the topic.")
        self.assertFalse(got["searched"])
        self.assertIn("FOUND", got["refusal"])

    def test_a_provider_saying_it_cannot_browse_is_recorded_as_such(self):
        for text in ("I cannot browse the web, so this is from memory.",
                     "I'm unable to search the internet.",
                     "I do not have access to the web."):
            got = parse_answer(text)
            self.assertFalse(got["searched"], text)
            self.assertTrue(got["refusal"])

    def test_an_empty_reply_is_a_refusal_not_an_empty_result(self):
        self.assertFalse(parse_answer("")["searched"])
        self.assertFalse(parse_answer("   \n ")["searched"])

    def test_markdown_emphasis_is_not_counted_as_a_source(self):
        """From a real reply, not an invented one.

        A live call to `claude` returned `**FOUND:**` / `**NOT FOUND:**` headers
        and bullet lines whose entire body was `*`. The bullet pattern captured
        those, and `sources_claimed` read 8 when six real sources came back. That
        number is what a reader uses to judge coverage, so inflating it with
        punctuation is worse than reporting fewer.
        """
        got = parse_answer(
            "**FOUND:**\n"
            "*   *\n"
            "- 소방청 지하주차장 전기차 화재안전 대책 | 소방청 | https://nfa.go.kr/x\n"
            "*\n"
            "- **KFS-1130** | 한국화재보험협회 | https://kfpa.or.kr/y\n"
            "**NOT FOUND:**\n"
            "- 산업단지 특화 규정\n"
            "**ASSESSMENT:** 관련 규정은 건축·소방법에 있음.")
        self.assertEqual(len(got["sources"]), 2,
                         f"emphasis counted as a source: {got['sources']}")
        self.assertEqual([s["url"] for s in got["sources"]],
                         ["https://nfa.go.kr/x", "https://kfpa.or.kr/y"])
        self.assertFalse(got["not_found"].startswith("*"),
                         "the section kept its closing emphasis marker")
        self.assertTrue(got["assessment"].startswith("관련"),
                        f"assessment kept markdown: {got['assessment']!r}")

    def test_a_source_without_a_url_is_kept_but_marked_unresolved(self):
        got = parse_answer("FOUND:\n- Some report | Some ministry\n")
        self.assertEqual(len(got["sources"]), 1)
        self.assertIsNone(got["sources"][0]["url"])
        self.assertIn("not resolved", got["sources"][0]["status"])


class RefusesAProviderThatCannotSearch(_NoPaidCalls):

    def test_a_provider_without_web_capability_is_refused_with_a_reason(self):
        with self.assertRaises(ResearchError) as caught:
            web_provider("codex")
        message = str(caught.exception)
        self.assertIn("web", message)
        self.assertIn("memory", message,
                      "the refusal must say WHY, not just that it declined")

    def test_a_web_capable_provider_is_accepted(self):
        self.assertEqual(web_provider("claude"), "claude")

    def test_run_plan_refuses_before_making_any_call(self):
        with self.assertRaises(ResearchError):
            run_plan(self.plan, provider_id="codex")


class KeepsAbsenceApartFromFailure(_NoPaidCalls):
    """Four ways to get zero sources; four different verdicts."""

    def _verdict(self, results):
        return summarize(self.plan, results)["prior_art_verdict"]["claim"]

    def test_searched_and_empty_is_not_absence(self):
        out = summarize(self.plan, [
            {"shape": "mechanism", "ok": True, "searched": True, "sources": []}])
        self.assertEqual(out["prior_art_verdict"]["claim"], "NOTHING RETRIEVED")
        why = out["prior_art_verdict"]["why"]
        self.assertIn("not evidence that none exist", why)
        self.assertEqual(out["shapes_with_no_results"], ["mechanism"])

    def test_a_failed_call_is_incomplete_not_empty(self):
        self.assertEqual(self._verdict([
            {"shape": "mechanism", "ok": False, "error": "timeout after 300s",
             "searched": False, "sources": []}]), "INCOMPLETE")

    def test_a_provider_refusal_is_incomplete_not_empty(self):
        self.assertEqual(self._verdict([
            {"shape": "mechanism", "ok": True, "searched": False, "sources": [],
             "refusal": "cannot browse"}]), "INCOMPLETE")

    def test_found_sources_are_claimed_never_established(self):
        out = summarize(self.plan, [
            {"shape": "mechanism", "ok": True, "searched": True,
             "sources": [{"raw": "A | B | https://x", "url": "https://x",
                          "status": "CLAIMED, not resolved"}]}])
        self.assertEqual(out["prior_art_verdict"]["claim"], "PRIOR ART CLAIMED")
        self.assertIn("none were resolved", out["prior_art_verdict"]["why"])
        self.assertIn("CLAIM", out["interpretation"])

    def test_the_citation_count_does_not_read_as_nothing_to_check(self):
        """`verify_citations` returns `checked: 0` on an empty corpus always.

        Printed next to `sources_claimed: 2` that reads as "there was nothing to
        check", when the truth is "two are waiting and nothing here can resolve
        them". The waiting count has to be stated separately or the report
        understates its own uncertainty.
        """
        out = summarize(self.plan, [
            {"shape": "mechanism", "ok": True, "searched": True, "sources": [
                {"raw": "A | B | https://x", "url": "https://x", "status": "c"},
                {"raw": "C | D", "url": None, "status": "c"}]}])
        cit = out["citations"]
        self.assertEqual(cit["checked"], 0)
        self.assertIn("NOT CHECKED", cit["verdict"])
        self.assertEqual(cit["awaiting_resolution"], 2)
        self.assertEqual(cit["with_url"], 1)
        self.assertEqual(cit["without_url"], 1)


class DrivesEveryQueryShape(_NoPaidCalls):

    def test_one_call_per_shape_and_a_failure_does_not_abort_the_rest(self):
        calls = []

        class _Result:
            def __init__(self, ok, text="", error=""):
                self.ok, self.text, self.error = ok, text, error

        def fake(pid, prompt, **kwargs):
            calls.append((pid, prompt))
            if len(calls) == 2:
                return _Result(False, error="boom")
            return _Result(True, "FOUND:\n- T | P | https://e/%d\n" % len(calls))

        with mock.patch("dobby.providers.run_by_id", side_effect=fake):
            out = run_plan(self.plan, provider_id="claude")

        self.assertEqual(len(calls), len(self.plan.queries))
        self.assertTrue(all(pid == "claude" for pid, _ in calls))
        self.assertEqual(len(out["shapes_failed"]), 1,
                         "one call failed and the run continued")
        self.assertEqual(out["shapes_searched"], len(self.plan.queries) - 1)
        self.assertEqual(out["prior_art_verdict"]["claim"], "INCOMPLETE")

    def test_the_query_text_reaches_the_provider_intact(self):
        """A Korean query must not be mangled on the way to the call."""
        seen = []

        class _Result:
            ok, text, error = True, "FOUND: none\n", ""

        def fake(pid, prompt, **kwargs):
            seen.append(prompt)
            return _Result()

        with mock.patch("dobby.providers.run_by_id", side_effect=fake):
            run_plan(self.plan, provider_id="claude")

        for query in self.plan.queries:
            self.assertTrue(any(query["query"] in prompt for prompt in seen),
                            f"query never reached a provider: {query['query']!r}")

    def test_every_shape_is_reported_even_when_all_fail(self):
        class _Result:
            ok, text, error = False, "", "no such provider binary"

        with mock.patch("dobby.providers.run_by_id",
                        side_effect=lambda *a, **k: _Result()):
            out = run_plan(self.plan, provider_id="claude")
        self.assertEqual(len(out["shapes_failed"]), len(self.plan.queries))
        self.assertEqual(out["shapes_searched"], 0)
        self.assertNotEqual(out["prior_art_verdict"]["claim"],
                            "NOTHING RETRIEVED",
                            "total failure must never read as an empty search")


if __name__ == "__main__":
    unittest.main()
