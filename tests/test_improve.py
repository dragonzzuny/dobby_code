import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.improve import ImprovementLoop, ImprovementError


def const_fitness(scores):
    """fitness factory: returns fixed scores per split, reading a mutable dict
    so tests can flip behavior after apply()."""
    def fn(split):
        return {"score": scores[split], "per_case": dict(scores.get(
            f"{split}_cases", {}))}
    return fn


class TestImprovementLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.target = os.path.join(self.data, "kg.json")
        with open(self.target, "w", encoding="utf-8") as f:
            json.dump({"nodes": [{"id": "n1", "keywords": []}],
                       "edges": []}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def _cand(self, loop):
        return loop.make_candidate(
            "kg_keyword_add",
            {"target_file": self.target, "node_id": "n1",
             "keywords": ["billing"]},
            origin_failure="test miss")

    def test_forbidden_targets_rejected(self):
        loop = ImprovementLoop(self.data, const_fitness({"dev": 0}))
        with self.assertRaises(ImprovementError):
            loop.make_candidate("kg_keyword_add",
                                {"target_file": "evals/retrieval_gold.yaml"},
                                "x")
        with self.assertRaises(ImprovementError):
            loop.make_candidate("kg_keyword_add",
                                {"target_file": ".dobby/criteria/c.json"},
                                "x")

    def test_no_gain_rejected_and_rolled_back(self):
        scores = {"dev": 0.5, "val": 0.5, "dev_cases": {}, "val_cases": {}}
        loop = ImprovementLoop(self.data, const_fitness(scores))
        before = open(self.target, encoding="utf-8").read()
        rec = loop.run_once(self._cand(loop))
        self.assertEqual(rec["decision"], "rejected")
        self.assertEqual(open(self.target, encoding="utf-8").read(), before,
                         "rollback must restore the target file exactly")

    def test_regression_rejected_even_with_dev_gain(self):
        state = {"applied": False}
        def fn(split):
            if not state["applied"]:
                return {"score": 0.5, "per_case": {"c1": 0.5, "c2": 0.5}}
            if split == "dev":
                return {"score": 0.9, "per_case": {"c1": 0.9, "c2": 0.9}}
            return {"score": 0.45, "per_case": {"c1": 0.9, "c2": 0.0}}
        loop = ImprovementLoop(self.data, fn)
        orig_apply = loop.apply
        def apply_and_flag(cand):
            snap = orig_apply(cand)
            state["applied"] = True
            return snap
        loop.apply = apply_and_flag
        rec = loop.run_once(self._cand(loop))
        self.assertEqual(rec["decision"], "rejected")
        self.assertIn("regression", rec["reason"])

    def test_gain_promoted_and_holdout_recorded(self):
        state = {"applied": False}
        def fn(split):
            base = 0.9 if state["applied"] else 0.5
            return {"score": base, "per_case": {"c1": base}}
        loop = ImprovementLoop(self.data, fn)
        orig_apply = loop.apply
        def apply_and_flag(cand):
            snap = orig_apply(cand)
            state["applied"] = True
            return snap
        loop.apply = apply_and_flag
        rec = loop.run_once(self._cand(loop))
        self.assertEqual(rec["decision"], "promoted")
        self.assertIn("holdout_after", rec)
        data = json.load(open(self.target, encoding="utf-8"))
        self.assertIn("billing", data["nodes"][0]["keywords"])

    def test_negative_memory_blocks_rediscovery(self):
        scores = {"dev": 0.5, "val": 0.5, "dev_cases": {}, "val_cases": {}}
        loop = ImprovementLoop(self.data, const_fitness(scores))
        loop.run_once(self._cand(loop))          # rejected once
        rec = loop.run_once(self._cand(loop))    # same payload again
        self.assertEqual(rec["decision"], "rejected")
        self.assertIn("previously rejected", rec["reason"])


if __name__ == "__main__":
    unittest.main()
