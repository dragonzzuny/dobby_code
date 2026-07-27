"""The known-gaps list must describe the code as it is, not as it was.

`docs/RESEARCH_EVIDENCE_MATRIX.md` §10 opens with "Recorded so they are not
mistaken for oversights". It was found carrying two entries — a model-judge
adapter and a driver that runs `search.search` against real providers — as
unimplemented, months after both were built and exercised against live providers.
`dobby/judge.py` was 175 lines and `dobby/search_driver.py` 278 at the moment the
document said neither existed.

A known-gaps section that lists closed items is worse than having none: its whole
purpose is to be the one place a reader trusts about what is missing, so a stale
entry spends someone's afternoon rebuilding what is already there — or, worse,
makes them distrust the rest of the list, which is accurate.

Prose cannot be tested in general. Two specific, load-bearing claims can be, and
they are the two most likely to rot next: which providers have never been run,
and which modules the document says do not exist.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATRIX = os.path.join(REPO, "docs", "RESEARCH_EVIDENCE_MATRIX.md")


def _matrix() -> str:
    with open(MATRIX, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _gaps_section(case: unittest.TestCase) -> str:
    """Only the OPEN-gap bullets of §10.

    The first version of this ran to the next `## ` heading and so swallowed the
    "Closed since 0.1.0" paragraph and the `### Closed on 2026-07-26` narrative
    that follows it. That narrative says, correctly and in the past tense, that
    the search driver "never executed a model call" before that date — and the
    check read it as a live claim and failed on `codex`.

    A checker whose scope is wrong reports defects in the thing it is checking.
    The boundary is therefore the first `###` subheading or the word "Closed",
    whichever comes first.
    """
    text = _matrix()
    start = text.find("## 10. Not implemented")
    case.assertNotEqual(start, -1, "the known-gaps section is gone")
    rest = text[start:]
    ends = [rest.find(marker, 4) for marker in ("\n## ", "\n### ", "\nClosed since")]
    ends = [e for e in ends if e != -1]
    return rest[:min(ends)] if ends else rest


class UnexecutedProvidersAreActuallyUnexecuted(unittest.TestCase):
    """`verified_on` is the record; the prose must agree with it in BOTH directions.

    One direction stops the document overclaiming — naming a provider as verified
    when nothing ever ran it. The other stops it underclaiming, which is how this
    section went stale: work gets done and the list is not revisited.
    """

    #: Named in the document as catalogued-but-never-run.
    CLAIMED_UNEXECUTED = ("qwen", "ollama", "kimi", "dashscope")

    def setUp(self):
        from dobby.providers import registry
        self.registry = registry()

    def test_each_provider_the_docs_call_unexecuted_has_no_verified_platform(self):
        for pid in self.CLAIMED_UNEXECUTED:
            with self.subTest(provider=pid):
                spec = self.registry.get(pid)
                self.assertEqual(
                    tuple(spec.verified_on), (),
                    f"the docs record {pid} as never executed, but its "
                    f"verified_on is {spec.verified_on!r}. One of the two is "
                    f"wrong, and a run that happened is the more useful fact.")

    def test_the_docs_still_name_them(self):
        """If a run fills `verified_on`, this fails and the prose gets updated."""
        text = _matrix()
        for pid in self.CLAIMED_UNEXECUTED:
            with self.subTest(provider=pid):
                self.assertIn(
                    pid, text,
                    f"{pid} is unexecuted but the evidence matrix no longer "
                    f"mentions it; an unverified provider must stay recorded")

    def test_a_provider_with_a_verified_platform_is_not_called_unexecuted(self):
        gaps = self._gaps_section()
        for pid in self.registry.ids():
            spec = self.registry.get(pid)
            if not spec.verified_on:
                continue
            with self.subTest(provider=pid):
                for line in gaps.splitlines():
                    if pid in line and re.search(r"(?i)never executed|unexecuted",
                                                 line):
                        self.fail(
                            f"{pid} has verified_on={spec.verified_on!r} but the "
                            f"known-gaps section says it was never executed: "
                            f"{line.strip()[:100]}")

    def _gaps_section(self) -> str:
        return _gaps_section(self)


class ClosedGapsAreNotStillListed(unittest.TestCase):
    """The exact regression: modules called missing while sitting on disk."""

    #: (module, the phrase the document used to deny it existed)
    ONCE_LISTED_AS_MISSING = (
        ("dobby/judge.py", "model-judge adapter"),
        ("dobby/search_driver.py", "search.search"),
    )

    def test_no_gap_entry_denies_a_module_that_exists(self):
        gaps = _gaps_section(self)
        for module, phrase in self.ONCE_LISTED_AS_MISSING:
            with self.subTest(module=module):
                exists = os.path.exists(os.path.join(REPO, module))
                if not exists:
                    continue
                bullets = [line for line in gaps.splitlines()
                           if line.lstrip().startswith("- ") and phrase in line]
                self.assertEqual(
                    bullets, [],
                    f"{module} exists, yet the known-gaps list still carries an "
                    f"entry about {phrase!r}: {bullets[:1]}")

    def test_the_correction_itself_is_recorded(self):
        """A silently fixed list teaches nothing about why it drifted."""
        self.assertIn("This list was stale", _matrix())


if __name__ == "__main__":
    unittest.main()
