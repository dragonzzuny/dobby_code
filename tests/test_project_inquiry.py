"""The research loop: decomposition, artifact acceptance, and the two cycles.

Three modules are under test and they are tested for three different properties.

`inquiry.py` is a pure function, so it is tested for the things a template can
get wrong: producing something different on the second call, wiring a dependency
onto an item that was dropped, and quietly making the implementation stage look
gradeable when nobody has said what to build.

`evidence.py` is an acceptance gate, so every kind is tested against an artifact
that a stage doing the work BADLY would produce — five sources with no locators,
an idea with no falsifiable test, a score with one run per arm. A gate that only
rejects an empty file grades effort rather than work, and each test here names
the specific bad artifact it refuses.

`refine.py` and `reattempt.py` are cycles, so they are tested for where they
STOP. A retry loop that cannot stop is worse than no retry loop, and the two
stops that matter are the ones that say the repair was not applied
(`NO_PROGRESS`) and that nothing suggested a repair at all
(`NO_REPAIR_DERIVED`).
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise, items_from_specs
from dobby.project import evidence as E
from dobby.project import inquiry as I
from dobby.project import loop as L
from dobby.project import models as M
from dobby.project import reattempt as R
from dobby.project import refine as F
from dobby.swarm.grounding import Evidence, Idea

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_CHECK = '{python} -c "import sys; sys.exit(1)"'


def literature(n, *, locators=True, disconfirming=True):
    """An artifact with `n` sources, degradable in the two ways that matter."""
    rows = []
    for i in range(n):
        row = {"title": f"Groundwater study {i}", "publisher": "Water Research"}
        if locators:
            row["url"] = f"https://doi.org/10.1000/x{i}"
        row["shape"] = ("refutation" if (disconfirming and i == 0)
                        else "canonical")
        rows.append(row)
    return {"sources": rows}


def idea_row(**over):
    row = {"title": "Cache the retrieval index",
           "body": ("Change dobby/core/kg.py to memoise the ontology lookup, "
                    "cutting the 40 repeated file reads per context call to 1"),
           "evidence_ids": ["component:kg"],
           "falsifiable_test": ("run dobby context twice and assert the second "
                                "call performs zero additional reads")}
    row.update(over)
    return row


CORPUS = [{"id": "component:kg", "summary": "knowledge graph loader",
           "path": "dobby/core/kg.py", "verified": True}]


class TheDecomposerIsATemplateAndSaysSo(unittest.TestCase):
    def test_the_same_topic_twice_is_the_same_portfolio(self):
        """A decomposer that drifts moves select.py's determinism problem up a level."""
        first = I.decompose("탄소중립 정책 효과 분석")
        second = I.decompose("탄소중립 정책 효과 분석")
        self.assertEqual(json.dumps(first, ensure_ascii=False),
                         json.dumps(second, ensure_ascii=False))

    def test_two_korean_topics_do_not_collide_on_one_directory(self):
        """An ASCII slug reduces most CJK topics to nothing; the digest is the separator."""
        a = I.slug("지하수 오염 예측")
        b = I.slug("대기질 오염 예측")
        self.assertNotEqual(a, b)

    def test_every_stage_but_implementation_is_gradeable_without_an_architect(self):
        items = items_from_specs("p", I.decompose("a topic"))
        ungradeable = [i.work_item_id for i in items if i.needs_architect]
        titles = {i.work_item_id: i.title for i in items}
        self.assertEqual(len(ungradeable), 1, titles)
        self.assertIn("구현", titles[ungradeable[0]])

    def test_the_implementation_stage_carries_its_uncertainty_even_once_gradeable(self):
        """Supplying checks answers "how is it graded", not "what should be built".

        The template must not answer the second by understating uncertainty, so
        the item stays above UNCERTAINTY_ESCALATION and the loop still routes it
        through an architect — with, now, a definition of done already attached.
        """
        items = {i.work_item_id: i for i in items_from_specs(
            "p", I.decompose("a topic", smoke_checks=("pytest -q",)))}
        implementation = items["W006"]
        self.assertEqual(implementation.acceptance_checks, ["pytest -q"])
        self.assertTrue(implementation.needs_architect)
        self.assertGreaterEqual(implementation.uncertainty,
                                M.UNCERTAINTY_ESCALATION)
        implementation.planned_by = "plan-1"
        self.assertFalse(implementation.needs_architect,
                         "an applied plan left the item still ungradeable")

    def test_without_the_projects_checks_the_implementation_stage_has_no_definition_of_done(self):
        items = {i.work_item_id: i for i in
                 items_from_specs("p", I.decompose("a topic"))}
        self.assertEqual(items["W006"].acceptance_checks, [])

    def test_a_dropped_stage_does_not_leave_a_dependency_nobody_will_satisfy(self):
        """A dangling depends_on reports NOTHING_STARTABLE for a config mistake."""
        specs = I.decompose("a topic", stages=("literature", "ideation"))
        ids = {s["work_item_id"] for s in specs}
        for spec in specs:
            for dep in spec["depends_on"]:
                self.assertIn(dep, ids, spec)

    def test_an_empty_topic_is_refused_rather_than_expanded(self):
        with self.assertRaises(ValueError):
            I.decompose("   ")

    def test_an_unknown_stage_is_named_rather_than_ignored(self):
        with self.assertRaises(ValueError):
            I.decompose("a topic", stages=("literature", "peer-review"))

    def test_the_plan_states_that_the_artifacts_do_not_exist_yet(self):
        plan = I.plan("a topic")
        self.assertIn("do not exist yet", plan["note"])
        self.assertEqual(plan["ungradeable"], ["W006"])


