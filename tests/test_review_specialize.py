import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.research import (extract_claims, parse_reference, plan_queries,
                            reproducibility_report, verify_citations,
                            verify_claim)
from dobby.review import (EVOLVABILITY, FUNCTIONAL, PERSPECTIVES, Finding,
                          assign_perspectives, escape_metrics, qa_findings,
                          qc_findings, review_plan, verdict)
from dobby.specialize import (LEVELS, MasteryEvidence, SpecializationLedger,
                              dual_gate, mastery_level)


# ======================================================================
class TestPerspectiveAssignment(unittest.TestCase):
    def test_distinct_until_exhausted(self):
        a = assign_perspectives(len(PERSPECTIVES))
        self.assertEqual(len({x["perspective"] for x in a}), len(PERSPECTIVES))

    def test_risk_areas_assigned_first(self):
        """A small panel must not silently drop `security`."""
        a = assign_perspectives(2, risk_areas=["security", "reliability"])
        self.assertEqual([x["perspective"] for x in a],
                         ["security", "reliability"])
        self.assertTrue(all(x["prioritized_by_risk"] for x in a))

    def test_duplicates_flagged_when_panel_exceeds_perspectives(self):
        a = assign_perspectives(len(PERSPECTIVES) + 1)
        self.assertTrue(a[-1]["duplicate_of_earlier"])

    def test_zero_reviewers(self):
        self.assertEqual(assign_perspectives(0), [])

    def test_plan_reports_uncovered_perspectives(self):
        plan = review_plan(2, risk_areas=["security"])
        self.assertTrue(plan["perspectives_uncovered"])
        self.assertIn("UNCOVERED", plan["coverage_note"])

    def test_plan_full_coverage(self):
        plan = review_plan(len(PERSPECTIVES))
        self.assertEqual(plan["perspectives_uncovered"], [])
        self.assertIn("all", plan["coverage_note"])

    def test_unknown_risk_area_surfaced(self):
        plan = review_plan(3, risk_areas=["telepathy"])
        self.assertIn("telepathy", plan["unknown_risk_areas"])

    def test_checklist_stays_short(self):
        plan = review_plan(3)
        self.assertLessEqual(len(plan["mechanical_checklist"]), 8)


class TestFindingValidation(unittest.TestCase):
    def test_rejects_type_from_the_wrong_family(self):
        with self.assertRaises(ValueError):
            Finding(title="t", file="a.py", line=1, family=FUNCTIONAL,
                    defect_type="naming", severity="minor")

    def test_rejects_unknown_severity(self):
        with self.assertRaises(ValueError):
            Finding(title="t", file="a.py", line=1, family=FUNCTIONAL,
                    defect_type="logic", severity="apocalyptic")

    def test_rejects_unknown_escape_stage(self):
        with self.assertRaises(ValueError):
            Finding(title="t", file="a.py", line=1, family=FUNCTIONAL,
                    defect_type="logic", severity="minor",
                    would_escape_to="the_moon")


class TestPricing(unittest.TestCase):
    def test_incident_escape_outranks_production_escape(self):
        incident = Finding(title="a", file="x.py", line=1, family=FUNCTIONAL,
                           defect_type="data", severity="critical",
                           would_escape_to="incident",
                           failure_scenario="empty input drops the table")
        prod = Finding(title="b", file="y.py", line=1, family=FUNCTIONAL,
                       defect_type="logic", severity="critical",
                       would_escape_to="production",
                       failure_scenario="off-by-one on the last row")
        self.assertGreater(incident.priority_score(), prod.priority_score())

    def test_cosmetic_never_outranks_critical(self):
        cosmetic = Finding(title="c", file="x.py", line=1, family=EVOLVABILITY,
                           defect_type="naming", severity="cosmetic",
                           would_escape_to="incident")
        critical = Finding(title="d", file="y.py", line=1, family=FUNCTIONAL,
                           defect_type="logic", severity="critical",
                           would_escape_to="review",
                           failure_scenario="negative qty passes validation")
        self.assertGreater(critical.priority_score() * 20,
                           cosmetic.priority_score())

    def test_evolvability_never_blocks_merge(self):
        f = Finding(title="huge function", file="x.py", line=1,
                    family=EVOLVABILITY, defect_type="complexity",
                    severity="critical")
        self.assertFalse(f.blocks_merge())

    def test_major_functional_blocks(self):
        f = Finding(title="race", file="x.py", line=9, family=FUNCTIONAL,
                    defect_type="timing", severity="major",
                    failure_scenario="two writers, same tree, interleaved edits")
        self.assertTrue(f.blocks_merge())

    def test_minor_functional_does_not_block(self):
        f = Finding(title="edge", file="x.py", line=9, family=FUNCTIONAL,
                    defect_type="logic", severity="minor",
                    failure_scenario="empty list returns None not []")
        self.assertFalse(f.blocks_merge())


