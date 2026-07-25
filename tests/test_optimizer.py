import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import yaml

from dobby.core.kg import Ontology
from dobby.core.bootstrap import merged_graph
from dobby.core.optimizer import (RetrievalFitness, vec_to_config, SPACE,
                               goa_optimize, random_search, hill_climb)


def load_fitness():
    data = os.path.join(REPO, ".dobby")
    kg = merged_graph(Ontology.load(os.path.join(data, "ontology.json")), data)
    with open(os.path.join(REPO, "evals", "retrieval_gold.yaml"),
              encoding="utf-8") as f:
        gold = yaml.safe_load(f)
    return RetrievalFitness(kg, gold)


class TestFitness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fit = load_fitness()

    def test_deterministic(self):
        x = [1.0, 0.5, 0.3, 0.1, 0.5, 1.0, 8.0]
        a = self.fit(x, "dev")
        b = self.fit(x, "dev")
        self.assertEqual(a["score"], b["score"])
        self.assertEqual(a["per_case"], b["per_case"])

    def test_score_bounded(self):
        x = [1.0, 0.5, 0.3, 0.1, 0.5, 1.0, 8.0]
        res = self.fit(x, "dev")
        self.assertLessEqual(res["score"], 1.0)
        for cid, s in res["per_case"].items():
            self.assertGreaterEqual(s, -0.2, cid)

    def test_holdout_split_isolated(self):
        dev = set(self.fit([1, .5, .3, .1, .5, 1, 8], "dev")["per_case"])
        hold = set(self.fit([1, .5, .3, .1, .5, 1, 8], "holdout")["per_case"])
        self.assertFalse(dev & hold, "dev and holdout scenarios must not overlap")


class TestOptimizers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fit = load_fitness()
        cls.f = lambda _, x: fit(x, "dev")
        cls.fit = fit

    def _check(self, res):
        cfg = res["best_config"]
        for name, lo, hi in SPACE:
            self.assertGreaterEqual(cfg[name], lo if name != "context_k" else int(lo))
            self.assertLessEqual(cfg[name], hi)
        self.assertIsInstance(cfg["context_k"], int)
        # best-so-far history must be monotone
        self.assertEqual(res["history"], sorted(res["history"]))

    def test_goa_runs_and_respects_bounds(self):
        self._check(goa_optimize(lambda x: self.fit(x, "dev"),
                                 pop_size=6, iters=5, seed=1))

    def test_random_and_hillclimb(self):
        self._check(random_search(lambda x: self.fit(x, "dev"), 30, seed=1))
        self._check(hill_climb(lambda x: self.fit(x, "dev"), 30, seed=1))

    def test_seeded_reproducibility(self):
        a = goa_optimize(lambda x: self.fit(x, "dev"), 6, 5, seed=7)
        b = goa_optimize(lambda x: self.fit(x, "dev"), 6, 5, seed=7)
        self.assertEqual(a["best_score"], b["best_score"])
        self.assertEqual(a["best_config"], b["best_config"])


if __name__ == "__main__":
    unittest.main()
