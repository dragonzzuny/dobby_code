import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.search import (DEBUG, DRAFT, IMPROVE, STOP_ALL_BUGGY, STOP_BUDGET,
                          STOP_CONVERGED, STOP_TARGET, Case, Layer, Node,
                          retain_case, retrieve_cases, search,
                          suggest_pipeline, validate_pipeline, yield_report)


def scripted(steps):
    """An expander that returns a queued result per call, recording the actions."""
    queue = list(steps)
    seen = []

    def expand(action, parent, context):
        seen.append((action, parent.id if parent else None))
        return queue.pop(0) if queue else {"content": "x", "score": 0.0}

    expand.seen = seen
    return expand


class TestPolicyOrder(unittest.TestCase):
    def test_drafts_come_first(self):
        """Improving one early draft is the linear-agent failure to avoid."""
        exp = scripted([{"content": "a", "score": 0.1},
                        {"content": "b", "score": 0.2},
                        {"content": "c", "score": 0.3},
                        {"content": "d", "score": 0.4}])
        search(expand=exp, max_nodes=4, min_drafts=3)
        self.assertEqual([a for a, _ in exp.seen[:3]], [DRAFT, DRAFT, DRAFT])
        self.assertEqual(exp.seen[3][0], IMPROVE)

    def test_buggy_node_is_debugged_before_improving(self):
        exp = scripted([{"content": "a", "score": 0.5},
                        {"content": "b", "buggy": True, "error": "SyntaxError"},
                        {"content": "c", "score": 0.6}])
        search(expand=exp, max_nodes=3, min_drafts=2)
        self.assertEqual(exp.seen[2][0], DEBUG)
        self.assertEqual(exp.seen[2][1], "n2")

    def test_improve_targets_the_best_node_not_the_newest(self):
        exp = scripted([{"content": "a", "score": 0.9},
                        {"content": "b", "score": 0.1},
                        {"content": "c", "score": 0.5}])
        search(expand=exp, max_nodes=3, min_drafts=2)
        self.assertEqual(exp.seen[2], (IMPROVE, "n1"))

    def test_debug_depth_bound_abandons_a_broken_branch(self):
        """Without the bound, the whole budget goes into one broken script."""
        exp = scripted([{"content": "a", "buggy": True, "error": "boom"}] * 10)
        result = search(expand=exp, max_nodes=10, min_drafts=1, debug_depth=2)
        debugs = [a for a, _ in exp.seen if a == DEBUG]
        # depth 0 -> 1 -> 2 means at most `debug_depth` repair attempts per chain
        self.assertLessEqual(len(debugs), 6)
        self.assertEqual(result.stopped_because, STOP_ALL_BUGGY)

    def test_all_buggy_stops_rather_than_looping(self):
        exp = scripted([{"content": "x", "buggy": True}] * 20)
        result = search(expand=exp, max_nodes=20, min_drafts=2, debug_depth=1)
        self.assertEqual(result.stopped_because, STOP_ALL_BUGGY)
        self.assertIsNone(result.best)


class TestScoringDiscipline(unittest.TestCase):
    def test_unscored_node_is_treated_as_buggy(self):
        """An unscored solution cannot be compared, so it must not be selectable."""
        exp = scripted([{"content": "a"}])
        result = search(expand=exp, max_nodes=1, min_drafts=1)
        self.assertTrue(result.nodes[0].buggy)
        self.assertIsNone(result.best)
        self.assertIn("cannot be compared", result.nodes[0].error)

    def test_buggy_node_never_wins_best(self):
        exp = scripted([{"content": "a", "buggy": True},
                        {"content": "b", "score": -5.0}])
        result = search(expand=exp, max_nodes=2, min_drafts=2)
        self.assertEqual(result.best.id, "n2")

    def test_lower_is_better_flips_selection(self):
        exp = scripted([{"content": "a", "score": 0.9},
                        {"content": "b", "score": 0.1},
                        {"content": "c", "score": 0.5}])
        result = search(expand=exp, max_nodes=3, min_drafts=3,
                        higher_is_better=False)
        self.assertEqual(result.best.id, "n2")

    def test_lower_is_better_improves_the_lowest(self):
        exp = scripted([{"content": "a", "score": 0.9},
                        {"content": "b", "score": 0.1},
                        {"content": "c", "score": 0.5}])
        search(expand=exp, max_nodes=3, min_drafts=2, higher_is_better=False)
        self.assertEqual(exp.seen[2], (IMPROVE, "n2"))