class TestActionability(unittest.TestCase):
    def test_functional_finding_needs_a_scenario(self):
        f = Finding(title="something is wrong", file="x.py", line=3,
                    family=FUNCTIONAL, defect_type="logic", severity="major")
        ok, why = f.actionable()
        self.assertFalse(ok)
        self.assertIn("cannot be reproduced", why)

    def test_evolvability_finding_needs_no_scenario(self):
        f = Finding(title="name is misleading", file="x.py", line=3,
                    family=EVOLVABILITY, defect_type="naming",
                    severity="minor")
        self.assertTrue(f.actionable()[0])

    def test_finding_without_a_file_is_unactionable(self):
        f = Finding(title="somewhere", file="", line=None,
                    family=EVOLVABILITY, defect_type="naming",
                    severity="minor")
        self.assertFalse(f.actionable()[0])


class TestQaQcSeparation(unittest.TestCase):
    def test_new_logic_without_tests_is_a_qa_gap(self):
        out = qa_findings({"new_logic": True, "new_tests": False,
                           "requirements_linked": True})
        self.assertTrue(any(c["check"] == "tests_for_new_logic" for c in out))

    def test_out_of_scope_change_detected(self):
        out = qa_findings({"requirements_linked": True,
                           "scope_files": ["a.py"],
                           "changed_files": ["a.py", "unrelated.py"]})
        gap = next(c for c in out if c["check"] == "scope_discipline")
        self.assertIn("unrelated.py", gap["detail"])

    def test_clean_process_yields_no_qa_gaps(self):
        self.assertEqual(qa_findings({"requirements_linked": True}), [])

    def test_no_checks_run_is_a_qc_failure_not_a_pass(self):
        """An unmeasured output is not a passing output."""
        out = qc_findings({})
        self.assertTrue(any(c["check"] == "checks_executed" for c in out))

    def test_failing_check_is_reported(self):
        out = qc_findings({"checks_run": [{"name": "tests", "exit_code": 1}]})
        self.assertTrue(any("tests" in c["check"] for c in out))

    def test_produced_but_unvalidated_artifacts_flagged(self):
        out = qc_findings({"checks_run": [{"name": "t", "exit_code": 0}],
                           "produced_artifacts": ["out.json"],
                           "artifacts_validated": False})
        self.assertTrue(any(c["check"] == "output_validation" for c in out))


