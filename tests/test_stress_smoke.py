"""A short run of the concurrency stress harness, inside the suite.

`evals/stress/concurrency.py` is the real instrument: many rounds, deliberate
CPU load, every invariant checked. It takes minutes and belongs in a deliberate
run, not in every suite pass.

What belongs here is the smallest version that still exercises the paths, so
that the harness itself cannot rot unnoticed and so a regression in the obvious
cases is caught by the ordinary suite. The rare, timing-dependent defects are
NOT what this file finds -- three of them were found by the full harness under
load and none of them by a single pass:

    a stale worker writing READY over a live lease, both then running the node
    several survivors of a kill all closing the same interrupted attempt
    a worker attaching to a run another worker finished mid-attach

Saying which tool found what matters, because a green suite here is not
evidence of a concurrency-clean build. The evidence is the harness output, and
the standing number as of 2026-08-30 is 162 rounds with no violation across
contend / diamond / kill / effect, at up to six worker processes, eight busy
cores, and three-way in-process parallelism.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "evals", "stress"))

import concurrency as S  # noqa: E402


class TheHarnessItself(unittest.TestCase):
    """It has to be able to fail, or a zero from it means nothing."""

    def test_every_invariant_is_reachable_from_the_checker(self):
        with open(S.__file__, encoding="utf-8") as fh:
            source = fh.read()
        for name in ("no_crash", "terminal_agreement", "single_promotion",
                     "no_stolen_lease", "no_concurrent_attempt", "replayable",
                     "effect_at_most_once"):
            self.assertIn(f'"{name}"', source, name)

    def test_a_bookkeeping_reason_is_what_marks_a_stolen_lease(self):
        """`LEASED -> READY` is legal for RECOVERY and not for bookkeeping, so
        the check is on the reason and not on the transition."""
        self.assertIn("dependencies satisfied", S.BOOKKEEPING_REASONS)

    def test_the_exit_code_is_the_verdict(self):
        with open(S.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("return 1 if failures else 0", source)


class OneRoundOfEach(unittest.TestCase):
    """One round per scenario, no added load. Fast, and it does run the code."""

    def round(self, scenario):
        shape = {"contend": "chain", "diamond": "diamond", "kill": "chain",
                 "effect": "effect"}[scenario]
        rnd = S.Round(shape=shape, workers=2,
                      nodes=4 if scenario == "diamond" else 3,
                      hold=0.05, die_on="n0" if scenario == "kill" else "",
                      parallel=1)
        self.addCleanup(rnd.close)
        rnd.seed()
        rnd.race(killer_first=(scenario == "kill"))
        if scenario == "kill":
            rnd.die_on = ""
            rnd.race()
        return rnd

    def test_contend(self):
        self.assertEqual(self.round("contend").violations(), [])

    def test_diamond(self):
        self.assertEqual(self.round("diamond").violations(), [])

    def test_kill(self):
        self.assertEqual(self.round("kill").violations(), [])

    def test_effect_happens_at_most_once(self):
        rnd = self.round("effect")
        self.assertEqual(rnd.violations(), [])
        self.assertLessEqual(rnd.ran("effect"), 1)


if __name__ == "__main__":
    unittest.main()
