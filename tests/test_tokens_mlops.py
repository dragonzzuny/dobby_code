import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.mlops import (ExperimentSetup, check_interpretation,
                         check_reproducibility, compare_runs, detect_leakage,
                         ml_gate, multiple_comparison_note,
                         trivial_baseline_check)
from dobby.tokens import (SNAPSHOT_TIERS, blast_radius, build_snapshot,
                          condense, estimate_savings, pick_handler)

PYTEST_FAIL = "\n".join(
    ["collected 200 items", ""]
    + ["tests/test_a.py " + "." * 40] * 8
    + ["=" * 30 + " FAILURES " + "=" * 30,
       "_" * 20 + " test_thing " + "_" * 20,
       ">       assert x == 4",
       "E       assert 3 == 4",
       "",
       "=" * 25 + " short test summary info " + "=" * 25,
       "FAILED tests/test_a.py::test_thing - assert 3 == 4",
       "1 failed, 199 passed in 3.21s"])


class TestHandlerSelection(unittest.TestCase):
    def test_longest_prefix_wins(self):
        key, _ = pick_handler("git status --porcelain")
        self.assertEqual(key, "git status")

    def test_unknown_command_has_no_handler(self):
        self.assertEqual(pick_handler("frobnicate --all"), (None, None))


class TestCondense(unittest.TestCase):
    def test_failing_command_is_never_condensed(self):
        """The detail explaining a failure is what condensing removes."""
        c = condense("python -m pytest", PYTEST_FAIL, exit_code=1)
        self.assertTrue(c.passthrough)
        self.assertEqual(c.text, PYTEST_FAIL)
        self.assertIn("non-zero exit", c.note)

    def test_pytest_keeps_failures_drops_dots(self):
        c = condense("python -m pytest", PYTEST_FAIL, exit_code=0)
        self.assertFalse(c.passthrough)
        self.assertIn("FAILED", c.text)
        self.assertIn("assert 3 == 4", c.text)
        self.assertGreater(c.reduction(), 0.3)

    def test_short_output_passed_through(self):
        c = condense("git status", "M a.py")
        self.assertTrue(c.passthrough)
        self.assertIn("under", c.note)

    def test_unknown_command_passed_through(self):
        c = condense("frobnicate", "x" * 1000)
        self.assertTrue(c.passthrough)
        self.assertIn("no per-command handler", c.note)

    def test_git_status_groups_by_directory(self):
        text = "\n".join([f"?? pkg/f{i}.py" for i in range(60)] + [" M src/main.py"])
        c = condense("git status --porcelain", text)
        self.assertGreater(c.reduction(), 0.8)
        self.assertIn("60 paths", c.text)
        self.assertIn("src/main.py", c.text)

    def test_diff_drops_context_keeps_hunks(self):
        diff = "\n".join(["diff --git a/x.py b/x.py", "@@ -1,5 +1,6 @@",
                          " unchanged line", " another unchanged",
                          "+added line", "-removed line"] + [" ctx"] * 200)
        c = condense("git diff", diff)
        self.assertIn("+added line", c.text)
        self.assertNotIn("another unchanged", c.text)
        self.assertGreater(c.reduction(), 0.7)

    def test_listing_reports_counts(self):
        listing = "\n".join(f"file{i}.py" for i in range(200))
        c = condense("ls -la", listing)
        self.assertIn("200 entries", c.text)

    def test_raw_output_preserved_to_disk(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = condense("python -m pytest", PYTEST_FAIL, exit_code=0,
                     data_dir=tmp.name)
        self.assertIsNotNone(c.raw_path)
        with open(c.raw_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), PYTEST_FAIL)

    def test_dict_labels_the_estimate_as_an_estimate(self):
        c = condense("python -m pytest", PYTEST_FAIL)
        d = c.to_dict()
        self.assertIn("ESTIMATE", d["accounting_note"])
        self.assertIn("INPUT", d["accounting_note"])