class TestVerdict(unittest.TestCase):
    def _blocker(self):
        return Finding(title="unbounded read", file="io.py", line=42,
                       family=FUNCTIONAL, defect_type="resource",
                       severity="critical", would_escape_to="incident",
                       failure_scenario="a 2GB file exhausts memory")

    def test_blocker_requests_changes(self):
        out = verdict([self._blocker()], qc=[{"kind": "qc", "ok": True}])
        self.assertEqual(out["decision"], "REQUEST_CHANGES")
        self.assertTrue(out["blockers"])

    def test_failed_output_check_requests_changes_even_with_no_findings(self):
        out = verdict([], qc=qc_findings({}))
        self.assertEqual(out["decision"], "REQUEST_CHANGES")

    def test_process_gap_alone_approves_with_a_named_gap(self):
        out = verdict([], qa=qa_findings({"new_logic": True, "new_tests": False,
                                          "requirements_linked": True}),
                      qc=[{"kind": "qc", "ok": True}])
        self.assertEqual(out["decision"], "APPROVE_WITH_PROCESS_GAPS")

    def test_clean_review_approves(self):
        out = verdict([], qa=[], qc=[{"kind": "qc", "ok": True}])
        self.assertEqual(out["decision"], "APPROVE")

    def test_families_reported_separately(self):
        findings = [
            self._blocker(),
            Finding(title="long function", file="io.py", line=1,
                    family=EVOLVABILITY, defect_type="complexity",
                    severity="minor"),
        ]
        out = verdict(findings, qc=[{"kind": "qc", "ok": True}])
        self.assertEqual(len(out["functional"]), 1)
        self.assertEqual(len(out["evolvability"]), 1)

    def test_functional_ordered_by_priority(self):
        low = Finding(title="low", file="a.py", line=1, family=FUNCTIONAL,
                      defect_type="logic", severity="minor",
                      would_escape_to="review", failure_scenario="x -> y")
        out = verdict([low, self._blocker()], qc=[{"kind": "qc", "ok": True}])
        self.assertEqual(out["functional"][0]["title"], "unbounded read")

    def test_unactionable_findings_excluded_from_the_decision(self):
        vague = Finding(title="feels wrong", file="a.py", line=1,
                        family=FUNCTIONAL, defect_type="logic",
                        severity="critical")
        out = verdict([vague], qc=[{"kind": "qc", "ok": True}])
        self.assertTrue(out["unactionable"])
        self.assertIn("NOT counted", " ".join(out["reasons"]))

    def test_missing_plan_states_coverage_is_unknown(self):
        out = verdict([], qc=[{"kind": "qc", "ok": True}])
        self.assertIn("unknown", out["coverage"])

    def test_scope_caveat_always_present(self):
        out = verdict([], qc=[{"kind": "qc", "ok": True}])
        self.assertIn("unreviewed, not clean", out["scope_caveat"])


class TestEscapeMetrics(unittest.TestCase):
    def test_no_data_is_not_perfect_containment(self):
        out = escape_metrics(found_in_review=0, found_in_ci=0,
                             found_in_production=0)
        self.assertEqual(out["total"], 0)
        self.assertIn("indistinguishable", out["note"])

    def test_escape_rate_and_cost_multiple(self):
        out = escape_metrics(found_in_review=8, found_in_ci=1,
                             found_in_production=1)
        self.assertAlmostEqual(out["escape_rate"], 0.2, places=4)
        self.assertGreater(out["cost_multiple"], 1.0)

    def test_perfect_containment_costs_the_baseline(self):
        out = escape_metrics(found_in_review=10, found_in_ci=0,
                             found_in_production=0)
        self.assertEqual(out["escape_rate"], 0.0)
        self.assertEqual(out["cost_multiple"], 1.0)


# ======================================================================
class TestMastery(unittest.TestCase):
    def test_no_gold_means_novice_however_much_is_known(self):
        """Without a measurement, no level above novice is claimable."""
        ev = MasteryEvidence(verified_domain_nodes=500, promoted_candidates=50)
        level, reason = mastery_level(ev)
        self.assertEqual(level, "novice")
        self.assertIn("no domain gold", reason)

    def test_level_never_rises_on_session_count_alone(self):
        ev = MasteryEvidence(sessions=500, domain_gold_cases=10)
        level, _ = mastery_level(ev)
        self.assertEqual(level, "oriented")

    def test_expert_requires_all_axes(self):
        ev = MasteryEvidence(verified_domain_nodes=40,
                             unverified_domain_nodes=5,
                             promoted_candidates=9, rejected_candidates=6,
                             domain_gold_cases=12, domain_gold_score=0.86)
        level, reason = mastery_level(ev)
        self.assertEqual(level, "expert")
        self.assertIn("0.86", reason)

    def test_low_verified_ratio_caps_at_oriented(self):
        ev = MasteryEvidence(verified_domain_nodes=6,
                             unverified_domain_nodes=60,
                             domain_gold_cases=10, domain_gold_score=0.9,
                             promoted_candidates=10)
        self.assertEqual(mastery_level(ev)[0], "oriented")

    def test_every_level_has_a_licence(self):
        from dobby.specialize import LEVEL_LICENCE
        for level in LEVELS:
            self.assertIn(level, LEVEL_LICENCE)

    def test_promotion_precision_reported(self):
        ev = MasteryEvidence(promoted_candidates=2, rejected_candidates=8)
        self.assertAlmostEqual(ev.promotion_precision(), 0.2, places=4)


