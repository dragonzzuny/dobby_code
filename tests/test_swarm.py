import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.swarm import (COLLAPSE_MPD, PROTOCOLS, Evidence, Idea, analyze,
                         assess, build_prompts, coupling_ratio, effective_n,
                         entropy_of_votes, explore_cycle, gate, get,
                         has_prior_art, mean_pairwise_distance, recommend)
from dobby.swarm.grounding import (NO_EVIDENCE, NO_TEST, OVERCLAIM, RESTATEMENT,
                                   UNKNOWN_EVIDENCE, VAGUE)

IDENTICAL = ["the router assigns an agency level from policy severity"] * 4
DISTINCT = [
    "the router assigns an agency level from policy severity",
    "compression must preserve every file path and numeric threshold",
    "worktree isolation prevents two writing agents corrupting one tree",
    "citation resolution needs an independently retrieved corpus",
]


class TestDiversityAnchors(unittest.TestCase):
    def test_identical_answers_are_worth_one(self):
        """Six identical answers are one answer, however many were billed."""
        self.assertAlmostEqual(mean_pairwise_distance(IDENTICAL), 0.0, places=6)
        self.assertAlmostEqual(effective_n(IDENTICAL), 1.0, places=6)

    def test_disjoint_answers_are_worth_their_count(self):
        texts = ["alpha bravo charlie", "delta echo foxtrot", "golf hotel india"]
        self.assertAlmostEqual(mean_pairwise_distance(texts), 1.0, places=6)
        self.assertAlmostEqual(effective_n(texts), 3.0, places=6)

    def test_single_and_empty(self):
        self.assertEqual(effective_n([]), 0.0)
        self.assertEqual(effective_n(["only one"]), 1.0)
        self.assertEqual(mean_pairwise_distance(["only one"]), 0.0)

    def test_effective_n_between_anchors(self):
        eff = effective_n(DISTINCT)
        self.assertGreater(eff, 1.0)
        self.assertLessEqual(eff, len(DISTINCT))

    def test_two_empty_texts_are_identical_not_diverse(self):
        self.assertEqual(mean_pairwise_distance(["", ""]), 0.0)


class TestDiversityVerdicts(unittest.TestCase):
    def test_collapse_detected_and_named(self):
        rep = analyze(IDENTICAL, ["a", "b", "c", "d"])
        self.assertEqual(rep.verdict, "collapsed")
        self.assertIn("structural coupling", rep.advice)
        # The point of the metric: it must say the panel bought ~1 opinion.
        self.assertLess(rep.effective_n, 1.5)
        self.assertTrue(rep.redundant_pairs)

    def test_healthy_panel(self):
        rep = analyze(DISTINCT)
        self.assertIn(rep.verdict, ("healthy", "scattered"))
        self.assertGreater(rep.mean_pairwise_distance, COLLAPSE_MPD)

    def test_single_answer_is_not_corroboration(self):
        rep = analyze(["one answer only"])
        self.assertEqual(rep.verdict, "single")
        self.assertIn("not a panel", rep.advice)

    def test_label_count_must_match(self):
        with self.assertRaises(ValueError):
            analyze(["a", "b"], ["only-one-label"])

    def test_coverage_is_the_union(self):
        rep = analyze(["alpha bravo", "charlie delta"])
        self.assertEqual(rep.coverage_tokens, 4)


class TestCouplingRatio(unittest.TestCase):
    def test_sharing_that_collapses_the_panel_is_flagged(self):
        after = ["same converged answer about routing"] * 4
        out = coupling_ratio(DISTINCT, after)
        self.assertTrue(out["coupled"])
        self.assertGreater(out["contraction"], 0.6)

    def test_holding_positions_is_not_coupling(self):
        out = coupling_ratio(DISTINCT, DISTINCT)
        self.assertFalse(out["coupled"])
        self.assertAlmostEqual(out["ratio"], 1.0, places=6)

    def test_widening_is_reported_as_ratio_above_one(self):
        before = ["alpha bravo", "alpha bravo charlie"]
        after = ["alpha bravo", "xray yankee zulu"]
        out = coupling_ratio(before, after)
        self.assertGreater(out["ratio"], 1.0)
        self.assertEqual(out["contraction"], 0.0)

    def test_already_collapsed_returns_none_not_a_misleading_number(self):
        out = coupling_ratio(IDENTICAL, DISTINCT)
        self.assertIsNone(out["ratio"])
        self.assertIn("already collapsed", out["note"])


