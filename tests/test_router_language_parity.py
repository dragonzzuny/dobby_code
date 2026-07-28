"""Does the router treat a Korean request the way it treats the English one?

`/dobby <문장>` is only worth having if the routing behind it engages, and the
router's keyword lists are the gate everything downstream passes through. When a
producing verb is absent from a list, the request lands on "simple response task,
level 2, tier small" and no amount of care further down recovers it.

The method is MATCHED PAIRS: the same request written twice, once per language.
A difference between the two columns is the defect. A low level on BOTH is a
judgement about the ladder, not a language bug, so the pairing is what makes the
assertion meaningful — an absolute-level assertion would just encode whatever the
ladder happened to do the day it was written.

Measured before the fix these tests lock in: **7 of 12 pairs diverged**, every one
an authoring request. `논문 초안 작성` routed level 2 / small while `write the
paper draft` routed level 3 / medium, because `작성` — the most common Korean verb
for producing a document — was not in `PRODUCING_KW`. The destructive Korean
stems (`삭제`, `배포`) WERE present and fired correctly, which is why the gap was
invisible: the list looked bilingual.

The pairing then pointed the other way too. Adding the Korean stems made
`보고서 만들어줘` route producing while `make the report` did not, because `make`
and `design` were missing from the English half.

THE OPPOSITE ERROR IS ALSO TESTED. Over-escalation is not the safe direction:
routing a read-only question to level 5 with a large model is the failure the
whole-word rule in `router.py` was written about. `설계`, `수정`, `개선`, `make`,
`design` and `update` are all ordinary nouns as well as verbs, so each is checked
against sentences it must not fire on alone.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dobby.cli import _load_stack  # noqa: E402
from dobby.core.router import Router, multi_requirement_hits  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Routed(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _, kg, policies, registry, config = _load_stack(REPO)
        cls.router = Router(policies, registry, kg, config)

    def plan(self, text):
        return self.router.route(text)


class MatchedPairsRouteAlike(_Routed):
    """Same request, two languages, same rung.

    `포스터와 제안서 작성` is deliberately absent. Korean joins nouns with the
    particle `와`/`과`, and three candidate rules for detecting it were measured
    against sentences where the syllable appears inside a single word — `결과
    보여줘`, `효과 분석`, `성과 지표`, `이 학과 자료`, `나와 있는 값`. The bare
    particles produced 9 false positives out of 10, and even
    `[가-힣][와과]\\s+[가-힣]` produced 7, because `결과 보` matches it. Substring
    matching cannot separate the particle from the syllable without morphology, so
    nothing was added and the gap is recorded here rather than closed badly:
    a Korean two-deliverable request routes level 3 where the English routes 5.
    """

    PAIRS = (
        ("공모전 제안서 작성해줘", "write the contest proposal"),
        ("보고서 만들어줘", "make the report"),
        ("발표자료 제작", "produce the slide deck"),
        ("이 함수 수정해줘", "fix this function"),
        ("설계 문서 작성하고 리뷰 요청", "write the design doc and request review"),
        ("논문 초안 작성", "write the paper draft"),
        ("규정 개선안 설계", "design the regulation improvement"),
        ("테스트 코드 추가", "add tests"),
        ("이 파일 삭제", "delete this file"),
        # Loanword verbs: Korean engineering speech transliterates rather than
        # translates, and the native-stem list missed the vocabulary a developer
        # actually types. 6 of 8 such pairs diverged before these were added.
        ("이 함수 리팩터링해줘", "refactor this function"),
        ("DB 마이그레이션 해줘", "migrate the database"),
        ("브랜치 머지해줘", "merge the branch"),
        ("프로젝트 빌드해줘", "build the project"),
        ("설정 업데이트해줘", "update the config"),
        # Deliberately absent, and NOT a gap: `commit the changes` and `debug
        # this module` both route level 2 in English, so adding 커밋/디버깅 to
        # the Korean list would create a divergence rather than close one.
        ("설정값 몇 개인지 세어봐", "count how many settings there are"),
    )

    def test_no_pair_diverges_on_level_or_tier(self):
        diverged = []
        for korean, english in self.PAIRS:
            a, b = self.plan(korean), self.plan(english)
            if (a.level, a.model_tier) != (b.level, b.model_tier):
                diverged.append(
                    f"{korean!r} -> L{a.level}/{a.model_tier} but "
                    f"{english!r} -> L{b.level}/{b.model_tier}")
        self.assertEqual(diverged, [], "\n".join(diverged))


class KoreanAuthoringVerbsProduce(_Routed):
    """The stems whose absence made the harness silent for Korean authoring."""

    def test_each_authoring_verb_reaches_a_producing_rung(self):
        for text in ("논문 초안 작성", "제안서 작성해줘", "포스터 만들어줘",
                     "발표자료 제작", "코드 수정해줘", "규정 개선안 설계",
                     "이 문서 번역해줘", "핸들러 고쳐줘",
                     "이 함수 리팩터링해줘", "DB 마이그레이션 해줘",
                     "브랜치 머지해줘", "프로젝트 빌드해줘"):
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertGreaterEqual(
                    plan.level, 3,
                    f"{text!r} routed level {plan.level}: a producing request "
                    f"landed on a non-producing rung")
                self.assertNotEqual(plan.model_tier, "small")


class EnglishAuthoringVerbsProduce(_Routed):
    """Found by the pairing, not by inspection of the English list."""

    def test_each_authoring_verb_reaches_a_producing_rung(self):
        for text in ("make the report", "design the regulation improvement",
                     "draft the proposal", "modify the handler",
                     "improve the error message", "rewrite the parser",
                     "translate the docs to Korean"):
            with self.subTest(text=text):
                self.assertGreaterEqual(self.plan(text).level, 3, text)


class AmbiguousVerbsDoNotOverEscalate(_Routed):
    """A verb that is also a noun must not push a question up the ladder.

    Each sentence here contains a producing verb AND asks something. The
    producing reading loses, because `설계 검토해줘` inspects a design and
    `make sure the tests pass` verifies — and both would otherwise buy a bigger
    model and a higher agency rung for a read-only task.
    """

    READ_ONLY = (
        "make sure the tests pass",
        "check the design of this module",
        "explain the design decisions",
        "why is the update failing",
        "list the modified files",
        "count the improvements in the changelog",
        "compare the two designs",
        "개선 사항 확인해줘",
        "설계 검토해줘",
        "업데이트 실패 원인 확인",
        "수정된 파일 목록 확인",
        "요약본 있는지 찾아봐",
        "효과 분석 설명해줘",
    )

    def test_none_reaches_the_top_rung_or_the_large_tier(self):
        for text in self.READ_ONLY:
            with self.subTest(text=text):
                plan = self.plan(text)
                self.assertNotEqual(
                    plan.level, 5,
                    f"{text!r} is read-only and routed level 5")
                self.assertNotEqual(
                    plan.model_tier, "large",
                    f"{text!r} is read-only and bought the large tier")

    def test_an_ambiguous_verb_alone_still_produces(self):
        """The guard must not disarm the verb entirely."""
        for text in ("설계 문서 작성", "규정 개선안 설계", "update the changelog",
                     "design the regulation improvement"):
            with self.subTest(text=text):
                self.assertGreaterEqual(self.plan(text).level, 3, text)


class ProgressiveIsNotTwoRequirements(_Routed):
    """`-하고 있다` is one verb; `A하고 B해` is two requirements.

    `하고` is both the conjunctive ending and the first half of the progressive.
    Counting the progressive as a conjunction fired multi-requirement on
    `이 코드 사용하고 있는지 봐` — a single question — which raises the level to 5
    for a producing task and buys the large tier.
    """

    def test_the_progressive_does_not_count(self):
        for text in ("이 코드 사용하고 있는지 봐", "테스트가 동작하고 있어?",
                     "확인하고 있는 중", "지금 배포하고 있어"):
            with self.subTest(text=text):
                self.assertEqual(multi_requirement_hits(text), 0, text)

    def test_a_real_conjunction_still_counts(self):
        for text in ("테스트 실행하고 결과 보여줘", "설계 문서 작성하고 리뷰 요청",
                     "빌드하고 배포해줘"):
            with self.subTest(text=text):
                self.assertGreaterEqual(multi_requirement_hits(text), 1, text)

    def test_english_conjunctions_are_unaffected(self):
        self.assertGreaterEqual(
            multi_requirement_hits("write the doc and request review"), 1)
        self.assertEqual(multi_requirement_hits("write the doc"), 0)


if __name__ == "__main__":
    unittest.main()
