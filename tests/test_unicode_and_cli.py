"""Regression tests for the two defects that a green suite did not catch.

1. The tokenizer was ASCII-only, so every metric built on it silently returned
   "identical" for any non-Latin text. 529 tests passed while three unrelated
   Korean sentences reported as a collapsed panel worth 1.0 opinions.
2. The CLI surface was almost unexercised — the unit tests covered the modules,
   nothing ran the commands.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import child_env
from dobby.prompt import clarifying_question, compile_prompt, find_gaps
from dobby.swarm.diversity import (analyze, effective_n, jaccard_distance,
                                   mean_pairwise_distance, token_set, tokens)
from dobby.swarm.diversity import _split_scripts

KO_A = "라우터는 정책 심각도로부터 에이전시 레벨을 할당한다"
KO_B = "압축은 모든 파일 경로와 수치 임계값을 보존해야 한다"
KO_C = "워크트리 격리는 두 에이전트가 트리를 손상시키는 것을 막는다"


class TestNonLatinTokenization(unittest.TestCase):
    """The bug: `[a-z0-9_]+` matches no Hangul, so everything scored identical."""

    def test_korean_produces_tokens_at_all(self):
        self.assertTrue(tokens(KO_A), "Hangul must tokenize to something")

    def test_unrelated_korean_sentences_are_not_collapsed(self):
        report = analyze([KO_A, KO_B, KO_C])
        self.assertNotEqual(report.verdict, "collapsed")
        self.assertGreater(report.mean_pairwise_distance, 0.5)
        self.assertGreater(report.effective_n, 2.0)

    def test_identical_korean_still_collapses(self):
        report = analyze([KO_A, KO_A, KO_A])
        self.assertEqual(report.verdict, "collapsed")
        self.assertEqual(report.effective_n, 1.0)

    def test_japanese_and_chinese_tokenize(self):
        self.assertTrue(tokens("圧縮はファイルパスを保存しなければならない"))
        self.assertTrue(tokens("压缩必须保留每个文件路径"))
        self.assertGreater(
            mean_pairwise_distance(["圧縮はファイルパスを保存する",
                                    "ルーターはエージェンシーレベルを割り当てる"]),
            0.5)

    def test_cyrillic_tokenizes(self):
        self.assertTrue(tokens("маршрутизатор назначает уровень агентности"))

    def test_english_behaviour_unchanged(self):
        a = "the router assigns an agency level from policy severity"
        b = "compression must preserve every file path and numeric threshold"
        self.assertEqual(mean_pairwise_distance([a, a]), 0.0)
        self.assertGreater(mean_pairwise_distance([a, b]), 0.9)

    def test_english_stopwords_still_removed(self):
        self.assertNotIn("the", tokens("the thing and that one"))

    def test_two_char_korean_content_words_survive(self):
        """예산/버그 are full content words; the Latin min-length would drop them."""
        for word in ("예산", "버그", "압축", "경로"):
            self.assertIn(word, tokens(f"{word} 문제"))


class TestScriptSplitting(unittest.TestCase):
    """Mixed tokens must not have their identifier half shredded into bigrams."""

    def test_identifier_with_korean_particle_splits(self):
        self.assertEqual(_split_scripts("build_snapshot에서"),
                         ["build_snapshot", "에서"])

    def test_pure_tokens_pass_through(self):
        self.assertEqual(_split_scripts("build_snapshot"), ["build_snapshot"])
        self.assertEqual(_split_scripts("예산초과"), ["예산초과"])

    def test_empty(self):
        self.assertEqual(_split_scripts(""), [])

    def test_identifier_survives_whole_in_tokens(self):
        out = tokens("dobby/tokens.py의 build_snapshot에서 예산 초과 버그")
        self.assertIn("build_snapshot", out)
        self.assertIn("예산", out)
        # The shredded fragments the naive fix produced must be gone.
        for junk in ("bu", "ui", "il", "ld", "d_", "_s"):
            self.assertNotIn(junk, out, f"identifier was shredded into {junk!r}")

    def test_unrelated_identifiers_do_not_look_related(self):
        a = token_set("build_snapshot에서 버그")
        b = token_set("resolve_command에서 오류")
        self.assertLess(1 - jaccard_distance(a, b), 0.4)

    def test_same_identifier_different_particles_matches(self):
        a = token_set("build_snapshot이 실패했다")
        b = token_set("build_snapshot을 수정했다")
        self.assertGreater(1 - jaccard_distance(a, b), 0.2)

    def test_korean_noun_survives_its_particles(self):
        """파일은 / 파일이 must recognise each other via the shared stem."""
        self.assertIn("파일", tokens("파일은"))
        self.assertIn("파일", tokens("파일이"))


class TestDownstreamMetricsOnKorean(unittest.TestCase):
    """Every module that imports the tokenizer inherited the bug."""

    def test_grounding_specificity_sees_korean(self):
        from dobby.swarm.grounding import Evidence, Idea, assess
        corpus = [Evidence(id="kg:x", summary="압축 누수 감사", verified=True)]
        idea = Idea(
            title="빔을 분기 계수로 제한",
            body="tiers.py 에서 워커 빔을 고정값 대신 부모의 자식 수에서 "
                 "유도하고 최대 4로 제한하여 넓은 포레스트는 두 갈래를 유지한다",
            evidence_ids=("kg:x",),
            falsifiable_test="run 20 queries and compare hops to the baseline")
        result = assess(idea, corpus)
        self.assertGreater(result.specificity, 0.0,
                           "Korean body must produce a nonzero specificity")

    def test_memory_routing_sees_korean(self):
        from dobby.memory import HierarchicalMemory, MemoryItem
        with tempfile.TemporaryDirectory() as tmp:
            mem = HierarchicalMemory(tmp)
            mem.write_tier("leaf", [
                MemoryItem(id="l1", tier="leaf", title="압축 누수 감사",
                           body="압축이 파일 경로를 잃으면 거부한다"),
                MemoryItem(id="l2", tier="leaf", title="워크트리 격리",
                           body="두 에이전트가 같은 트리에 쓰면 손상된다"),
            ])
            out = mem.route("압축 누수")
            self.assertTrue(out["items"])
            self.assertEqual(out["items"][0]["id"], "l1")

    def test_case_retrieval_sees_korean(self):
        from dobby.search import Case, retrieve_cases
        bank = [Case(id="c1", task="고객 이탈을 표 형식 데이터로 예측",
                     approach="고객 아이디로 그룹 분할한 그래디언트 부스팅",
                     outcome_score=0.8, succeeded=True)]
        out = retrieve_cases(bank, "고객 이탈 예측")
        self.assertTrue(out["reuse"])


class TestPromptCompiler(unittest.TestCase):
    def test_casual_korean_request_finds_gaps(self):
        gaps = find_gaps("이거 좀 개선해줘")
        self.assertTrue(gaps)
        self.assertIn("context", {g.slot for g in gaps})

    def test_dangling_referent_is_the_top_question(self):
        out = clarifying_question("이거 좀 개선해줘")
        self.assertTrue(out["needed"])
        self.assertIn("이거", out["question"])
        self.assertEqual(out["rounds_at_risk"], 3)

    def test_english_dangling_referent(self):
        gaps = find_gaps("can you fix this")
        self.assertIn("context", {g.slot for g in gaps})

    def test_specified_request_needs_no_question(self):
        request = ("fix the budget overflow in build_snapshot in "
                   "dobby/tokens.py; done when the budget test passes; "
                   "do not touch other files")
        self.assertFalse(clarifying_question(request)["needed"],
                         find_gaps(request))

    def test_unbounded_verb_without_acceptance_is_an_objective_gap(self):
        gaps = find_gaps("optimize the retrieval layer in dobby/core/kg.py")
        self.assertIn("objective", {g.slot for g in gaps})

    def test_gaps_ordered_by_retry_cost(self):
        gaps = find_gaps("이거 좀 개선해줘")
        costs = [g.retry_cost for g in gaps]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_one_gap_per_slot(self):
        gaps = find_gaps("개선해줘")
        self.assertEqual(len({g.slot for g in gaps}), len(gaps))

    def test_compiled_prompt_names_gaps_and_forbids_guessing(self):
        compiled = compile_prompt("이거 좀 개선해줘")
        self.assertIn("UNSPECIFIED", compiled.prompt)
        self.assertIn("do NOT guess", compiled.prompt)
        self.assertIn("STOP and ask", compiled.prompt)

    def test_compiled_prompt_keeps_the_request_verbatim(self):
        compiled = compile_prompt("이거 좀 개선해줘")
        self.assertIn("이거 좀 개선해줘", compiled.prompt)

    def test_cost_states_the_trade_rather_than_claiming_brevity(self):
        cost = compile_prompt("이거 좀 개선해줘").cost()
        self.assertGreater(cost["delta_tokens"], 0)
        self.assertIn("intended trade", cost["note"])

    def test_supplied_slots_are_used_verbatim(self):
        compiled = compile_prompt(
            "fix it", objective="the budget test passes",
            acceptance="python -m unittest -k budget",
            scope="dobby/tokens.py only")
        self.assertIn("the budget test passes", compiled.prompt)
        self.assertIn("python -m unittest -k budget", compiled.prompt)

    def test_context_known_suppresses_a_resolved_gap(self):
        with_context = find_gaps("이거 좀 고쳐줘",
                                 context_known=["이것은 dobby/tokens.py를 뜻한다"])
        without = find_gaps("이거 좀 고쳐줘")
        self.assertLess(len(with_context), len(without))


CLI_SMOKE = [
    ["doctor"],
    ["route", "add a feature"],
    ["context", "add a feature"],
    ["fleet"],
    ["memory", "stats"],
    ["memory", "integrity"],
    ["memory", "route", "--query", "compression"],
    ["specialize"],
    ["research", "plan", "hierarchical memory"],
    ["design"],
    ["review", "--reviewers", "3"],
    ["tokens", "policy"],
    ["pipeline", "--budget", "8", "--kind", "verifiable"],
    ["sandbox", "sweep"],
    ["spend"],
    ["spend", "--line"],
    ["handoff-latest"],
    ["friction-report"],
    ["panel", "test task", "--size", "3", "--dry-run"],
    # `panel` accepted no timeout, so the only bound on a round was the
    # catalog's 900s per-provider default. A round ends when its slowest member
    # does, so one stalled provider held the panel for fifteen minutes with
    # nothing the caller could do. `fleet --probe` had the flag all along.
    ["panel", "test task", "--size", "2", "--timeout", "45", "--dry-run"],
    # `graph` needs no provider: it reads source with the stdlib parser.
    ["graph"],
    ["graph", "--changed", "dobby/core/security.py"],
]


class TestCliSurface(unittest.TestCase):
    """Every subcommand must at least run. 529 unit tests did not prove this."""

    def test_every_command_exits_zero(self):
        failures = []
        for argv in CLI_SMOKE:
            proc = subprocess.run(
                [sys.executable, "-m", "dobby.cli", *argv],
                cwd=REPO, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=child_env(), timeout=180)
            if proc.returncode != 0:
                failures.append(
                    f"{' '.join(argv)} -> exit {proc.returncode}: "
                    f"{(proc.stderr or '')[-200:]}")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_json_commands_emit_parseable_json(self):
        """stdout is a contract: another process consumes it without parsing prose."""
        for argv in (["doctor"], ["fleet"], ["memory", "stats"],
                     ["design"], ["review", "--reviewers", "2"],
                     ["pipeline", "--budget", "4"]):
            proc = subprocess.run(
                [sys.executable, "-m", "dobby.cli", *argv],
                cwd=REPO, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=child_env(), timeout=180)
            self.assertEqual(proc.returncode, 0, f"{argv}: {proc.stderr[-200:]}")
            try:
                json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                self.fail(f"{' '.join(argv)} did not emit JSON: {exc}")


if __name__ == "__main__":
    unittest.main()
