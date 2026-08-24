"""Methodology metrics: a solo arm's zero must read as SHAPE, not as failure."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "evals", "swebench"))

import method_metrics as M  # noqa: E402


def make_store(root, instance_id, arm, *, nodes, attempts, artifacts=()):
    store = os.path.join(root, f".store-{instance_id}-{arm}")
    runtime = os.path.join(store, "state", "runtime")
    os.makedirs(runtime, exist_ok=True)
    db = os.path.join(runtime, "runs.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE nodes (node_id TEXT, state TEXT)")
    conn.execute("CREATE TABLE attempts (node_id TEXT, attempt INT, "
                 "failure_class TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?)", nodes)
    conn.executemany("INSERT INTO attempts VALUES (?,?,?)", attempts)
    conn.commit()
    conn.close()
    if artifacts:
        adir = os.path.join(runtime, "run-1", "artifacts")
        os.makedirs(adir, exist_ok=True)
        for i, art in enumerate(artifacts):
            with open(os.path.join(adir, f"a{i}.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(art, fh)
    return store


def row(**providers):
    return {"record": {"providers": {
        pid: {"calls_total": n} for pid, n in providers.items()}}}


class TestSoloArm(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        self.dir.cleanup()

    def test_a_solo_arm_reports_absence_not_zero(self):
        got = M.for_arm(self.dir.name, "x__y-1", "A_claude", row(claude=1))
        self.assertFalse(got["has_loop_record"])
        self.assertTrue(got["single_call"])
        for key in ("rework_ratio", "contract_violation_rate",
                    "effect_observation_rate", "evidence_density"):
            self.assertIsNone(got[key],
                              f"{key} must be None for an arm that has no "
                              f"nodes to compute it over; 0.0 would sort it "
                              f"beside an arm that tried and failed")

    def test_providers_used_comes_from_the_record(self):
        got = M.for_arm(self.dir.name, "x__y-1", "D", row(claude=2, codex=3))
        self.assertEqual(got["providers_used"], ["claude", "codex"])
        self.assertEqual(got["provider_count"], 2)
        self.assertEqual(got["total_calls"], 5)
        self.assertFalse(got["single_call"])

    def test_a_provider_with_no_calls_is_not_counted_as_used(self):
        got = M.for_arm(self.dir.name, "x__y-1", "D", row(claude=1, gemini=0))
        self.assertEqual(got["providers_used"], ["claude"])


class TestLoopArm(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        make_store(
            self.dir.name, "x__y-1", "D_dobby",
            nodes=[("scout-1", "SUCCEEDED"), ("implement-2", "FAILED"),
                   ("critic-3", "SKIPPED")],
            attempts=[("scout-1", 1, "CONTRACT_VIOLATION"),
                      ("scout-1", 2, None),
                      ("implement-2", 1, "EFFECT_NOT_OBSERVED"),
                      ("implement-2", 2, None)],
            artifacts=[
                {"state": "REJECTED", "payload": {"nope": 1}},
                {"state": "PROMOTED", "payload": {"claims": [
                    {"claim": "a", "evidence": ["f.py:1", "f.py:2"]},
                    {"claim": "b", "evidence": ["g.py:9"]}]}},
            ])
        self.got = M.for_arm(self.dir.name, "x__y-1", "D_dobby",
                             row(claude=2, codex=3))

    def tearDown(self):
        self.dir.cleanup()

    def test_contract_violations_are_counted_over_attempts(self):
        self.assertEqual(self.got["contract_violations"], 1)
        self.assertEqual(self.got["contract_violation_rate"], 0.25)

    def test_effect_observation_counts_only_writing_attempts(self):
        self.assertEqual(self.got["writing_attempts"], 2)
        self.assertEqual(self.got["effect_observation_rate"], 0.5)

    def test_investigation_share_is_the_non_writing_attempts(self):
        self.assertEqual(self.got["investigation_share"], 0.5)

    def test_only_promoted_artifacts_contribute_evidence(self):
        """A rejected artifact's claims were never accepted as claims."""
        self.assertEqual(self.got["promoted_artifacts"], 1)
        self.assertEqual(self.got["claims"], 2)
        self.assertEqual(self.got["evidence_citations"], 3)
        self.assertEqual(self.got["evidence_density"], 1.5)

    def test_rework_ratio_is_attempts_over_nodes(self):
        self.assertEqual(self.got["rework_ratio"], round(4 / 3, 3))

    def test_node_states_are_carried_through(self):
        self.assertEqual(self.got["node_states"]["implement-2"], "FAILED")


if __name__ == "__main__":
    unittest.main()