class TestDualGate(unittest.TestCase):
    def test_rejects_any_per_case_generic_regression(self):
        """An average would hide this trade; per-case is the point."""
        state = {"applied": False}
        # Domain improves a lot; one generic case collapses.
        result = dual_gate(
            domain_fitness=lambda: 0.9 if state["applied"] else 0.5,
            generic_per_case=lambda: ({"g1": 0.9, "g2": 0.2} if state["applied"]
                                      else {"g1": 0.9, "g2": 0.8}),
            apply=lambda: state.update(applied=True),
            rollback=lambda: state.update(applied=False))
        self.assertFalse(result.accepted)
        self.assertIn("REJECTED", result.reason)
        self.assertEqual(len(result.generic_regressions), 1)
        self.assertFalse(state["applied"], "must roll back on rejection")

    def test_accepts_domain_gain_with_no_regression(self):
        state = {"applied": False}
        result = dual_gate(
            domain_fitness=lambda: 0.7 if state["applied"] else 0.5,
            generic_per_case=lambda: {"g1": 0.9, "g2": 0.8},
            apply=lambda: state.update(applied=True),
            rollback=lambda: state.update(applied=False))
        self.assertTrue(result.accepted, result.reason)
        self.assertAlmostEqual(result.domain_gain, 0.2, places=4)
        self.assertTrue(state["applied"])

    def test_rejects_insufficient_domain_gain(self):
        state = {"applied": False}
        result = dual_gate(
            domain_fitness=lambda: 0.5001 if state["applied"] else 0.5,
            generic_per_case=lambda: {"g1": 0.9},
            apply=lambda: state.update(applied=True),
            rollback=lambda: state.update(applied=False))
        self.assertFalse(result.accepted)
        self.assertIn("below the", result.reason)
        self.assertFalse(state["applied"])

    def test_case_becoming_unscorable_counts_as_regression(self):
        state = {"applied": False}
        result = dual_gate(
            domain_fitness=lambda: 0.9 if state["applied"] else 0.5,
            generic_per_case=lambda: ({"g1": 0.9} if state["applied"]
                                      else {"g1": 0.9, "g2": 0.8}),
            apply=lambda: state.update(applied=True),
            rollback=lambda: state.update(applied=False))
        self.assertFalse(result.accepted)
        self.assertTrue(any(r["after"] is None
                            for r in result.generic_regressions))

    def test_exception_in_apply_rolls_back(self):
        state = {"applied": False, "rolled_back": False}

        def boom():
            raise RuntimeError("half applied")

        result = dual_gate(
            domain_fitness=lambda: 0.5,
            generic_per_case=lambda: {"g1": 0.9},
            apply=boom,
            rollback=lambda: state.update(rolled_back=True))
        self.assertFalse(result.accepted)
        self.assertIn("rolled back", result.reason)
        self.assertTrue(state["rolled_back"])

    def test_exception_during_measurement_rolls_back(self):
        state = {"applied": False, "rolled_back": False}
        calls = {"n": 0}

        def domain():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("scorer died")
            return 0.5

        result = dual_gate(
            domain_fitness=domain,
            generic_per_case=lambda: {"g1": 0.9},
            apply=lambda: state.update(applied=True),
            rollback=lambda: state.update(rolled_back=True))
        self.assertFalse(result.accepted)
        self.assertTrue(state["rolled_back"])


class TestSpecializationLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "spec.json")

    def test_records_rejections_as_negative_memory(self):
        ledger = SpecializationLedger(self.path)
        result = dual_gate(
            domain_fitness=lambda: 0.5,
            generic_per_case=lambda: {"g1": 0.9},
            apply=lambda: None, rollback=lambda: None)
        detail = {"node": "kg:x", "keywords": ["a", "b"]}
        ledger.record("kg_keyword_add", result, detail)
        ledger.save()
        reloaded = SpecializationLedger(self.path)
        self.assertIsNotNone(
            reloaded.already_rejected("kg_keyword_add", detail),
            "a rejected change must not be re-proposed every session")

    def test_unseen_change_is_not_reported_as_rejected(self):
        ledger = SpecializationLedger(self.path)
        self.assertIsNone(ledger.already_rejected("kg_keyword_add",
                                                  {"node": "kg:new"}))

    def test_summary_derives_level_from_evidence(self):
        ledger = SpecializationLedger(self.path)
        ledger.set_evidence(MasteryEvidence(
            verified_domain_nodes=40, unverified_domain_nodes=4,
            promoted_candidates=9, rejected_candidates=5,
            domain_gold_cases=12, domain_gold_score=0.88))
        summary = ledger.summary()
        self.assertEqual(summary["level"], "expert")
        self.assertTrue(summary["licence"])
        self.assertTrue(summary["level_reason"])

    def test_fresh_ledger_defaults_to_novice(self):
        self.assertEqual(SpecializationLedger(self.path).summary()["level"],
                         "novice")