class TheGateRefusesTheArtifactOfAStageThatDidNotDoTheWork(unittest.TestCase):
    def test_five_sources_with_no_locator_fail_even_though_the_count_is_met(self):
        verdict = E.check_literature(literature(5, locators=False), min_rows=5)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["measured"]["with_locator"], 0)
        self.assertEqual(
            sum(1 for f in verdict["failures"] if "no resolvable locator" in f),
            5, verdict["failures"])

    def test_a_search_that_only_confirmed_is_refused(self):
        verdict = E.check_literature(literature(6, disconfirming=False),
                                     min_rows=5)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("refutation" in f for f in verdict["failures"]))

    def test_a_complete_search_passes_but_says_the_sources_are_unverified(self):
        verdict = E.check_literature(literature(5), min_rows=5)
        self.assertTrue(verdict["ok"], verdict["failures"])
        self.assertTrue(verdict["unverified"],
                        "a pass with no corpus implied the citations resolved")

    def test_a_background_claim_pointing_at_nothing_fails(self):
        doc = {"findings": [{"claim": "the loader is slow",
                             "path": "dobby/core/does_not_exist.py"}] * 3}
        verdict = E.check_background(doc, min_rows=3, root=REPO)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["measured"]["paths_resolved"], 0)

    def test_a_background_claim_pointing_at_a_real_file_resolves(self):
        doc = {"findings": [{"claim": f"claim {i}", "path": "dobby/core/kg.py"}
                            for i in range(3)]}
        verdict = E.check_background(doc, min_rows=3, root=REPO)
        self.assertTrue(verdict["ok"], verdict["failures"])
        self.assertEqual(verdict["measured"]["paths_resolved"], 3)

    def test_a_dataset_with_no_split_declared_fails(self):
        doc = {"datasets": [{"name": "wells", "source": "KIGAM",
                             "license": "CC-BY", "n_rows": 4000}]}
        verdict = E.check_dataset(doc, min_rows=1)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("split" in f for f in verdict["failures"]))

    def test_a_dataset_row_that_declares_everything_passes(self):
        doc = {"datasets": [{"name": "wells", "source": "KIGAM",
                             "license": "CC-BY", "n_rows": 4000,
                             "split": "train"}]}
        self.assertTrue(E.check_dataset(doc, min_rows=1)["ok"])

    def test_an_idea_with_no_falsifiable_test_does_not_count(self):
        doc = {"corpus": CORPUS,
               "ideas": [idea_row(falsifiable_test="")]}
        verdict = E.check_ideation(doc, min_rows=1)
        self.assertFalse(verdict["ok"])
        self.assertIn("no_falsifiable_test",
                      verdict["measured"]["rejection_histogram"])

    def test_a_failing_ideation_artifact_carries_its_repairs(self):
        """This is what reattempt.py feeds back; without it a retry has nothing to change."""
        doc = {"corpus": CORPUS, "ideas": [idea_row(falsifiable_test="")]}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "ideation.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            verdict = E.check_file(E.IDEATION, path, min_rows=1)
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["repairs"]["items"], verdict)

    def test_an_elaboration_without_a_named_target_is_not_buildable(self):
        doc = {"corpus": CORPUS,
               "ideas": [idea_row(acceptance_checks=["pytest -q"])]}
        verdict = E.check_elaboration(doc, min_rows=1)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("targets" in f for f in verdict["failures"]))

    def test_one_run_per_arm_is_not_an_evaluation(self):
        doc = {"metric": "auc", "headline_score": 0.81,
               "baseline_runs": [0.70], "candidate_runs": [0.81]}
        verdict = E.check_evaluation(doc)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("run comparison" in f for f in verdict["failures"]))

    def test_a_score_below_the_trivial_floor_is_refused(self):
        doc = {"metric": "auc", "headline_score": 0.48,
               "baseline_runs": [0.5, 0.5, 0.5],
               "candidate_runs": [0.48, 0.48, 0.48]}
        verdict = E.check_evaluation(doc)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("trivial baseline" in f
                            for f in verdict["failures"]), verdict["failures"])

    def test_a_defect_with_no_reproduction_does_not_count(self):
        doc = {"defects": [{"observed": "wrong", "expected": "right",
                            "path": "a.py"}]}
        verdict = E.check_debug(doc, min_rows=1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["measured"]["actionable"], 0)


