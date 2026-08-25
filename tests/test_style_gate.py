"""The prose gate: dobby's own writing has to pass it.

`dobby/style.py` detects the signature that makes generated text read as
generated, and it had one caller in the whole repository — somebody typing
`dobby style`. It printed a report and exited zero either way. A module written
to keep generated writing out of a deliverable could describe it and never stop
one.

Three things had to be true before it could stop anything:

1. A MACHINE verdict. `_verdict` returns prose for a person, and prose cannot
   fail a build. `gate()` is the tiers turned into `(ok, reason)`.
2. An exit code, so `dobby style --check` can be an acceptance check.
3. A payload-aware hook, because an acceptance check is a shell command and
   cannot see inside an artifact. `ArtifactContract.prose_at` names the field
   the way `grounding.claims_at` already does.

And one thing had to be FIXED first, or the gate would have refused human
writing: sentence uniformity was measured as an absolute stdev against a
relative property. Prose averaging 5.7 words per sentence cannot reach a stdev
of 5.0 at any variance. The Korean human sample below was being flagged S1 —
"one occurrence is sufficient evidence" — on a 6x spread between its shortest
and longest sentence.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import ArtifactContract, SCHEMAS  # noqa: E402
from dobby.runtime.runner import Runner, default_graph  # noqa: E402
from dobby.runtime.workers import (WorkerAdapter,  # noqa: E402
                                   WorkerRegistry, WorkerResult)
from dobby.style import (GATE_ACTING_CEILING, UNIFORMITY_CV_FLOOR,  # noqa: E402
                         analyze, gate, measure)

#: Both are Korean, both are six sentences, and their sentence-length stdev is
#: IDENTICAL at 4.07. The absolute measure cannot tell them apart even in
#: principle; the coefficient of variation reads 0.53 against 0.72.
AI_KO = ("본 연구는 인공지능 기술을 통해 다양한 산업 분야에 있어서 혁신적인 "
         "변화를 가져올 수 있을 것으로 보인다. 첫째, 생산성 향상이 기대된다. "
         "둘째, 비용 절감이 예상된다. 셋째, 새로운 시장이 창출될 것으로 "
         "예상된다. 또한, 이러한 변화는 사회 전반에 시사하는 바가 크다. "
         "따라서, 결론적으로 지속적인 관심이 필요하다고 할 수 있다.")
HUMAN_KO = ("어제 팀 회의에서 이 얘기가 나왔다. 다들 반신반의했다. "
            "나는 반대했다. 이유는 간단하다. 지난번에도 같은 방식으로 했다가 "
            "이틀을 날렸고, 그때 아무도 로그를 안 봤기 때문이다. 이번에는 "
            "먼저 로그부터 보자고 했더니 회의가 십 분 만에 끝났다.")
AI_EN = ("This approach offers several key benefits. First, it improves "
         "efficiency. Second, it reduces overall costs. Third, it enables new "
         "capabilities. Furthermore, these changes have significant "
         "implications.")
HUMAN_EN = ("The store was the other door into artifact state, and it was "
            "unlocked. The runner never did that. Nothing was broken. It was a "
            "claim that was not true, and it held because of what the runner "
            "happened to call rather than because anything refused.")


class UniformityIsRelative(unittest.TestCase):
    def test_the_two_korean_samples_have_the_same_stdev(self):
        """The premise. If this ever stops holding, the test below is moot."""
        self.assertEqual(measure(AI_KO)["sentence_stdev"],
                         measure(HUMAN_KO)["sentence_stdev"])

    def test_and_the_coefficient_of_variation_separates_them(self):
        ai, human = measure(AI_KO), measure(HUMAN_KO)
        self.assertLess(ai["sentence_cv"], human["sentence_cv"])
        self.assertGreater(human["sentence_cv"], UNIFORMITY_CV_FLOOR)

    def test_short_sentences_are_no_longer_uniform_by_arithmetic(self):
        """A mean of 5.7 words cannot reach a stdev of 5.0 without negatives."""
        self.assertNotIn("uniform_sentence_length",
                         analyze(HUMAN_KO)["acting_signals"])

    def test_genuinely_uniform_prose_is_still_caught(self):
        self.assertIn("uniform_sentence_length",
                      analyze(AI_EN)["acting_signals"])


class TheMachineVerdict(unittest.TestCase):
    def test_generated_prose_fails_in_both_languages(self):
        for label, text in (("ko", AI_KO), ("en", AI_EN)):
            ok, why = gate(analyze(text))
            self.assertFalse(ok, f"{label}: {why}")

    def test_human_prose_passes_in_both_languages(self):
        for label, text in (("ko", HUMAN_KO), ("en", HUMAN_EN)):
            ok, why = gate(analyze(text))
            self.assertTrue(ok, f"{label}: {why}")

    def test_one_s1_is_enough_because_that_is_what_s1_means(self):
        ok, why = gate(analyze(AI_EN))
        self.assertFalse(ok)
        self.assertIn("deterministic", why)

    def test_empty_and_trivial_text_does_not_fail(self):
        for text in ("", "   ", "짧다."):
            ok, _ = gate(analyze(text))
            self.assertTrue(ok, text)

    def test_the_ceiling_is_stated_rather_than_hidden(self):
        self.assertIsInstance(GATE_ACTING_CEILING, int)
        self.assertGreaterEqual(GATE_ACTING_CEILING, 2)


class TheAddedCategories(unittest.TestCase):
    """One focused sample per category adopted from the im-not-ai taxonomy."""

    def fires(self, code, text):
        self.assertIn(code, analyze(text)["acting_signals"])

    def test_connector_openers(self):
        self.fires("connector_openers",
                   "또한 이것은 중요하다. 따라서 우리는 검토해야 한다. "
                   "그러나 문제가 있다. 게다가 비용도 든다. 즉 다시 봐야 한다.")

    def test_nominal_forms(self):
        self.fires("nominal_forms",
                   "이것은 중요한 것이다. 저것도 필요한 것이다. 결과가 좋다는 "
                   "점이다. 우리가 확인할 바가 있다. 그런 것으로 보인다.")

    def test_intensifier_density(self):
        self.fires("intensifier_density",
                   "매우 중요한 결과다. 정말 굉장히 인상적이다. 상당히 "
                   "유의미하다. 아주 훌륭한 성과다. 무척 기대된다.")

    def test_english_gloss_rate(self):
        self.fires("english_gloss_rate",
                   "혁신(innovation)이 필요하다. 효율(efficiency)도 중요하다. "
                   "확장성(scalability)을 봐야 한다. 신뢰성(reliability)이 "
                   "관건이다.")

    def test_decoration_is_detected_but_does_not_act_alone(self):
        """S3 by design: a bulleted list is legitimate technical writing."""
        text = ("**중요**한 결과다.\n- 첫 항목\n- 둘째 항목\n- 셋째 항목\n"
                "**결론**은 명확하다. 다시 보자. 확인이 필요하다.")
        report = analyze(text)
        codes = [s["code"] for s in report["signals"]]
        self.assertIn("visual_decoration", codes)
        self.assertNotIn("visual_decoration", report["acting_signals"])


class ThroughTheRunner(unittest.TestCase):
    """The wiring. Detection that stops nothing is a description."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    class Writer(WorkerAdapter):
        name = "provider"

        def __init__(self, texts):
            self.texts = list(texts)
            self.n = 0

        def run(self, node, context):
            text = self.texts[min(self.n, len(self.texts) - 1)]
            self.n += 1
            return WorkerResult(True, payload={"summary": text,
                                               "not_established": []})

    def run_report(self, texts):
        writer = self.Writer(texts)
        runner = Runner(repo=self.tmp.name,
                        data_dir=os.path.join(self.tmp.name, "d"),
                        workers=WorkerRegistry({"provider": writer}),
                        sleep=lambda _s: None)
        node = G.TaskNode(
            node_id="report", kind="report", worker="provider",
            instruction="i",
            contract=ArtifactContract(output_schema=SCHEMAS["report"],
                                      prose_at="summary"),
            config={"provider": "claude"})
        result = runner.run(runner.start("t", G.TaskGraph([node])))
        return result, runner, writer

    def test_a_report_that_reads_as_generated_does_not_promote(self):
        result, runner, _ = self.run_report([AI_KO])
        self.assertEqual(result.state, G.FAILED)
        self.assertEqual(runner.store.artifacts(result.run_id,
                                                state="PROMOTED"), [])

    def test_the_failure_is_a_quality_failure_so_it_is_repaired(self):
        """Not CONTRACT_VIOLATION: the shape is fine, the writing is not."""
        result, runner, _ = self.run_report([AI_KO])
        attempts = runner.store.attempts(result.run_id, "report")
        self.assertEqual(attempts[-1]["failure_class"], "QUALITY_FAILURE")
        self.assertGreater(len(attempts), 1, "REPAIR gives it another attempt")

    def test_a_rewritten_report_passes_on_the_second_attempt(self):
        human = ("고쳤다. 테스트 세 개가 깨졌고 원인은 전부 같았다. "
                 "이유는 로그에 다 있었는데 아무도 안 봤다. 지금은 통과한다.")
        result, runner, writer = self.run_report([AI_KO, human])
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(writer.n, 2)

    def test_a_human_report_passes_first_time(self):
        result, _, writer = self.run_report([HUMAN_KO])
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual(writer.n, 1)

    def test_the_default_graph_puts_the_gate_on_the_report_node(self):
        """And on that node only: it is the one whose product is prose."""
        graph = default_graph("t", provider="claude")
        carrying = {node_id for node_id, node in graph.nodes.items()
                    if node.contract.prose_at}
        self.assertEqual(carrying, {"report"})
        self.assertEqual(graph.nodes["report"].contract.prose_at, "summary")

    def test_a_contract_without_prose_at_is_untouched(self):
        """The gate must not reach nodes that never claimed to write prose."""
        contract = ArtifactContract(output_schema={"type": "object"})
        self.assertEqual(contract.prose_at, "")
        from dobby.runtime.verify import Verifier

        verdict = Verifier(repo=self.tmp.name).verify(contract,
                                                      {"summary": AI_KO})
        self.assertTrue(verdict.passed)


if __name__ == "__main__":
    unittest.main()
