"""Tests for the improvement flywheel and the benchmark harness.

Both are machinery for making claims, so most of what is asserted here is what
they REFUSE to claim: a comparison from too few tasks, a winner whose interval
spans zero, a golden task promoted without a human.
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import (ArtifactContract, RunStore, Runner, TaskGraph,
                           TaskNode)
from dobby.runtime import graph as G
from dobby.runtime import bench as B
from dobby.runtime import flywheel as F


def failing_node(node_id, failure_class="QUALITY_FAILURE", detail="boom"):
    return TaskNode(node_id=node_id, kind="execute", worker="static",
                    contract=ArtifactContract(output_schema=dict(type="object")),
                    config={"fail_with": failure_class, "fail_detail": detail})


class Signatures(unittest.TestCase):
    def test_paths_numbers_and_hashes_are_removed(self):
        a = F.signature(r"timeout after 120s reading C:\Users\x\a.txt (7f3ab991c2)")
        b = F.signature(r"timeout after 300s reading C:\Users\y\b.txt (aa19bb44f0)")
        self.assertEqual(a, b)

    def test_genuinely_different_failures_stay_different(self):
        self.assertNotEqual(F.signature("schema mismatch at $.steps"),
                            F.signature("timeout after 120s"))

    def test_it_is_bounded_so_one_huge_detail_cannot_dominate(self):
        self.assertLessEqual(len(F.signature("x" * 5000)), 200)


class Harvesting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.runner = Runner(self.tmp.name, data_dir=self.data,
                             sleep=lambda _s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def fail_a_run(self, detail="boom"):
        graph = TaskGraph([failing_node("a", "NON_RETRYABLE", detail)])
        self.runner.run(self.runner.start("a task that fails", graph))

    def test_one_failure_is_an_incident_and_is_not_proposed(self):
        self.fail_a_run()
        self.assertEqual(F.harvest(self.runner.store), [])

    def test_the_same_failure_twice_becomes_a_candidate(self):
        self.fail_a_run()
        self.fail_a_run()
        candidates = F.harvest(self.runner.store)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].occurrences, 2)
        self.assertEqual(candidates[0].node_kind, "execute")
        self.assertEqual(candidates[0].failure_class, "NON_RETRYABLE")
        self.assertEqual(len(candidates[0].runs), 2)

    def test_failures_that_differ_only_in_volatile_text_group_together(self):
        self.fail_a_run("timeout after 120s")
        self.fail_a_run("timeout after 300s")
        self.assertEqual(len(F.harvest(self.runner.store)), 1)

    def test_an_interrupted_attempt_is_not_a_task_failure(self):
        graph = TaskGraph([TaskNode(node_id="a", kind="execute",
                                    worker="static",
                                    contract=ArtifactContract(output_schema=dict(type="object")),
                                    config={"payload": {"ok": True}})])
        for _ in range(3):
            run_id = self.runner.start("a task", graph)
            self.runner.store.set_node_state(run_id, "a", G.READY)
            self.runner.store.lease_node(run_id, "a", holder="ghost")
            self.runner.store.start_attempt(run_id, "a", 1, worker="static")
            self.runner.run(run_id)
        self.assertEqual(F.harvest(self.runner.store), [],
                         "machine interruptions filled the task-failure pile")

    def test_a_candidate_is_a_candidate_and_says_who_promotes_it(self):
        self.fail_a_run()
        self.fail_a_run()
        row = F.harvest(self.runner.store)[0].to_dict()
        self.assertEqual(row["status"], "candidate")
        self.assertIn("a human decides", row["promote_by"])

    def test_an_empty_pile_says_which_of_the_two_it_is(self):
        """It used to say the empty pile was "either a healthy system or a
        young one -- check the run count", and the report did not carry the run
        count. The walk that wrote that note had just read every run there was,
        so the reader was sent to fetch a number the writer already had. It now
        answers, and these assertions are on the ANSWER rather than on the
        words: an empty pile from no runs and an empty pile from many runs are
        different findings and must not read the same."""
        out = F.report(self.runner.store, self.data)
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["runs_examined"], 0)
        self.assertIn("not a finding about the system", out["note"])

    def test_an_empty_pile_from_enough_runs_is_a_real_absence(self):
        self.fail_a_run("schema mismatch at $.steps")
        self.fail_a_run("timeout after 120s")
        out = F.report(self.runner.store, self.data)
        self.assertEqual(out["candidates"], [],
                         "two DIFFERENT failures are not a recurrence")
        self.assertGreaterEqual(out["runs_examined"], 2)
        self.assertIn("real absence", out["note"])

    def test_the_two_empty_piles_do_not_read_the_same(self):
        """The property the old wording was guarding, asserted directly."""
        young = F.report(self.runner.store, self.data)["note"]
        self.fail_a_run("schema mismatch at $.steps")
        self.fail_a_run("timeout after 120s")
        grown = F.report(self.runner.store, self.data)["note"]
        self.assertNotEqual(young, grown)


class PersistingCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, occurrences=2, signature="boom"):
        return F.Candidate(node_kind="execute", failure_class="NON_RETRYABLE",
                           signature=signature, occurrences=occurrences,
                           runs=["r1"], first_seen="t", last_seen="t")

    def test_it_writes_and_reads_back(self):
        path = F.write_candidates(self.data, [self.candidate()])
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(len(payload["candidates"]), 1)

    def test_a_human_decision_survives_the_next_harvest(self):
        """The decision is the valuable part; a rewrite would lose it."""
        path = F.write_candidates(self.data, [self.candidate()])
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["candidates"][0]["status"] = "rejected: the check was wrong"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

        F.write_candidates(self.data, [self.candidate(occurrences=9)])
        with open(path, encoding="utf-8") as handle:
            after = json.load(handle)["candidates"][0]
        self.assertEqual(after["status"], "rejected: the check was wrong")
        self.assertEqual(after["occurrences"], 9, "the count did not refresh")

    def test_a_new_signature_is_added_alongside(self):
        F.write_candidates(self.data, [self.candidate(signature="one")])
        F.write_candidates(self.data, [self.candidate(signature="two")])
        with open(F.golden_path(self.data), encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)["candidates"]), 2)


class BenchConditions(unittest.TestCase):
    def task(self):
        return B.Task(task_id="t", task="do the thing",
                      execute_command='{python} -c "pass"',
                      checks=['{python} -c "pass"'])

    def test_baseline_has_no_gate(self):
        graph = B.build_graph(self.task(), B.BASELINE)
        self.assertEqual(len(graph.nodes), 1)
        self.assertEqual(graph.nodes["execute"].contract.acceptance_checks, [])

    def test_gated_is_the_same_step_with_the_checks(self):
        graph = B.build_graph(self.task(), B.GATED)
        self.assertEqual(len(graph.nodes), 1)
        self.assertTrue(graph.nodes["execute"].contract.acceptance_checks)

    def test_runtime_is_the_whole_graph(self):
        graph = B.build_graph(self.task(), B.RUNTIME)
        self.assertEqual(set(graph.nodes),
                         {"plan", "execute", "verify", "report"})

    def test_the_example_corpus_is_labelled_as_a_shape(self):
        self.assertIn("SHAPE", B.example_corpus.__doc__)
        self.assertEqual(len(B.example_corpus()), 2)


class BenchStatistics(unittest.TestCase):
    def outcomes(self, n, *, better: bool):
        rows = []
        for i in range(n):
            rows.append(B.Outcome(f"t{i}", B.BASELINE, verified=False,
                                  attempts=1, wall_s=1.0, state="FAILED"))
            rows.append(B.Outcome(f"t{i}", B.RUNTIME, verified=better,
                                  attempts=2, wall_s=2.0,
                                  state="SUCCEEDED" if better else "FAILED"))
        return rows

    def test_it_refuses_a_comparison_below_the_floor(self):
        result = B.compare(self.outcomes(3, better=True), B.BASELINE, B.RUNTIME)
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertIn("below", result["why"])

    def test_it_declares_a_winner_when_the_interval_excludes_zero(self):
        result = B.compare(self.outcomes(12, better=True), B.BASELINE,
                           B.RUNTIME)
        self.assertEqual(result["verdict"], "runtime verified more")
        low, high = result["verified_rate"]["ci95"]
        self.assertGreater(low, 0.0)

    def test_no_difference_is_inconclusive_not_a_tie_declared_as_a_win(self):
        result = B.compare(self.outcomes(12, better=False), B.BASELINE,
                           B.RUNTIME)
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertIn("spans zero", result["why"])

    def test_unpaired_tasks_are_excluded_rather_than_compared(self):
        rows = self.outcomes(9, better=True)
        rows.append(B.Outcome("orphan", B.RUNTIME, True, 1, 1.0, "SUCCEEDED"))
        self.assertEqual(B.compare(rows, B.BASELINE, B.RUNTIME)["n"], 9)

    def test_the_interval_is_reproducible_from_its_seed(self):
        """Reproducibility is the contract. Two seeds landing on the same
        percentile is ordinary for a 0/1 sample and is not a defect — asserting
        they differ would be testing the random number generator."""
        deltas = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
        self.assertEqual(B.bootstrap_ci(deltas, seed=7),
                         B.bootstrap_ci(deltas, seed=7))
        low, high = B.bootstrap_ci(deltas, seed=7)
        self.assertLessEqual(low, sum(deltas) / len(deltas))
        self.assertGreaterEqual(high, sum(deltas) / len(deltas))

    def test_an_empty_sample_has_no_interval_rather_than_a_fake_one(self):
        self.assertEqual(B.bootstrap_ci([]), (0.0, 0.0))

    def test_every_comparison_carries_what_it_does_not_establish(self):
        result = B.compare(self.outcomes(12, better=True), B.BASELINE,
                           B.RUNTIME)
        self.assertIn("nothing here licenses", result["not_established"])


class BenchEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_example_corpus_runs_under_all_three_conditions(self):
        outcomes = B.run_corpus(self.tmp.name, self.data, B.example_corpus())
        self.assertEqual(len(outcomes), 6)
        self.assertEqual({o.condition for o in outcomes}, set(B.CONDITIONS))
        self.assertTrue(all(o.verified for o in outcomes),
                        [o.to_dict() for o in outcomes])

    def test_the_report_refuses_to_rank_two_tasks(self):
        outcomes = B.run_corpus(self.tmp.name, self.data, B.example_corpus())
        payload = B.report(outcomes)
        for comparison in payload["comparisons"]:
            self.assertEqual(comparison["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