class TestSnapshot(unittest.TestCase):
    EVENTS = {
        "files_modified": ["dobby/tokens.py", "dobby/review.py"],
        "decisions": ["PBR over CBR: lower cost per defect"],
        "blockers": ["ollama absent"],
        "tool_counts": ["bash x40"] * 30,
    }

    def test_budget_is_a_hard_bound_at_every_size(self):
        for budget in (60, 80, 150, 300, 700, 2048):
            snap = build_snapshot(self.EVENTS, budget_bytes=budget)
            self.assertLessEqual(snap["bytes"], budget,
                                 f"overshot at budget={budget}")

    def test_p1_survives_when_p4_is_dropped(self):
        """Priority order is the claim: working state outlives trivia."""
        snap = build_snapshot(self.EVENTS, budget_bytes=120)
        self.assertIn("files_modified", snap["included"])
        self.assertNotIn("tool_counts", snap["included"])

    def test_drops_are_reported_not_silent(self):
        snap = build_snapshot(self.EVENTS, budget_bytes=300)
        self.assertFalse(snap["complete"])
        self.assertTrue(snap["dropped"])
        self.assertIn("summary, not the whole session state", snap["note"])

    def test_complete_when_it_fits(self):
        snap = build_snapshot({"files_modified": ["a.py"]}, budget_bytes=2048)
        self.assertTrue(snap["complete"])
        self.assertEqual(snap["dropped"], {})

    def test_empty_events(self):
        snap = build_snapshot({})
        self.assertEqual(snap["text"], "")
        self.assertTrue(snap["complete"])

    def test_tier_policy_is_published(self):
        snap = build_snapshot({})
        self.assertEqual(set(snap["tier_policy"]),
                         {f"P{t}" for t in SNAPSHOT_TIERS})


class TestBlastRadius(unittest.TestCase):
    EDGES = [("cli.py", "tokens.py"), ("review.py", "tokens.py"),
             ("test_review.py", "review.py"), ("other.py", "unrelated.py")]

    def test_follows_edges_backward_to_find_dependents(self):
        out = blast_radius(self.EDGES, ["tokens.py"], max_hops=2)
        self.assertEqual(set(out["impacted"]),
                         {"cli.py", "review.py", "test_review.py"})
        self.assertNotIn("unrelated.py", out["impacted"])

    def test_hop_limit_respected(self):
        out = blast_radius(self.EDGES, ["tokens.py"], max_hops=1)
        self.assertEqual(set(out["impacted"]), {"cli.py", "review.py"})

    def test_truncation_is_reported_as_a_lower_bound(self):
        edges = [(f"dep{i}.py", "core.py") for i in range(50)]
        out = blast_radius(edges, ["core.py"], max_nodes=10)
        self.assertTrue(out["truncated"])
        self.assertIn("lower bound", out["note"])

    def test_isolated_change_has_empty_radius(self):
        out = blast_radius(self.EDGES, ["nothing_depends_on_me.py"])
        self.assertEqual(out["impacted"], [])
        self.assertFalse(out["truncated"])


class TestAccounting(unittest.TestCase):
    def test_style_instruction_cost_can_exceed_its_saving(self):
        """A brevity instruction resident every turn can be a net loss."""
        c = condense("git status --porcelain",
                     "\n".join(f"?? f{i}.py" for i in range(30)))
        out = estimate_savings([c], style_output_reduction=0.65,
                               style_instruction_chars=5000, turns=20)
        self.assertLess(out["net_estimated_input_tokens"], 0)

    def test_caveats_name_the_limits(self):
        out = estimate_savings([])
        joined = " ".join(out["caveats"])
        self.assertIn("ESTIMATE", joined)
        self.assertIn("reasoning", joined)
        self.assertIn("OUTPUT", joined)