class TestStopping(unittest.TestCase):
    def test_budget_and_convergence_are_distinguished(self):
        """'Ran out of money mid-climb' is not 'we plateaued'."""
        climbing = scripted([{"content": str(i), "score": i / 10.0}
                             for i in range(1, 6)])
        result = search(expand=climbing, max_nodes=5, min_drafts=2, patience=10)
        self.assertEqual(result.stopped_because, STOP_BUDGET)

        flat = scripted([{"content": "a", "score": 0.5}] * 12)
        result = search(expand=flat, max_nodes=12, min_drafts=2, patience=3)
        self.assertEqual(result.stopped_because, STOP_CONVERGED)

    def test_patience_does_not_fire_during_drafting(self):
        exp = scripted([{"content": "a", "score": 0.5}] * 6)
        result = search(expand=exp, max_nodes=6, min_drafts=5, patience=1)
        self.assertGreaterEqual(result.drafts, 5)

    def test_target_score_stops_early(self):
        exp = scripted([{"content": "a", "score": 0.1},
                        {"content": "b", "score": 0.99}])
        result = search(expand=exp, max_nodes=10, min_drafts=2,
                        target_score=0.9)
        self.assertEqual(result.stopped_because, STOP_TARGET)
        self.assertEqual(result.evaluated, 2)


class TestHoldoutHonesty(unittest.TestCase):
    def test_missing_holdout_is_loudly_refused(self):
        exp = scripted([{"content": "a", "score": 0.9}] * 4)
        result = search(expand=exp, max_nodes=4, min_drafts=2)
        self.assertIsNone(result.holdout_score)
        self.assertIn("NO HOLDOUT", result.holdout_note)
        self.assertIn("optimistically biased", result.holdout_note)

    def test_holdout_supplied_becomes_the_reportable_number(self):
        exp = scripted([{"content": "a", "score": 0.9}] * 3)
        result = search(expand=exp, max_nodes=3, min_drafts=2,
                        holdout_eval=lambda n: 0.71)
        self.assertEqual(result.holdout_score, 0.71)
        self.assertIn("reportable number", result.holdout_note)

    def test_summary_always_carries_the_selection_bias_warning(self):
        exp = scripted([{"content": "a", "score": 0.9}] * 3)
        summary = search(expand=exp, max_nodes=3, min_drafts=2).summary()
        self.assertIn("optimistically biased",
                      summary["selection_bias_warning"])

    def test_no_viable_solution_says_so(self):
        exp = scripted([{"content": "a", "buggy": True}] * 3)
        result = search(expand=exp, max_nodes=3, min_drafts=3, debug_depth=0)
        self.assertIn("nothing to score", result.holdout_note)


class TestSummaryContext(unittest.TestCase):
    def test_summary_carries_scores_and_errors_but_not_content(self):
        seen_summaries = []

        def expand(action, parent, context):
            seen_summaries.append(context["summary"])
            return {"content": "VERY_LONG_SCRIPT_BODY" * 50, "score": 0.5}

        search(expand=expand, max_nodes=3, min_drafts=2)
        last = seen_summaries[-1]
        self.assertIn("score=0.5", last)
        self.assertNotIn("VERY_LONG_SCRIPT_BODY", last,
                         "inlining node content saturates the context window")

    def test_context_names_the_parent_error_for_debug(self):
        contexts = []

        def expand(action, parent, context):
            contexts.append(context)
            if len(contexts) == 1:
                return {"content": "a", "buggy": True, "error": "ZeroDivision"}
            return {"content": "b", "score": 0.5}

        search(expand=expand, max_nodes=2, min_drafts=1, debug_depth=2)
        self.assertEqual(contexts[1]["action"], DEBUG)
        self.assertIn("ZeroDivision", contexts[1]["parent_error"])


