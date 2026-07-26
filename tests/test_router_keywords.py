"""Router classification: what counts as producing, and what does not.

Two defects, found by asking the router about the tasks a user would actually
be most afraid of getting wrong.

1. `PRODUCING_KW` contained not one destructive or irreversible verb. No
   `delete`, `deploy`, `publish`, `release`, `migrate`, `install`, `remove`.
   `deploy to production and notify the team` was therefore classified as
   NON-producing and routed to level 2 — the lowest agency rung — while
   AGENTS.md invariant 9 requires escalation before exactly that action.

2. Matching was by substring, for the fourth time in this codebase (after
   "proves" in "improves", "kfold" in "groupkfold", and the PowerShell aliases
   in the command guard). Measured false positives:

       the prefix is wrong           -> producing, via "fix"
       inspect the fixture files     -> producing, via "fix"
       read the underwriter report   -> producing, via "write"
       how many packages             -> producing, via "package"

   The last is the worst: an investigative question routed as a producing task,
   to a larger model and a higher rung.

Korean must stay substring-matched. It is agglutinative — `삭제` appears inside
`삭제하라` and `삭제하고` — so a word boundary would never fire.
"""

import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.kg import KnowledgeGraph, Ontology
from dobby.core.policies import PolicyBook
from dobby.core.router import (INVESTIGATE_KW, PRODUCING_KW, Router,
                               _mentions)
from dobby.core.skills import SkillRegistry

#: Tasks that must NOT be classified as producing. Every one was, before.
NOT_PRODUCING = [
    "the prefix is wrong",
    "check the suffix",
    "inspect the fixture files",
    "read the underwriter report",
    "how many packages are there",
    "what is 2+2",
    "read the config",
]

#: Verbs whose omission routed an irreversible action to the lowest rung.
MUST_BE_PRODUCING = [
    "deploy to production",
    "delete the migrations",
    "remove the old table",
    "publish the release",
    "upload the artifact",
    "install the dependency",
    "migrate the schema",
    "drop the index",
    "rollback the deployment",
    "프로덕션에 배포하라",
    "마이그레이션을 삭제하라",
    "패키지를 설치하고 구현하라",
]


def build_router() -> Router:
    onto = Ontology.load(os.path.join(REPO, ".dobby", "ontology.json"))
    with open(os.path.join(REPO, ".dobby", "knowledge", "kg.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    kg = KnowledgeGraph(onto, raw["nodes"], raw["edges"])
    with open(os.path.join(REPO, ".dobby", "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return Router(
        PolicyBook(os.path.join(REPO, ".dobby", "policies", "policies.json")),
        SkillRegistry(os.path.join(REPO, ".dobby", "registry", "skills.json")),
        kg, cfg)


class TestWholeWordMatching(unittest.TestCase):
    def test_no_substring_false_positives(self):
        offenders = [(t, _mentions(t.lower(), PRODUCING_KW))
                     for t in NOT_PRODUCING
                     if _mentions(t.lower(), PRODUCING_KW)]
        self.assertEqual(offenders, [],
                         f"substring matches classified these as producing: "
                         f"{offenders}")

    def test_fix_does_not_match_prefix_suffix_or_fixture(self):
        for word in ("prefix", "suffix", "fixture", "affix"):
            self.assertEqual(_mentions(f"the {word} is here", PRODUCING_KW), [])

    def test_write_does_not_match_underwriter(self):
        self.assertEqual(_mentions("the underwriter said no", PRODUCING_KW), [])

    def test_hyphenated_words_are_not_split_into_keywords(self):
        self.assertEqual(_mentions("re-fix-ing is not a word", PRODUCING_KW), [])

    def test_real_verbs_still_match(self):
        self.assertIn("fix", _mentions("fix the parser", PRODUCING_KW))
        self.assertIn("write", _mentions("write the report", PRODUCING_KW))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(_mentions("DEPLOY TO PRODUCTION", PRODUCING_KW))


class TestKoreanStaysSubstring(unittest.TestCase):
    """Agglutination means a word boundary would never fire."""

    def test_korean_verb_stems_match_inside_inflections(self):
        for text, stem in (("삭제하라", "삭제"), ("삭제하고", "삭제"),
                           ("배포했다", "배포"), ("설치하려면", "설치")):
            self.assertIn(stem, _mentions(text, PRODUCING_KW),
                          f"{stem!r} not found in {text!r}")

    def test_korean_investigative_stems(self):
        self.assertTrue(_mentions("몇 개인지 확인하라", INVESTIGATE_KW))


class TestHighConsequenceVerbs(unittest.TestCase):
    def test_every_destructive_and_irreversible_verb_is_known(self):
        missing = [t for t in MUST_BE_PRODUCING
                   if not _mentions(t.lower(), PRODUCING_KW)]
        self.assertEqual(missing, [],
                         f"these change or publish something and were not "
                         f"classified as producing: {missing}")

    def test_the_keyword_list_covers_all_three_consequence_classes(self):
        for verb in ("delete", "remove", "drop"):
            self.assertIn(verb, PRODUCING_KW, "destructive verbs")
        for verb in ("deploy", "publish", "release", "upload"):
            self.assertIn(verb, PRODUCING_KW, "irreversible/outward verbs")
        for verb in ("create", "write", "implement"):
            self.assertIn(verb, PRODUCING_KW, "authoring verbs")


class TestRoutingOutcomes(unittest.TestCase):
    """The classification only matters through the level it produces."""

    @classmethod
    def setUpClass(cls):
        cls.router = build_router()

    def test_deploy_no_longer_routes_to_the_lowest_rung(self):
        plan = self.router.route("deploy to production and notify the team")
        self.assertGreaterEqual(plan.level, 5,
                                "an irreversible outward action must not route "
                                "to a low-agency rung")
        self.assertEqual(plan.model_tier, "large")

    def test_korean_deploy_and_delete_routes_high(self):
        plan = self.router.route("프로덕션에 배포하고 마이그레이션을 삭제하라")
        self.assertGreaterEqual(plan.level, 5)

    def test_publish_routes_high(self):
        self.assertGreaterEqual(self.router.route("publish the release").level, 5)

    def test_read_only_task_stays_low(self):
        plan = self.router.route("read the config")
        self.assertLessEqual(plan.level, 2)
        self.assertEqual(plan.model_tier, "small")

    def test_prefix_typo_task_is_not_escalated(self):
        plan = self.router.route("the prefix is wrong")
        self.assertLessEqual(plan.level, 2)

    def test_investigative_question_is_not_producing(self):
        """`how many packages` was routed as a producing task via 'package'."""
        plan = self.router.route("how many packages are there")
        self.assertIn("investigative", " ".join(plan.justification))

    def test_every_plan_records_its_justification(self):
        for task in ("read the config", "deploy to production",
                     "how many tests are there"):
            self.assertTrue(self.router.route(task).justification,
                            f"no recorded reason for routing {task!r}")

    def test_degenerate_inputs_do_not_crash(self):
        for task in ("", "   ", "x", "\x00", "🔥" * 50, "delete " * 5000):
            plan = self.router.route(task)
            self.assertIn(plan.model_tier, ("small", "medium", "large"))
            self.assertGreaterEqual(plan.level, 1)

    def test_a_huge_repeated_destructive_task_is_still_producing(self):
        """`delete `*30000 routed to level 2 while one `delete` routed to 5."""
        plan = self.router.route("delete " * 5000)
        self.assertGreaterEqual(plan.level, 3)


if __name__ == "__main__":
    unittest.main()