# ======================================================================
class TestLeakage(unittest.TestCase):
    def test_fit_before_split_is_confirmed_from_ordering(self):
        setup = ExperimentSetup(
            steps=("StandardScaler.fit", "train_test_split", "model.fit"),
            split_method="train_test_split")
        out = detect_leakage(setup)
        self.assertTrue(out["blocks_result"])
        kinds = {l["kind"] for l in out["leaks"] if l["severity"] == "confirmed"}
        self.assertIn("fit_before_split", kinds)

    def test_fit_after_split_is_clean(self):
        setup = ExperimentSetup(
            steps=("train_test_split", "StandardScaler.fit", "model.fit"),
            split_method="train_test_split")
        out = detect_leakage(setup)
        confirmed = [l for l in out["leaks"] if l["severity"] == "confirmed"]
        self.assertEqual(confirmed, [])

    def test_feature_equal_to_target_is_confirmed(self):
        setup = ExperimentSetup(steps=("train_test_split",),
                                split_method="train_test_split",
                                feature_names=("age", "churned"),
                                target_name="churned")
        out = detect_leakage(setup)
        self.assertTrue(any(l["kind"] == "target_derived_feature"
                            and l["severity"] == "confirmed"
                            for l in out["leaks"]))

    def test_post_outcome_feature_is_only_suspected(self):
        """A naming heuristic must never produce a confirmed accusation."""
        setup = ExperimentSetup(steps=("train_test_split",),
                                split_method="train_test_split",
                                feature_names=("next_month_spend",),
                                target_name="revenue")
        out = detect_leakage(setup)
        leaks = [l for l in out["leaks"] if l["kind"] == "target_derived_feature"]
        self.assertTrue(leaks)
        self.assertTrue(all(l["severity"] == "suspected" for l in leaks))

    def test_group_spillover_confirmed_when_group_declared(self):
        setup = ExperimentSetup(steps=("train_test_split",),
                                split_method="train_test_split",
                                group_column="patient_id")
        out = detect_leakage(setup)
        self.assertTrue(any(l["kind"] == "group_spillover"
                            and l["severity"] == "confirmed"
                            for l in out["leaks"]))

    def test_group_aware_splitter_clears_it(self):
        setup = ExperimentSetup(steps=("GroupKFold.split",),
                                split_method="GroupKFold",
                                group_column="patient_id")
        out = detect_leakage(setup)
        self.assertFalse(any(l["kind"] == "group_spillover"
                             for l in out["leaks"]))

    def test_temporal_shuffle_confirmed(self):
        setup = ExperimentSetup(steps=("train_test_split",),
                                split_method="train_test_split", temporal=True)
        out = detect_leakage(setup)
        self.assertTrue(any(l["kind"] == "temporal_shuffle"
                            and l["severity"] == "confirmed"
                            for l in out["leaks"]))

    def test_timeseries_split_clears_temporal(self):
        setup = ExperimentSetup(steps=("TimeSeriesSplit.split",),
                                split_method="TimeSeriesSplit", temporal=True)
        out = detect_leakage(setup)
        self.assertFalse(any(l["kind"] == "temporal_shuffle"
                             for l in out["leaks"]))

    def test_repeated_holdout_evaluation_confirmed(self):
        setup = ExperimentSetup(steps=("train_test_split",),
                                split_method="GroupKFold",
                                test_set_evaluations=40)
        out = detect_leakage(setup)
        self.assertTrue(any(l["kind"] == "test_set_reused_for_selection"
                            and l["severity"] == "confirmed"
                            for l in out["leaks"]))

    def test_verdict_states_it_checked_the_description(self):
        setup = ExperimentSetup(steps=("TimeSeriesSplit.split",),
                                split_method="TimeSeriesSplit")
        out = detect_leakage(setup)
        self.assertIn("DESCRIPTION", out["verdict"])


class TestReproducibility(unittest.TestCase):
    def test_missing_everything_lists_every_gap_with_a_consequence(self):
        out = check_reproducibility(ExperimentSetup())
        self.assertFalse(out["reproducible"])
        self.assertGreaterEqual(out["gap_count"], 4)
        for gap in out["gaps"]:
            self.assertTrue(gap["consequence"])
            self.assertTrue(gap["fix"])

    def test_fully_recorded_setup_is_reproducible(self):
        out = check_reproducibility(ExperimentSetup(
            seeds=(0, 1, 2), data_version="sha256:abc", environment_pinned=True,
            command="python train.py --seed 0", n_train=800, n_test=200))
        self.assertTrue(out["reproducible"], out["gaps"])


class TestStatisticalRigor(unittest.TestCase):
    def test_single_run_per_arm_cannot_separate_signal_from_noise(self):
        out = compare_runs([0.80], [0.85])
        self.assertFalse(out["comparable"])
        self.assertIn("UNVERIFIED", out["verdict"])

    def test_delta_inside_the_noise_is_not_an_improvement(self):
        out = compare_runs([0.80, 0.86, 0.74], [0.82, 0.88, 0.76])
        self.assertTrue(out["comparable"])
        self.assertFalse(out["better"])
        self.assertIn("NOT distinguishable", out["verdict"])

    def test_clear_separation_is_reported_as_real(self):
        out = compare_runs([0.700, 0.702, 0.701], [0.900, 0.902, 0.901])
        self.assertTrue(out["better"])
        self.assertFalse(out["ranges_overlap"])

    def test_zero_variance_with_a_gap_is_flagged_as_suspicious(self):
        out = compare_runs([0.5, 0.5, 0.5], [0.7, 0.7, 0.7])
        self.assertIn("not independent", out["verdict"])

    def test_accuracy_below_majority_class_beats_nothing(self):
        out = trivial_baseline_check("accuracy", 0.94, majority_class_rate=0.95)
        self.assertFalse(out["beats_trivial"])
        self.assertIn("demonstrated nothing", out["verdict"])

    def test_auc_floor_is_half(self):
        out = trivial_baseline_check("roc_auc", 0.48)
        self.assertEqual(out["trivial_floor"], 0.5)
        self.assertFalse(out["beats_trivial"])

    def test_unknown_metric_says_it_is_uninterpretable(self):
        out = trivial_baseline_check("custom_score", 0.9)
        self.assertFalse(out["checked"])
        self.assertIn("not interpretable", out["note"])

    def test_multiple_comparisons_quantified(self):
        out = multiple_comparison_note(60)
        self.assertGreater(out["family_wise_error"], 0.9)
        self.assertIn("optimistically biased", out["note"])

    def test_single_config_has_no_selection_bias(self):
        self.assertIn("no selection bias", multiple_comparison_note(1)["note"])