class TheGateReportsItsOwnFailuresRatherThanRaising(unittest.TestCase):
    def test_a_missing_artifact_is_a_fail_and_not_an_exception(self):
        verdict = E.check_file(E.LITERATURE, os.path.join(REPO, "nope.json"))
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["measured"]["exists"])

    def test_unparseable_json_is_a_fail_that_names_the_parse_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = os.path.join(tmp, "lit.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            verdict = E.check_file(E.LITERATURE, path)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("not readable JSON" in f
                            for f in verdict["failures"]))

    def test_a_missing_row_key_is_not_graded_as_zero_rows(self):
        with self.assertRaises(E.ArtifactError):
            E.check_literature({"srcs": []}, min_rows=1)

    def test_a_relative_artifact_resolves_against_the_project_not_the_caller(self):
        """derive_repair grades a project elsewhere; cwd resolution reports a false absence."""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.makedirs(os.path.join(tmp, "sub"))
            with open(os.path.join(tmp, "sub", "lit.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(literature(5), fh)
            verdict = E.check_file(E.LITERATURE, "sub/lit.json", root=tmp)
        self.assertTrue(verdict["ok"], verdict["failures"])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(E.ArtifactError):
            E.check_file("peer-review", "x.json")

    def test_the_emitted_acceptance_command_is_one_this_kit_can_parse_back(self):
        """The item's definition of done and the retry's repair source must agree."""
        command = E.acceptance_command(E.LITERATURE, "a/lit.json", min_rows=7)
        parsed = R.parse_artifact_check(command)
        self.assertEqual(parsed, {"kind": "literature", "file": "a/lit.json",
                                  "min": 7})


class TheIdeaCycleStopsForAReason(unittest.TestCase):
    corpus = [Evidence(id="component:kg", summary="knowledge graph loader",
                       path="dobby/core/kg.py", verified=True)]

    #: Long enough to clear grounding's `_MIN_BODY_TOKENS` floor and specific
    #: enough to name a real module — a shorter body scores 0.0 specificity
    #: regardless of content, which would make these tests assert the wrong
    #: thing about why an idea was rejected.
    GOOD_BODY = ("Change the ontology loader in dobby/core/kg.py so the parsed "
                 "structure is cached in memory after the first read, replacing "
                 "the repeated filesystem reads performed on every context "
                 "call with a single one that costs 40 ms")

    def good_idea(self, n=0):
        return Idea(title=f"Memoise the ontology read {n}",
                    body=self.GOOD_BODY,
                    evidence_ids=("component:kg",),
                    falsifiable_test=("run dobby context twice and assert zero "
                                      "additional reads on the second"))

    def test_it_refuses_to_ideate_before_prior_art_was_retrieved(self):
        calls = []
        result = F.refine(lambda i, n: calls.append(i) or [self.good_idea()],
                          [], base_instruction="propose")
        self.assertEqual(result["stopped"], F.NO_PRIOR_ART)
        self.assertEqual(calls, [], "a provider was paid before retrieval ran")

    def test_a_first_round_that_clears_the_gate_stops_at_one_round(self):
        result = F.refine(lambda i, n: [self.good_idea()], self.corpus,
                          base_instruction="propose", min_accepted=1)
        self.assertEqual(result["stopped"], F.SATISFIED)
        self.assertEqual(len(result["rounds"]), 1)

    def test_the_repair_reaches_the_next_round_as_an_instruction(self):
        """The whole point: explore_cycle's repairs stop being advice for a human."""
        seen = []

        def generate(instruction, index):
            seen.append(instruction)
            if index == 0:
                return [Idea(title="Memoise the ontology read",
                             body=self.GOOD_BODY,
                             evidence_ids=("component:kg",),
                             falsifiable_test="")]
            return [self.good_idea()]

        result = F.refine(generate, self.corpus, base_instruction="propose",
                          rounds=3, min_accepted=1)
        self.assertEqual(result["stopped"], F.SATISFIED, result)
        self.assertEqual(len(seen), 2)
        self.assertIn("show this is WRONG", seen[1],
                      "round 2 was re-prompted without the repair")

    def test_a_generator_that_ignores_the_repair_stops_the_cycle(self):
        flat = Idea(title="Make it better", body="improve things",
                    evidence_ids=(), falsifiable_test="")
        result = F.refine(lambda i, n: [flat], self.corpus,
                          base_instruction="propose", rounds=5)
        self.assertEqual(result["stopped"], F.NO_PROGRESS, result)
        self.assertEqual(len(result["rounds"]), 2,
                         "the cycle kept paying for identical output")

    def test_a_generator_that_returns_nothing_is_its_own_diagnosis(self):
        result = F.refine(lambda i, n: [], self.corpus,
                          base_instruction="propose")
        self.assertEqual(result["stopped"], F.GENERATOR_EMPTY)

    def test_new_but_still_rejected_ideas_exhaust_the_rounds(self):
        def generate(instruction, index):
            return [Idea(title=f"Idea {index}", body=f"do thing {index}",
                         evidence_ids=(), falsifiable_test="")]

        result = F.refine(generate, self.corpus, base_instruction="propose",
                          rounds=3)
        self.assertEqual(result["stopped"], F.ROUNDS_EXHAUSTED, result)
        self.assertEqual(len(result["rounds"]), 3)

    def test_every_stop_reason_is_a_declared_one(self):
        result = F.refine(lambda i, n: [self.good_idea()], self.corpus,
                          base_instruction="propose")
        self.assertIn(result["stopped"], F.STOP_REASONS)


class TheProviderBackedGeneratorParsesRatherThanTrusts(unittest.TestCase):
    corpus = [Evidence(id="component:kg", summary="knowledge graph loader",
                       path="dobby/core/kg.py", verified=True)]

    def generator(self, result):
        from dobby.providers import run as provider_run
        with unittest.mock.patch.object(provider_run, "run_provider",
                                        return_value=result) as call:
            generate = F.provider_generator("claude", self.corpus)
            return generate("propose something", 0), call

    def test_a_provider_that_fails_yields_no_ideas_rather_than_raising(self):
        """refine already has a stop reason for an empty round; an exception loses the transcript."""
        result = unittest.mock.Mock(ok=False, text="", error="timed out")
        ideas, _ = self.generator(result)
        self.assertEqual(ideas, [])

    def test_prose_where_json_was_required_yields_no_ideas(self):
        result = unittest.mock.Mock(ok=True, text="Sure! Here are some ideas:",
                                    error="")
        ideas, _ = self.generator(result)
        self.assertEqual(ideas, [])

    def test_returned_ideas_are_attributed_to_the_provider_that_wrote_them(self):
        payload = json.dumps({"ideas": [
            {"title": "Cache it", "body": "change dobby/core/kg.py",
             "evidence_ids": ["component:kg"],
             "falsifiable_test": "assert zero re-reads"}]})
        ideas, _ = self.generator(unittest.mock.Mock(ok=True, text=payload,
                                                     error=""))
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0].author, "claude",
                         "an idea with no author cannot be checked against the "
                         "critic-is-not-the-author rule")

    def test_the_prompt_carries_the_citable_ids(self):
        payload = json.dumps({"ideas": []})
        _, call = self.generator(unittest.mock.Mock(ok=True, text=payload,
                                                    error=""))
        prompt = call.call_args[0][1]
        self.assertIn("component:kg", prompt,
                      "a model asked to cite without being shown the citable "
                      "set invents ids")

    def test_the_package_exposes_refine_as_a_module_not_a_function(self):
        """Re-exporting a function named `refine` would shadow its own module."""
        self.assertEqual(F.__name__, "dobby.project.refine")
        self.assertTrue(hasattr(F, "STOP_REASONS"))


