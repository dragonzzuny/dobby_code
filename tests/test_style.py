import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.style import (COMMA_PER_SENTENCE_CEILING, REWRITE_ABORT_RATE, S1,
                         S2, S3, UNIFORMITY_STDEV_FLOOR, analyze, measure,
                         rewrite_budget, rewrite_instruction, sentences)

UNIFORM = (
    "The system processes the input and returns a result to the caller. "
    "The module handles the request and produces an output for the user. "
    "The service accepts a payload and delivers a response to the client. "
    "The handler receives a message and forwards a reply to the sender. "
    "The worker consumes a task and emits a record for the pipeline.")

VARIED = (
    "The tokenizer was ASCII-only. That broke Korean, and it broke it silently: "
    "three unrelated sentences scored as identical while every test passed, "
    "because every test used English. I found it by accident. "
    "A Korean request came back with a gap that should not have been there.")

KO_AI = ("이 시스템은 혁신적인 접근을 통해, 다양한 문제를 해결할 수 있을 것으로 "
         "보인다. 결론적으로, 시사하는 바가 크다고 할 수 있다.")


class TestMeasurement(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(analyze("")["verdict"], "empty")
        self.assertEqual(measure("")["sentences"], 0)

    def test_sentence_splitting_handles_cjk_terminators(self):
        self.assertEqual(len(sentences("첫 문장이다. 두 번째다。 세 번째？")), 3)

    def test_stdev_is_zero_for_a_single_sentence(self):
        self.assertEqual(measure("one sentence here")["sentence_stdev"], 0.0)

    def test_counts_are_reported(self):
        stats = measure("a, b, c. **bold** — dash;\n- bullet\n")
        self.assertGreaterEqual(stats["commas"], 2)
        self.assertEqual(stats["em_dashes"], 1)
        self.assertEqual(stats["bold_runs"], 1)
        self.assertEqual(stats["bullets"], 1)
        self.assertEqual(stats["semicolons"], 1)


class TestUniformitySignal(unittest.TestCase):
    """Uniformity, not vocabulary, is the primary tell."""

    def test_uniform_lengths_are_flagged_deterministically(self):
        report = analyze(UNIFORM)
        self.assertIn("uniform_sentence_length", report["acting_signals"])
        signal = next(s for s in report["signals"]
                      if s["code"] == "uniform_sentence_length")
        self.assertEqual(signal["severity"], S1)

    def test_varied_lengths_are_not_flagged(self):
        self.assertNotIn("uniform_sentence_length",
                         analyze(VARIED)["acting_signals"])

    def test_short_text_is_not_judged_on_uniformity(self):
        """Three sentences is not enough to call a rhythm."""
        report = analyze("One. Two words here. Three words are here.")
        self.assertNotIn("uniform_sentence_length", report["acting_signals"])

    def test_the_fix_says_no_vocabulary_change_helps(self):
        report = analyze(UNIFORM)
        signal = next(s for s in report["signals"]
                      if s["code"] == "uniform_sentence_length")
        self.assertIn("no vocabulary change fixes it", signal["fix"])


class TestSeverityTiers(unittest.TestCase):
    def test_s1_acts_on_one_occurrence(self):
        report = analyze("이 결과는 시사하는 바가 크다. 다른 문장이 여기 있다.")
        self.assertTrue(any(c.startswith("phrase:시사") for c in
                            report["acting_signals"]))

    def test_s2_needs_three(self):
        once = analyze("It is worth noting that the value is 4. Another line.")
        self.assertNotIn("phrase:it is worth noting", once["acting_signals"])
        thrice = ("It is worth noting that a is 1. It is worth noting that b "
                  "is 2. It is worth noting that c is 3.")
        self.assertIn("phrase:it is worth noting",
                      analyze(thrice)["acting_signals"])

    def test_s3_never_acts_alone(self):
        report = analyze("Furthermore the value is 4. The other value is 9.")
        self.assertNotIn("phrase:furthermore", report["acting_signals"])

    def test_three_weak_signals_together_do_act(self):
        text = ("Furthermore the value is 4. Moreover the other one is 9. "
                "Additionally a third exists here.")
        report = analyze(text)
        weak = [c for c in report["acting_signals"] if c.startswith("phrase:")]
        self.assertGreaterEqual(len(weak), 3)


class TestKoreanSignals(unittest.TestCase):
    def test_connective_comma_is_deterministic(self):
        report = analyze("데이터를 처리하고, 결과를 반환한다. 값을 저장하며, 종료한다.")
        self.assertIn("ko:connective_comma", report["acting_signals"])
        signal = next(s for s in report["signals"]
                      if s["code"] == "ko:connective_comma")
        self.assertEqual(signal["severity"], S1)
        self.assertIn("imported English punctuation", signal["fix"])

    def test_clean_korean_has_no_connective_comma(self):
        report = analyze("데이터를 처리하고 결과를 반환한다. 값을 저장하며 종료한다.")
        self.assertNotIn("ko:connective_comma", report["acting_signals"])

    def test_korean_ai_phrases_detected(self):
        codes = analyze(KO_AI)["acting_signals"]
        self.assertTrue(any("시사하는" in c for c in codes))

    def test_korean_hedge_stack(self):
        report = analyze("성능이 향상될 수 있다고 보이며 비용도 절감될 것으로 보인다. "
                         "다른 문장.")
        signals = {s["code"] for s in report["signals"]}
        self.assertIn("hedge_stack", signals)


class TestOtherSignals(unittest.TestCase):
    def test_comma_density(self):
        text = ("First, we do a thing, and then, after that, another. "
                "Second, we check, verify, and then, finally, report.")
        report = analyze(text)
        self.assertIn("comma_density", report["acting_signals"])
        self.assertGreater(report["stats"]["commas_per_sentence"],
                           COMMA_PER_SENTENCE_CEILING)

    def test_hedge_stack_in_english(self):
        report = analyze("This may possibly work in some cases. Another line.")
        self.assertIn("hedge_stack", {s["code"] for s in report["signals"]})

    def test_single_hedge_is_fine(self):
        report = analyze("This may work. Another line here for the count.")
        self.assertNotIn("hedge_stack", {s["code"] for s in report["signals"]})

    def test_rule_of_three_needs_repetition(self):
        one = analyze("We measured speed, cost, and accuracy in the run.")
        self.assertNotIn("rule_of_three", {s["code"] for s in one["signals"]})
        two = ("We measured speed, cost, and accuracy. "
               "The system is fast, cheap, and correct.")
        self.assertIn("rule_of_three", {s["code"] for s in analyze(two)["signals"]})

    def test_em_dash_rate_is_only_weak(self):
        report = analyze("A — b. C — d. E — f. G — h.")
        signal = next((s for s in report["signals"]
                       if s["code"] == "em_dash_rate"), None)
        if signal:
            self.assertEqual(signal["severity"], S3)


class TestCleanProse(unittest.TestCase):
    def test_human_text_produces_no_acting_signal(self):
        self.assertEqual(analyze(VARIED)["acting_signals"], [])

    def test_verdict_says_so_plainly(self):
        self.assertIn("does not carry", analyze(VARIED)["verdict"])


class TestRewriteBudget(unittest.TestCase):
    def test_surgical_edit_accepted(self):
        before = "It is worth noting that the value is four and the flag is set."
        after = "The value is four and the flag is set."
        out = rewrite_budget(before, after)
        self.assertTrue(out["accepted"])
        self.assertLess(out["rate"], 0.5)

    def test_wholesale_replacement_aborts(self):
        """Above the abort rate it is ghostwriting, not editing."""
        before = "The tokenizer was ASCII only and that broke Korean silently."
        after = "Completely different content about unrelated matters entirely."
        out = rewrite_budget(before, after)
        self.assertFalse(out["accepted"])
        self.assertIn("ABORT", out["verdict"])
        self.assertGreater(out["rate"], REWRITE_ABORT_RATE)

    def test_reordering_counts_as_a_small_edit(self):
        before = "the value is four and the flag is set"
        after = "the flag is set and the value is four"
        self.assertLess(rewrite_budget(before, after)["rate"], 0.1)

    def test_identical_is_zero(self):
        self.assertEqual(rewrite_budget("same text here", "same text here")["rate"],
                         0.0)

    def test_empty_original(self):
        self.assertIn("nothing to compare", rewrite_budget("", "x")["verdict"])


class TestRewriteInstruction(unittest.TestCase):
    def test_no_signal_means_do_not_rewrite(self):
        instruction = rewrite_instruction(analyze(VARIED))
        self.assertIn("Do not rewrite", instruction)

    def test_instruction_names_specific_signals_not_make_it_human(self):
        instruction = rewrite_instruction(analyze(UNIFORM))
        self.assertNotIn("sound human", instruction.lower())
        self.assertIn("Signals to remove", instruction)

    def test_instruction_protects_content(self):
        instruction = rewrite_instruction(analyze(UNIFORM))
        self.assertIn("file path", instruction)
        self.assertIn("negation", instruction)

    def test_instruction_states_the_change_budget(self):
        instruction = rewrite_instruction(analyze(UNIFORM))
        self.assertIn("30%", instruction)
        self.assertIn("50%", instruction)

    def test_instruction_forbids_swapping_one_stock_set_for_another(self):
        instruction = rewrite_instruction(analyze(UNIFORM))
        self.assertIn("different set of stock phrases", instruction)


if __name__ == "__main__":
    unittest.main()