# ======================================================================
class TestQueryPlanning(unittest.TestCase):
    def test_includes_a_refutation_query(self):
        """One-shot search confirms its premise; refutation is the fix."""
        plan = plan_queries("hierarchical agent memory")
        shapes = {q["shape"] for q in plan.queries}
        self.assertIn("refutation", shapes)
        self.assertIn("limitation", shapes)

    def test_refutation_searches_the_same_topic(self):
        plan = plan_queries("hierarchical agent memory")
        refute = next(q for q in plan.queries if q["shape"] == "refutation")
        self.assertIn("hierarchical", refute["query"])

    def test_year_hint_adds_recency_shape(self):
        plan = plan_queries("agent memory", year_hint="2026")
        self.assertIn("recency", {q["shape"] for q in plan.queries})

    def test_stop_condition_is_not_satisfaction(self):
        plan = plan_queries("x y z")
        self.assertIn("refutation", plan.stop_condition)


class TestClaims(unittest.TestCase):
    def test_number_beats_hedge_in_strength(self):
        """'may reduce latency by 40%' is held to the quantified bar."""
        claims = extract_claims("Our method may reduce latency by 40% on the "
                                "benchmark suite we constructed.")
        self.assertEqual(claims[0].strength, "quantified")

    def test_absolute_claim_detected(self):
        claims = extract_claims("This approach always works and never fails "
                               "under any workload we can imagine.")
        self.assertEqual(claims[0].strength, "absolute")

    def test_quantified_claim_requires_measurement_artifacts(self):
        claims = extract_claims("The system improves throughput by 30% over "
                               "the baseline configuration in all trials.")
        needed = claims[0].required_artifacts()
        self.assertTrue(any("measurement" in n for n in needed))
        self.assertTrue(any("baseline" in n for n in needed))

    def test_headings_are_not_claims(self):
        self.assertEqual(extract_claims("Introduction\nMethod\nResults"), [])

    def test_absolute_claim_never_marked_supported_by_overlap(self):
        claims = extract_claims("The router always assigns the correct agency "
                               "level for every possible task description.")
        corpus = [{"id": "e1", "text": "the router always assigns the correct "
                                       "agency level for every task"}]
        v = verify_claim(claims[0], corpus)
        self.assertFalse(v.supported)
        self.assertIn("not established by lexical overlap", v.note)

    def test_unsupported_claim_says_unsupported_not_false(self):
        claims = extract_claims("Quantum entanglement accelerates our parser "
                               "by a considerable margin in practice.")
        v = verify_claim(claims[0], [{"id": "e1", "text": "unrelated content"}])
        self.assertEqual(v.matched_evidence, [])
        self.assertIn("not the same as false", v.note)

    def test_reproducibility_report_names_missing_artifacts(self):
        claims = extract_claims("Our method improves recall by 12% over the "
                               "strongest published baseline.")
        rep = reproducibility_report(claims, artifacts_present=[])
        self.assertEqual(rep["blocked"], 1)
        self.assertTrue(rep["rows"][0]["missing"])

    def test_reproducibility_satisfied_when_artifacts_present(self):
        claims = extract_claims("Our method improves recall by 12% over the "
                               "strongest published baseline.")
        rep = reproducibility_report(
            claims,
            artifacts_present=["the measurement script or command",
                               "the exact dataset split used",
                               "the baseline it is measured against",
                               "the number of runs and the variance"])
        self.assertEqual(rep["blocked"], 0)


