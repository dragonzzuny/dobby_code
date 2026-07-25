import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.skills import SkillRegistry, SkillError

META = {"name": "demo-skill", "description": "d",
        "applicable_when": ["demo"], "not_applicable_when": ["never"],
        "inputs": ["x"], "outputs": ["y"], "validation_commands": ["true"],
        "version": "0.1",
        "provenance": {"source": "test", "method": "curated",
                       "date": "2026-07-12", "confidence": "verified"}}


class TestSkillLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = SkillRegistry(os.path.join(self.tmp.name, "skills.json"))
        self.r.register_candidate(dict(META), proposed_by="agent-A")

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_required(self):
        with self.assertRaises(SkillError):
            self.r.register_candidate({"name": "x"}, "a")

    def test_illegal_transition(self):
        with self.assertRaises(SkillError):
            self.r.transition("demo-skill", "active", by="agent-B")

    def test_evidence_floor_blocks_promotion(self):
        self.r.transition("demo-skill", "sandboxed", by="agent-A")
        with self.assertRaises(SkillError):  # 0 eval passes, needs 1
            self.r.transition("demo-skill", "evaluated", by="agent-A")

    def test_self_approval_blocked(self):
        self.r.transition("demo-skill", "sandboxed", by="agent-A")
        self.r.record_eval_pass("demo-skill", "S1", "reports/e1.md")
        self.r.record_eval_pass("demo-skill", "S2", "reports/e2.md")
        self.r.transition("demo-skill", "evaluated", by="agent-A")
        with self.assertRaises(SkillError):
            self.r.transition("demo-skill", "approved", by="agent-A")
        s = self.r.transition("demo-skill", "approved", by="reviewer-B")
        self.assertEqual(s["approved_by"], "reviewer-B")

    def test_single_scenario_not_enough_for_approved(self):
        self.r.transition("demo-skill", "sandboxed", by="agent-A")
        self.r.record_eval_pass("demo-skill", "S1", "reports/e1.md")
        self.r.record_eval_pass("demo-skill", "S1", "reports/e1b.md")  # same scenario
        self.r.transition("demo-skill", "evaluated", by="agent-A")
        with self.assertRaises(SkillError):  # needs 2 DISTINCT scenarios
            self.r.transition("demo-skill", "approved", by="reviewer-B")

    def test_progressive_disclosure_levels(self):
        real = SkillRegistry(os.path.join(REPO, ".dobby", "registry",
                                          "skills.json"))
        idx = real.index()
        self.assertTrue(all(set(e) == {"name", "description"} for e in idx))
        sig = real.signature("ledgered-task")
        self.assertIn("applicable_when", sig)
        self.assertTrue(real.body_path("ledgered-task").endswith("SKILL.md"))


if __name__ == "__main__":
    unittest.main()