class TestSearchGuards(unittest.TestCase):
    def test_min_drafts_below_one_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            search(expand=lambda *a: {}, min_drafts=0)
        self.assertIn("linear agent", str(ctx.exception))

    def test_max_nodes_below_one_is_rejected(self):
        with self.assertRaises(ValueError):
            search(expand=lambda *a: {}, max_nodes=0)


# ======================================================================
class TestPipelineValidation(unittest.TestCase):
    def test_rank_with_one_candidate_is_a_paid_noop(self):
        out = validate_pipeline([Layer("generate", n=1), Layer("rank", keep=1)])
        self.assertFalse(out["valid"])
        self.assertIn("no-op", out["problems"][0]["detail"])

    def test_fuse_after_collapse_is_flagged(self):
        out = validate_pipeline([Layer("generate", n=4), Layer("fuse"),
                                 Layer("fuse")])
        self.assertFalse(out["valid"])

    def test_revise_without_critique_is_flagged(self):
        out = validate_pipeline([Layer("generate", n=3), Layer("rank", keep=1),
                                 Layer("revise")])
        self.assertTrue(any("no preceding critique" in p["detail"]
                            for p in out["problems"]))

    def test_pipeline_not_starting_with_generate(self):
        out = validate_pipeline([Layer("critique"), Layer("generate", n=2)])
        self.assertTrue(any("nothing to operate on" in p["detail"]
                            for p in out["problems"]))

    def test_uncollapsed_end_is_flagged(self):
        out = validate_pipeline([Layer("generate", n=4), Layer("rank", keep=3)])
        self.assertTrue(any("does not collapse" in p["detail"]
                            for p in out["problems"]))

    def test_keep_larger_than_candidates(self):
        out = validate_pipeline([Layer("generate", n=2), Layer("rank", keep=9),
                                 Layer("fuse")])
        self.assertTrue(any("exceeds" in p["detail"] for p in out["problems"]))

    def test_well_formed_pipeline_passes(self):
        out = validate_pipeline([Layer("generate", n=5), Layer("rank", keep=3),
                                 Layer("fuse")])
        self.assertTrue(out["valid"], out["problems"])
        self.assertEqual(out["candidates_at_end"], 1)

    def test_empty_pipeline(self):
        self.assertFalse(validate_pipeline([])["valid"])

    def test_unknown_layer_kind_rejected(self):
        with self.assertRaises(ValueError):
            Layer("telepathy")


class TestPipelineSuggestion(unittest.TestCase):
    def test_single_call_budget_is_the_honest_baseline(self):
        out = suggest_pipeline(budget_calls=1)
        self.assertEqual(len(out["layers"]), 1)
        self.assertIn("baseline", out["rationale"])

    def test_every_suggestion_validates(self):
        for kind in ("verifiable", "open_ended", "general"):
            for budget in (2, 4, 8, 16):
                out = suggest_pipeline(budget_calls=budget, task_kind=kind)
                self.assertTrue(out["validation"]["valid"],
                                f"{kind}@{budget}: {out['validation']}")

    def test_verifiable_tasks_buy_a_verifier(self):
        out = suggest_pipeline(budget_calls=8, task_kind="verifiable")
        self.assertIn("verify", [l.kind for l in out["layers"]])

    def test_open_ended_tasks_buy_breadth_and_fusion(self):
        out = suggest_pipeline(budget_calls=8, task_kind="open_ended")
        kinds = [l.kind for l in out["layers"]]
        self.assertIn("fuse", kinds)

    def test_zero_budget_rejected(self):
        with self.assertRaises(ValueError):
            suggest_pipeline(budget_calls=0)