class TestInterpretation(unittest.TestCase):
    def test_causal_language_from_a_predictive_model_is_flagged(self):
        out = check_interpretation(
            "Feature importance shows that low tenure causes churn.",
            is_causal_design=False, evaluated_on_holdout=True,
            population_described=True)
        self.assertFalse(out["supported"])
        self.assertTrue(any(p["kind"] == "causal_from_predictive"
                            for p in out["problems"]))

    def test_causal_design_permits_causal_language(self):
        out = check_interpretation(
            "The randomized rollout shows the banner causes a lift.",
            is_causal_design=True, evaluated_on_holdout=True,
            population_described=True)
        self.assertTrue(out["supported"], out["problems"])

    def test_unbounded_generalization_flagged(self):
        out = check_interpretation(
            "This model works in production for all users.",
            is_causal_design=True, evaluated_on_holdout=True,
            population_described=False)
        self.assertTrue(any(p["kind"] == "unbounded_generalization"
                            for p in out["problems"]))

    def test_number_without_holdout_flagged(self):
        out = check_interpretation(
            "The model improves recall by 12% over the baseline.",
            is_causal_design=True, evaluated_on_holdout=False,
            population_described=True)
        self.assertTrue(any(p["kind"] == "number_without_holdout"
                            for p in out["problems"]))


class TestCombinedGate(unittest.TestCase):
    def test_leakage_short_circuits_everything_downstream(self):
        """A leaked result must not be dressed in statistics."""
        out = ml_gate(
            ExperimentSetup(steps=("StandardScaler.fit", "train_test_split"),
                            split_method="train_test_split"),
            baseline_runs=[0.1, 0.1, 0.1], candidate_runs=[0.9, 0.9, 0.9],
            metric="accuracy", score=0.99, majority_class_rate=0.5,
            conclusion="Our method is better.")
        self.assertEqual(out["decision"], "INVALID")
        self.assertTrue(out["comparison"]["skipped"])
        self.assertTrue(out["interpretation"]["skipped"])

    def test_not_established_when_trivial_baseline_wins(self):
        out = ml_gate(
            ExperimentSetup(steps=("GroupKFold.split", "StandardScaler.fit"),
                            split_method="GroupKFold", seeds=(0,),
                            data_version="v1", environment_pinned=True,
                            command="python t.py", n_train=8, n_test=2),
            metric="accuracy", score=0.90, majority_class_rate=0.95)
        self.assertEqual(out["decision"], "NOT_ESTABLISHED")
        self.assertIn("trivial baseline", " ".join(out["blocking"]))

    def test_provisional_when_reproducibility_gaps_remain(self):
        out = ml_gate(
            ExperimentSetup(steps=("GroupKFold.split", "StandardScaler.fit"),
                            split_method="GroupKFold"),
            metric="roc_auc", score=0.81)
        self.assertEqual(out["decision"], "PROVISIONAL")

    def test_established_when_every_gate_passes(self):
        out = ml_gate(
            ExperimentSetup(steps=("GroupKFold.split", "StandardScaler.fit",
                                   "model.fit"),
                            split_method="GroupKFold", group_column=None,
                            seeds=(0, 1, 2), data_version="sha256:abc",
                            environment_pinned=True,
                            command="python train.py", n_train=800, n_test=200),
            baseline_runs=[0.700, 0.701, 0.702],
            candidate_runs=[0.900, 0.901, 0.902],
            metric="roc_auc", score=0.90,
            conclusion="The model is associated with higher recall on this "
                       "cohort.",
            evaluated_on_holdout=True, population_described=True)
        self.assertEqual(out["decision"], "ESTABLISHED", out)


if __name__ == "__main__":
    unittest.main()