class TheRetryOnlyHappensAfterSomethingChanged(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.artifact = os.path.join(self.root, "lit.json")
        # The acceptance check runs with cwd set to the project root, which is
        # outside this repository, so `-m dobby.cli` would not resolve. The
        # alternative — a bespoke checker script — would test a command shape
        # this kit never emits, and the emitted shape is exactly what
        # `parse_artifact_check` has to recognise.
        self.saved_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = REPO

    def tearDown(self):
        if self.saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.saved_pythonpath
        self.tmp.cleanup()

    def write_artifact(self, n):
        with open(self.artifact, "w", encoding="utf-8") as fh:
            json.dump(literature(n), fh, ensure_ascii=False)

    def artifact_check(self, min_rows=5):
        """The command `evidence.acceptance_command` emits, with `{python}` for the runner.

        Forward slashes because the string survives both `shlex.split` (which
        strips backslashes it reads as escapes) and the shell, and a check whose
        path arrives mangled would fail for a reason that has nothing to do with
        the artifact.
        """
        return E.acceptance_command(E.LITERATURE,
                                    f'"{self.artifact}"'.replace("\\", "/"),
                                    min_rows=min_rows, python="{python}")

    def init(self, items):
        report = initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                            item_specs=items, run_baseline=True)
        self.project_id = report["project_id"]

    def item(self, work_item_id="W001"):
        return ProjectStore(self.data).load_project(
            self.project_id)["portfolio"].get(work_item_id)

    def test_a_block_with_nothing_to_change_is_not_retried(self):
        self.init([{"outcome": "will not verify",
                    "acceptance_checks": [FAILING_CHECK]}])
        result = R.persevere(self.data, max_attempts=3)
        self.assertEqual(result["stopped"], R.NO_REPAIR_DERIVED, result)
        self.assertEqual(result["attempts_used"], 1,
                         "an item that gives no repair was retried anyway")
        self.assertEqual(result["repairs_applied"], 0)

    def test_a_failing_artifact_check_becomes_a_repair_written_into_the_item(self):
        self.write_artifact(2)
        self.init([{"outcome": "collect the literature",
                    "acceptance_checks": [self.artifact_check()]}])
        result = R.persevere(self.data, max_attempts=2)

        self.assertEqual(result["stopped"], R.ATTEMPTS_EXHAUSTED, result)
        self.assertEqual(result["repairs_applied"], 2)
        outcome = self.item().outcome
        self.assertIn(R.REPAIR_MARKER.strip(), outcome)
        self.assertIn("fewer than the 5", outcome,
                      "the repair did not carry the condition that failed")

    def test_the_repair_replaces_the_previous_one_rather_than_stacking(self):
        self.write_artifact(2)
        self.init([{"outcome": "collect the literature",
                    "acceptance_checks": [self.artifact_check()]}])
        R.persevere(self.data, max_attempts=3)
        outcome = self.item().outcome
        self.assertEqual(outcome.count(R.REPAIR_MARKER.strip()), 1, outcome)
        self.assertTrue(outcome.startswith("collect the literature"))

    def test_a_repaired_item_that_then_satisfies_its_check_completes(self):
        """The cycle closing end to end: fail, repair, act, pass."""
        self.write_artifact(3)
        grow = os.path.join(self.root, "grow.py")
        with open(grow, "w", encoding="utf-8") as fh:
            fh.write("import json, sys\n"
                     "p = sys.argv[1]\n"
                     "d = json.load(open(p, encoding='utf-8'))\n"
                     "n = len(d['sources'])\n"
                     "d['sources'].append({'title': f'Groundwater study {n}',"
                     " 'publisher': 'Water Research',"
                     " 'url': f'https://doi.org/10.1000/x{n}',"
                     " 'shape': 'canonical'})\n"
                     "json.dump(d, open(p, 'w', encoding='utf-8'))\n"
                     "print('added')\n")
        self.init([{"outcome": "collect the literature",
                    "acceptance_checks": [self.artifact_check()]}])

        result = R.persevere(
            self.data, max_attempts=3,
            execute_command=f'{{python}} "{grow}" "{self.artifact}"')

        self.assertEqual(self.item().state, M.DONE, result)
        self.assertEqual(result["repairs_applied"], 1,
                         "the item passed without needing exactly one repair")
        self.assertEqual(result["attempts_used"], 2)
        self.assertIn(result["stopped"], L.STOP_REASONS)

    def test_a_check_that_is_not_an_artifact_check_is_not_parsed_as_one(self):
        for command in ("pytest -q",
                        "echo project check --kind literature --file x.json",
                        "python -m dobby.cli project check --kind bogus "
                        "--file x.json",
                        "python -m dobby.cli project check --kind literature"):
            self.assertIsNone(R.parse_artifact_check(command), command)

    def test_derive_repair_returns_nothing_when_the_artifact_now_passes(self):
        self.write_artifact(5)
        self.init([{"outcome": "collect the literature",
                    "acceptance_checks": [self.artifact_check()]}])
        self.assertIsNone(R.derive_repair(self.item(), root=self.root))


if __name__ == "__main__":
    unittest.main()