class TestVoteEntropy(unittest.TestCase):
    def test_unanimity_is_zero_bits(self):
        self.assertEqual(entropy_of_votes(["real"] * 5), 0.0)

    def test_even_split_is_one_bit(self):
        self.assertAlmostEqual(entropy_of_votes(["a", "b"]), 1.0, places=6)

    def test_empty(self):
        self.assertEqual(entropy_of_votes([]), 0.0)


class TestProtocols(unittest.TestCase):
    def test_every_protocol_isolates_its_first_phase(self):
        """Isolation is the intervention, not a convenience."""
        for pid, proto in PROTOCOLS.items():
            self.assertTrue(proto.phases[0].isolated,
                            f"{pid} does not isolate generation")

    def test_lens_assignment_is_distinct_until_exhausted(self):
        proto = get("adversarial")
        assigned = [name for name, _ in proto.assign(len(proto.lenses))]
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_reuse_is_reported_not_hidden(self):
        proto = get("adversarial")
        n = len(proto.lenses) + 2
        note = proto.assignment_note(n)
        self.assertIsNotNone(note)
        self.assertIn("correlated", note)

    def test_no_note_when_lenses_suffice(self):
        proto = get("adversarial")
        self.assertIsNone(proto.assignment_note(2))

    def test_recommend_routes_verification_to_adversarial(self):
        self.assertEqual(recommend("verify this bug is real", 4), "adversarial")

    def test_recommend_respects_small_panels(self):
        # A six-lens protocol at panel 2 would silently reuse lenses.
        self.assertEqual(recommend("design a new architecture", 2), "ngt")

    def test_recommend_design_task(self):
        self.assertEqual(recommend("redesign the retrieval approach", 4),
                         "double_diamond")

    def test_build_prompts_carries_lens_and_isolation_notice(self):
        proto = get("adversarial")
        prompts = build_prompts(proto, "check the hop bound", 3,
                                shared_context="- [n1] tiers are six deep")
        self.assertEqual(len(prompts), 3)
        lenses = {p["lens"] for p in prompts}
        self.assertEqual(len(lenses), 3)
        for p in prompts:
            self.assertIn("check the hop bound", p["prompt"])
            self.assertIn("isolation", p["prompt"])
            self.assertIn("[n1]", p["prompt"])

    def test_unknown_protocol_raises_with_the_known_list(self):
        with self.assertRaises(KeyError) as ctx:
            get("telepathy")
        self.assertIn("known", str(ctx.exception))


CORPUS = [
    Evidence(id="kg:router", summary="router assigns agency level 1..7 from "
                                     "policy severity", path="dobby/core/router.py",
             verified=True),
    Evidence(id="kg:memory", summary="six memory tiers with five hops",
             path="dobby/memory/tiers.py", verified=True),
]


