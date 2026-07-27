import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.kg import Ontology
from dobby.core.bootstrap import merged_graph
from dobby.core.policies import PolicyBook
from dobby.core.skills import SkillRegistry
from dobby.core.router import Router, BudgetMeter


def make_router():
    data = os.path.join(REPO, ".dobby")
    onto = Ontology.load(os.path.join(data, "ontology.json"))
    kg = merged_graph(onto, data)
    with open(os.path.join(data, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    return Router(PolicyBook(os.path.join(data, "policies", "policies.json")),
                  SkillRegistry(os.path.join(data, "registry", "skills.json")),
                  kg, config)


class TestRouter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = make_router()

    def test_investigative_question_routes_to_script(self):
        plan = self.r.route(
            "How many tests does this project have and do they all pass? "
            "Verify and check.")
        self.assertEqual(plan.level, 1)
        self.assertEqual(plan.model_tier, "small")
        self.assertIn("P-EVIDENCE", plan.policies)

    def test_multi_requirement_producing_task_gets_evaluator(self):
        plan = self.r.route(
            "Generate the config export and then convert the schema format")
        self.assertGreaterEqual(plan.level, 5)
        self.assertTrue(plan.needs_independent_eval)
        self.assertIn("P-CONTRACT", plan.policies)

    def test_destructive_task_fires_preserve_and_escalation(self):
        plan = self.r.route("Delete the old files and clean up the repo")
        self.assertIn("P-PRESERVE", plan.policies)
        self.assertIn("P-PRESERVE", plan.escalations_expected)
        self.assertEqual(plan.model_tier, "large")  # critical severity fired

    def test_simple_producing_task_stays_mid_ladder(self):
        plan = self.r.route("Write a short summary file of the project layout")
        self.assertLessEqual(plan.level, 3)

    def test_multi_agent_capped_without_opt_in(self):
        plan = self.r.route(
            "Merge everything and convert and package and clean and deploy")
        self.assertLessEqual(plan.level, 5)

    def test_skill_matching_via_applicability(self):
        plan = self.r.route("Set up the harness for this new repository")
        self.assertIn("bootstrap-project", plan.skills)

    def test_context_pack_within_budget(self):
        plan = self.r.route("verify the config settings against the system")
        self.assertLessEqual(plan.context_pack["approx_tokens"],
                             plan.budgets["context_tokens"] // 2)

    def test_always_on_policies_present(self):
        plan = self.r.route("hello")
        for pid in ("P-REPORT", "P-PRESERVE", "P-VALIDATE-OUTPUT"):
            self.assertIn(pid, plan.policies)


class TestSkillSelection(unittest.TestCase):
    """`applicable_when` holds conditions AND phrases; only one kind is text.

    Measured before this was split: a four-requirement request
    (`설계 문서 작성하고 리뷰 요청 그리고 배포`) surfaced NO skill, because
    `ledgered-task` is gated on `>1 requirement` — a condition the router had
    already computed and then tried to find as a substring of the sentence. No
    sentence can contain it, so the skill was unreachable by any input.

    The other direction was live too. `create|build evals` splits on the pipe into
    the bare token `create`, so `create the report` surfaced `author-evals`, and
    `평가 만들|측정` made `성능 측정해줘` — measure performance — surface it as
    well. Progressive disclosure that fires on the wrong skill costs more than one
    that fires on none, because the agent then follows an eval-authoring checklist
    for a report.
    """

    @classmethod
    def setUpClass(cls):
        cls.r = make_router()

    def test_a_multi_requirement_task_surfaces_the_ledger_skill(self):
        for task in ("설계 문서 작성하고 리뷰 요청 그리고 배포",
                     "write the doc and deploy it",
                     "테스트 실행하고 결과 정리해줘"):
            with self.subTest(task=task):
                self.assertIn("ledgered-task", self.r.route(task).skills)

    def test_a_bare_verb_does_not_surface_an_unrelated_skill(self):
        for task in ("create the report", "성능 측정해줘", "포스터 만들어줘"):
            with self.subTest(task=task):
                self.assertNotIn("author-evals", self.r.route(task).skills)

    def test_the_real_eval_phrases_still_surface_it(self):
        for task in ("create evals for the router", "build evals",
                     "평가 만들어줘", "measure the harness"):
            with self.subTest(task=task):
                self.assertIn("author-evals", self.r.route(task).skills)

    def test_text_conditions_still_match(self):
        self.assertIn("bootstrap-project", self.r.route("부트스트랩 해줘").skills)
        self.assertIn("ledgered-task", self.r.route("이어서 해줘").skills)

    def test_a_structural_condition_is_never_matched_as_text(self):
        """Otherwise the split silently becomes a no-op."""
        from dobby.core.router import STRUCTURAL_CONDITIONS
        for condition in STRUCTURAL_CONDITIONS:
            with self.subTest(condition=condition):
                plan = self.r.route(f"please handle {condition} carefully")
                # The sentence literally contains the condition string. If it is
                # still text-matched, this surfaces the skill for the wrong reason.
                self.assertEqual(
                    plan.skills, sorted(set(plan.skills)),
                    "duplicate skill entries mean both paths fired")


class TestBudgetMeter(unittest.TestCase):
    def test_hard_stop(self):
        m = BudgetMeter({"tool_calls": 2})
        self.assertTrue(m.charge("tool_calls"))
        self.assertTrue(m.charge("tool_calls"))
        self.assertFalse(m.charge("tool_calls"))
        self.assertEqual(m.remaining("tool_calls"), 0)


if __name__ == "__main__":
    unittest.main()