# ======================================================================
BANK = [
    Case(id="c1", task="predict customer churn from tabular billing data",
         approach="gradient boosting with group split on customer_id",
         outcome_score=0.81, succeeded=True, verified=True,
         tags=("tabular", "classification")),
    Case(id="c2", task="forecast daily demand from time series",
         approach="temporal split with lag features",
         outcome_score=0.66, succeeded=True, verified=False,
         tags=("timeseries",)),
    Case(id="c3", task="predict churn using random split",
         approach="random k-fold ignoring customer grouping",
         outcome_score=0.99, succeeded=False, tags=("tabular", "leakage")),
]


class TestCaseBank(unittest.TestCase):
    def test_retrieves_similar_successes(self):
        out = retrieve_cases(BANK, "predict churn for billing customers")
        self.assertTrue(out["reuse"])
        self.assertEqual(out["reuse"][0]["id"], "c1")

    def test_failures_are_excluded_by_default(self):
        out = retrieve_cases(BANK, "predict churn")
        ids = {r["id"] for r in out["reuse"]}
        self.assertNotIn("c3", ids)
        self.assertEqual(out["avoid"], [])

    def test_failures_available_on_request_in_a_separate_list(self):
        """A recorded dead end is the cheapest possible saving."""
        out = retrieve_cases(BANK, "predict churn", require_success=False)
        self.assertTrue(any(r["id"] == "c3" for r in out["avoid"]))
        self.assertNotIn("c3", {r["id"] for r in out["reuse"]})

    def test_empty_match_tells_you_to_retain(self):
        out = retrieve_cases([], "something entirely new")
        self.assertEqual(out["reuse"], [])
        self.assertIn("RETAIN", out["note"])

    def test_retain_rejects_a_duplicate(self):
        bank = list(BANK)
        dup = Case(id="c4",
                   task="predict customer churn from tabular billing data",
                   approach="gradient boosting with group split on customer_id",
                   outcome_score=0.80, succeeded=True)
        out = retain_case(bank, dup)
        self.assertFalse(out["retained"])
        self.assertIn("c1", out["reason"])

    def test_verified_case_replaces_an_unverified_duplicate(self):
        bank = [Case(id="old", task="forecast daily demand from time series",
                     approach="temporal split with lag features",
                     outcome_score=0.6, succeeded=True, verified=False)]
        new = Case(id="new", task="forecast daily demand from time series",
                   approach="temporal split with lag features",
                   outcome_score=0.66, succeeded=True, verified=True)
        out = retain_case(bank, new)
        self.assertTrue(out["retained"])
        self.assertEqual([c.id for c in bank], ["new"])

    def test_retain_accepts_a_novel_case(self):
        bank = list(BANK)
        novel = Case(id="c9", task="segment satellite imagery into land cover",
                     approach="unet with spatial blocking",
                     outcome_score=0.7, succeeded=True)
        self.assertTrue(retain_case(bank, novel)["retained"])
        self.assertEqual(len(bank), len(BANK) + 1)

    def test_empty_case_rejected(self):
        self.assertFalse(retain_case([], Case(id="x", task="", approach="",
                                              outcome_score=None,
                                              succeeded=True))["retained"])


class TestYieldReport(unittest.TestCase):
    def test_low_yield_is_reported_as_normal_not_broken(self):
        out = yield_report(attempts=700, genuine=20)
        self.assertLess(out["yield_rate"], 0.05)
        self.assertIn("published norm", out["calibration"])
        self.assertEqual(out["attempts_per_improvement"], 35.0)

    def test_zero_yield_is_a_result_worth_reporting(self):
        out = yield_report(attempts=200, genuine=0)
        self.assertEqual(out["yield_rate"], 0.0)
        self.assertIn("worth reporting rather than hiding", out["note"])
        self.assertIsNone(out["attempts_per_improvement"])

    def test_no_attempts(self):
        self.assertEqual(yield_report(attempts=0, genuine=0)["attempts"], 0)

    def test_suspiciously_high_yield_gets_the_calibration_warning(self):
        out = yield_report(attempts=10, genuine=9)
        self.assertIn("validation gate is too weak", out["calibration"])


if __name__ == "__main__":
    unittest.main()