class TestGroundingGate(unittest.TestCase):
    def test_ideation_blocked_without_prior_art(self):
        ok, note = has_prior_art([])
        self.assertFalse(ok)
        self.assertIn("before ideation", note)

    def test_unverified_only_corpus_warns_but_allows(self):
        ok, note = has_prior_art([Evidence(id="e1", summary="x", verified=False)])
        self.assertTrue(ok)
        self.assertIn("none VERIFIED", note)

    def test_idea_without_anchor_rejected(self):
        idea = Idea(title="Make it faster",
                    body="We should improve the retrieval speed in router.py "
                         "by caching results for 50 ms windows",
                    falsifiable_test="measure the dev score before and after")
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(NO_EVIDENCE, a.reasons)

    def test_fabricated_evidence_id_caught(self):
        """The fabricated-citation failure in miniature."""
        idea = Idea(title="Add prefetch to the tier walker",
                    body="Prefetch children in tiers.py when beam is 2, capping "
                         "at 50 nodes per hop",
                    evidence_ids=("kg:does-not-exist",),
                    falsifiable_test="run dobby memory route and compare hops")
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(UNKNOWN_EVIDENCE, a.reasons)
        self.assertTrue(any("fabricated" in n for n in a.notes))

    def test_vague_idea_rejected(self):
        idea = Idea(title="Improve things",
                    body="Make the system better and more robust overall",
                    evidence_ids=("kg:router",),
                    falsifiable_test="measure it somehow")
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(VAGUE, a.reasons)

    def test_missing_test_rejected(self):
        idea = Idea(title="Cache the tier index",
                    body="Hold tiers.py index in memory, capped at 500 nodes, "
                         "invalidated on write_tier",
                    evidence_ids=("kg:memory",))
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(NO_TEST, a.reasons)

    def test_restatement_of_prior_art_rejected(self):
        idea = Idea(title="six memory tiers with five hops",
                    body="six memory tiers with five hops in tiers.py",
                    evidence_ids=("kg:memory",),
                    falsifiable_test="run dobby memory stats and count tiers")
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(RESTATEMENT, a.reasons)

    def test_overclaim_without_substantiation_rejected(self):
        idea = Idea(title="Dramatically faster routing",
                    body="This revolutionary change to router.py makes lookup "
                         "10x faster and always works",
                    evidence_ids=())
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(OVERCLAIM, a.reasons)

    def test_well_formed_idea_accepted(self):
        idea = Idea(
            title="Bound the tier walker's beam by measured branching factor",
            body="In tiers.py, replace the fixed beam=2 with a beam derived "
                 "from the parent's child count, capped at 4, so wide forests "
                 "keep 2 branches and narrow ones spend 1",
            evidence_ids=("kg:memory",),
            falsifiable_test="run dobby memory route on 20 queries and compare "
                             "hops and hit rate against beam=2 baseline")
        a = assess(idea, CORPUS)
        self.assertTrue(a.accepted, a.reasons + a.notes)
        self.assertEqual(a.groundedness, 1.0)
        self.assertEqual(a.anchored_verified, 1)

    def test_gate_reports_a_histogram_not_just_a_count(self):
        ideas = [
            Idea(title="A", body="vague thing"),
            Idea(title="B", body="another vague thing"),
            Idea(title="C", body="third vague thing",
                 evidence_ids=("kg:nope",)),
        ]
        out = gate(ideas, CORPUS)
        self.assertEqual(out["accepted"], 0)
        self.assertIn(NO_EVIDENCE, out["rejection_histogram"])
        self.assertIn("no ideas survived", out["verdict"])

    def test_explore_cycle_returns_actionable_repairs(self):
        idea = Idea(title="Improve things", body="make it better")
        assessments = [assess(idea, CORPUS)]
        out = explore_cycle(assessments)
        self.assertEqual(out["needs_repair"], 1)
        self.assertTrue(out["items"][0]["repairs"])
        self.assertIn("Geneplore", out["guidance"])

    def test_accepted_ideas_produce_no_repairs(self):
        idea = Idea(
            title="Bound the beam by branching factor",
            body="In tiers.py, derive the walker beam from the parent's child "
                 "count instead of the fixed value, capping it at 4 so a wide "
                 "forest keeps two branches and a narrow one spends only 1",
            evidence_ids=("kg:memory",),
            falsifiable_test="run 20 queries and compare hops to the baseline")
        out = explore_cycle([assess(idea, CORPUS)])
        self.assertEqual(out["needs_repair"], 0)

    def test_body_too_short_is_rejected_as_vague(self):
        """A one-line idea cannot contain a mechanism; the gate must say so."""
        idea = Idea(title="Tune the beam", body="tiers.py beam should be 4",
                    evidence_ids=("kg:memory",),
                    falsifiable_test="run 20 queries and compare hops")
        a = assess(idea, CORPUS)
        self.assertFalse(a.accepted)
        self.assertIn(VAGUE, a.reasons)


if __name__ == "__main__":
    unittest.main()