class TestCitations(unittest.TestCase):
    def test_empty_corpus_is_not_a_clean_bill_of_health(self):
        out = verify_citations(["Some Paper, 2024"], [])
        self.assertIn("NOT CHECKED", out["verdict"])
        self.assertEqual(out["checked"], 0)

    def test_unresolvable_reference_flagged(self):
        out = verify_citations(
            ['"A Totally Invented Framework For Nothing", 2025'],
            [{"id": "p1", "text": "hierarchical memory for language agents",
              "year": "2026"}])
        self.assertEqual(out["results"][0]["severity"], "unresolvable")
        self.assertIn("unsupported", out["verdict"])

    def test_year_mismatch_is_metadata_not_fabrication(self):
        out = verify_citations(
            ['"Hierarchical Memory For Language Agents", 2024'],
            [{"id": "p1", "text": "hierarchical memory for language agents",
              "year": "2026"}])
        self.assertEqual(out["results"][0]["severity"], "metadata_mismatch")

    def test_exact_match(self):
        out = verify_citations(
            ['"Hierarchical Memory For Language Agents", 2026'],
            [{"id": "p1", "text": "hierarchical memory for language agents",
              "year": "2026"}])
        self.assertEqual(out["results"][0]["severity"], "exact")
        self.assertIn("all", out["verdict"])

    def test_parse_extracts_doi_and_arxiv(self):
        self.assertIn("10.1145", parse_reference(
            "Someone. Title. 2024. doi:10.1145/3800963").identifier)
        self.assertTrue(parse_reference(
            "Someone. Title. arXiv:2510.00615").identifier)




class TestQueryScriptFollowsTheNeed(unittest.TestCase):
    """English intent words were being appended to Korean subjects.

    Found by installing dobby into two Korean-language projects. Measured on a
    real need:

        need   산업단지 추락 끼임 사고 예방 제도 개선 선행사례와 현행 법령
        query  산업단지 안전규칙 how it works implementation

    Nothing errored. Every shape was present and every rationale sensible - and
    that query retrieves badly from the sources such a need actually has (국가법령
    정보센터, KOSHA, Korean academic databases), because the operative words were
    in the wrong language. Only thin results would have revealed it, much later.
    """

    KOREAN = "산업단지 추락 끼임 사고 예방 제도 개선 선행사례와 현행 법령"
    ENGLISH = "industrial complex fall and entrapment accident prevention"
    ENGLISH_WITH_KOREAN_NOUN = "KOSHA guideline for fall prevention 산업단지"

    def _by_shape(self, need):
        return {q["shape"]: q["query"] for q in plan_queries(need).queries}

    def test_a_korean_need_gets_korean_intent_words(self):
        queries = self._by_shape(self.KOREAN)
        self.assertIn("작동", queries["mechanism"])
        self.assertIn("실패", queries["refutation"])
        self.assertIn("한계", queries["limitation"])
        self.assertIn("대안", queries["alternative"])

    def test_a_korean_need_gets_no_english_intent_words(self):
        """The exact defect: Korean subject, English search intent."""
        for shape, query in self._by_shape(self.KOREAN).items():
            for english in ("how it works", "does not work", "limitations",
                            "alternative compared"):
                self.assertNotIn(english, query, f"{shape}: {query}")

    def test_an_english_need_is_unchanged(self):
        queries = self._by_shape(self.ENGLISH)
        self.assertIn("how it works", queries["mechanism"])
        self.assertIn("does not work", queries["refutation"])

    def test_one_korean_noun_does_not_flip_an_english_need(self):
        """Decided by character counts, so a cited proper noun does not swing it."""
        self.assertIn("how it works",
                      self._by_shape(self.ENGLISH_WITH_KOREAN_NOUN)["mechanism"])

    def test_the_canonical_query_never_gains_intent_words(self):
        for need in (self.KOREAN, self.ENGLISH):
            shapes = self._by_shape(need)
            for shape, query in shapes.items():
                if shape != "canonical":
                    self.assertLess(len(shapes["canonical"]), len(query), shape)

    def test_every_shape_is_present_in_both_scripts(self):
        for need in (self.KOREAN, self.ENGLISH):
            self.assertEqual(
                set(self._by_shape(need)),
                {"canonical", "mechanism", "refutation", "limitation",
                 "alternative"}, need[:24])

    def test_the_script_detector_is_explicit_about_its_default(self):
        from dobby.research import _script_of
        self.assertEqual(_script_of(self.KOREAN), "ko")
        self.assertEqual(_script_of(self.ENGLISH), "en")
        self.assertEqual(_script_of(""), "en")


if __name__ == "__main__":
    unittest.main()
