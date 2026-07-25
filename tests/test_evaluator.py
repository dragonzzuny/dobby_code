import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.evaluator import Evaluator


class TestEvaluator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.crit = os.path.join(self.tmp.name, "criteria.json")
        with open(self.crit, "w", encoding="utf-8") as f:
            json.dump({"criteria": [
                {"id": "true-cmd", "description": "d", "kind": "command",
                 "command": "true", "expect_exit": 0, "severity": "high"},
                {"id": "file-there", "description": "d", "kind": "path_exists",
                 "path": "present.txt", "severity": "high"},
                {"id": "judge", "description": "d", "kind": "model_judgment",
                 "severity": "low"},
            ]}, f)
        open(os.path.join(self.tmp.name, "present.txt"), "w").close()
        self.ev = Evaluator(self.crit, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pass_verdict_and_stub_flagged(self):
        res = self.ev.evaluate()
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["not_evaluated"], ["judge"])
        judge = next(r for r in res["records"] if r["criterion"] == "judge")
        self.assertIn("NOT RUN", judge["evidence"])

    def test_criteria_tampering_detected(self):
        with open(self.crit, "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertFalse(self.ev.verify_criteria_integrity())
        res = self.ev.evaluate()
        self.assertFalse(res["criteria_integrity"])

    def test_destructive_criterion_blocked(self):
        with open(self.crit, encoding="utf-8") as f:
            data = json.load(f)
        data["criteria"].append({"id": "evil", "description": "d",
                                 "kind": "command",
                                 "command": "rm -rf .git", "severity": "high"})
        crit2 = os.path.join(self.tmp.name, "criteria2.json")
        with open(crit2, "w", encoding="utf-8") as f:
            json.dump(data, f)
        ev2 = Evaluator(crit2, self.tmp.name)
        rec = next(r for r in ev2.evaluate()["records"]
                   if r["criterion"] == "evil")
        self.assertFalse(rec["passed"])
        self.assertIn("BLOCKED", rec["evidence"])


if __name__ == "__main__":
    unittest.main()
