"""Guards for the two places a wrong number becomes a promoted change.

`RetrievalFitness` scores feed `ImprovementLoop.run_once`, which compares a
before score to an after score and promotes on the difference. Two behaviours
made that arithmetic unsafe:

- An unknown gold split returned **0.0** instead of raising. A typo in a split
  name therefore produced before=0.0, after=0.86, read as an enormous gain, and
  would promote an arbitrary change. A typo must not become a data point.
- A NaN weight scored 0.0 and an infinite weight scored **negative**. Both flow
  into the same comparison, where a garbage number is indistinguishable from a
  measurement.

`harvest` consumes packets exported by OTHER projects — external input, at the
one boundary the kit's threat model cares most about. It crashed on an empty
object, a truncated file, and a missing path, so one bad packet destroyed the
whole batch and lost every good packet with it.
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.bootstrap import scan_repo
from dobby.core.evolve import _load_packet, harvest
from dobby.core.kg import KnowledgeGraph, Ontology
from dobby.core.optimizer import RetrievalFitness, compare


def build_fitness() -> RetrievalFitness:
    onto = Ontology.load(os.path.join(REPO, ".dobby", "ontology.json"))
    with open(os.path.join(REPO, ".dobby", "knowledge", "kg.json"),
              encoding="utf-8") as f:
        raw = json.load(f)
    kg = KnowledgeGraph(onto, raw["nodes"], raw["edges"])
    with open(os.path.join(REPO, "evals", "retrieval_gold.yaml"),
              encoding="utf-8") as f:
        gold = yaml.safe_load(f)
    return RetrievalFitness(kg, gold)


class TestUnknownSplitRaises(unittest.TestCase):
    def setUp(self):
        self.fit = build_fitness()

    def test_typo_in_a_split_name_raises(self):
        for bad in ("nope", "dev2", "DEV", "", "train"):
            with self.assertRaises(KeyError, msg=f"split {bad!r} scored"):
                self.fit({}, split=bad)

    def test_the_error_names_the_available_splits(self):
        with self.assertRaises(KeyError) as ctx:
            self.fit({}, split="dev2")
        message = str(ctx.exception)
        self.assertIn("dev", message)
        self.assertIn("holdout", message)

    def test_real_splits_still_score(self):
        for split in ("dev", "val", "holdout"):
            self.assertIsInstance(self.fit({}, split=split)["score"], float)


class TestNonFiniteWeightsRefused(unittest.TestCase):
    def setUp(self):
        self.fit = build_fitness()

    def test_nan_weight_raises(self):
        with self.assertRaises(ValueError):
            self.fit({"lexical": float("nan")}, split="dev")

    def test_infinite_weight_raises(self):
        for value in (float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                self.fit({"lexical": value}, split="dev")

    def test_the_error_names_the_weight(self):
        with self.assertRaises(ValueError) as ctx:
            self.fit({"graph": float("nan")}, split="dev")
        self.assertIn("graph", str(ctx.exception))

    def test_ordinary_weights_are_untouched(self):
        self.assertGreater(self.fit({"lexical": 1.0}, split="dev")["score"], 0)

    def test_zero_and_negative_are_still_allowed(self):
        """They are legitimate points in the optimizer's search space."""
        self.fit({"lexical": 0.0}, split="dev")
        self.fit({"lexical": -1.0}, split="dev")


class TestOptimizerDeterminism(unittest.TestCase):
    def test_same_seed_gives_the_same_run(self):
        """A search whose result depends on wall-clock cannot be reviewed."""
        fit = build_fitness()
        a = compare(lambda x: fit(x, split="dev"), seeds=[7], pop_size=6, iters=4)
        b = compare(lambda x: fit(x, split="dev"), seeds=[7], pop_size=6, iters=4)
        self.assertEqual(json.dumps(a["runs"], sort_keys=True),
                         json.dumps(b["runs"], sort_keys=True))

    def test_baselines_are_reported_alongside_goa(self):
        """ADR-4: GOA is kept only because it is benchmarked honestly."""
        fit = build_fitness()
        res = compare(lambda x: fit(x, split="dev"), seeds=[0], pop_size=6,
                      iters=4)
        for method in ("goa", "random", "hillclimb"):
            self.assertIn(method, res["runs"][0])


class TestPacketValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_missing_file(self):
        packet, why = _load_packet(os.path.join(self.tmp, "nope.json"))
        self.assertIsNone(packet)
        self.assertIn("no such packet", why)

    def test_corrupt_json(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        packet, why = _load_packet(path)
        self.assertIsNone(packet)
        self.assertIn("not valid JSON", why)

    def test_not_an_object(self):
        packet, why = _load_packet(self.write("list.json", [1, 2, 3]))
        self.assertIsNone(packet)
        self.assertIn("expected an object", why)

    def test_missing_project_name(self):
        """Imported knowledge must be attributable to its source."""
        packet, why = _load_packet(self.write("noproj.json", {"promoted": []}))
        self.assertIsNone(packet)
        self.assertIn("attributable", why)

    def test_blank_project_name(self):
        packet, why = _load_packet(self.write("blank.json", {"project": "   "}))
        self.assertIsNone(packet)

    def test_valid_packet_loads(self):
        packet, why = _load_packet(self.write(
            "ok.json", {"project": "demo", "promoted": [], "rejected": []}))
        self.assertIsNotNone(packet)
        self.assertEqual(why, "")


class TestHarvestSurvivesABadBatch(unittest.TestCase):
    """One bad packet must not lose the good ones. Same rule as a fan-out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, payload):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_five_malformed_packets_do_not_lose_the_good_one(self):
        corrupt = os.path.join(self.tmp, "corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("{ not json")
        paths = [
            self.write("empty.json", {}),
            self.write("list.json", [1, 2]),
            self.write("noproj.json", {"promoted": []}),
            corrupt,
            os.path.join(self.tmp, "missing.json"),
            self.write("good.json", {"project": "other-proj", "promoted": [],
                                     "rejected": [], "weights": {}}),
        ]
        report = harvest(REPO, paths)
        self.assertIn("other-proj", report["packets"])
        self.assertEqual(len(report.get("unreadable", [])), 5)

    def test_each_rejection_carries_its_own_reason(self):
        corrupt = os.path.join(self.tmp, "c.json")
        with open(corrupt, "w", encoding="utf-8") as f:
            f.write("{{{")
        report = harvest(REPO, [corrupt])
        self.assertTrue(report["unreadable"][0]["reason"])
        self.assertIn("c.json", report["unreadable"][0]["path"])

    def test_empty_batch(self):
        report = harvest(REPO, [])
        self.assertEqual(report["packets"], [])


class TestBootstrapBoundaries(unittest.TestCase):
    def test_symlink_loops_are_not_followed(self):
        """os.walk defaults to followlinks=False; assert nobody changes that."""
        import inspect
        self.assertIs(inspect.signature(os.walk).parameters["followlinks"].default,
                      False)
        source = pathlib.Path(
            os.path.join(REPO, "dobby", "core", "bootstrap.py")
        ).read_text(encoding="utf-8")
        self.assertNotIn("followlinks=True", source)

    def test_max_files_cap_is_honoured(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for i in range(200):
            open(os.path.join(tmp, f"f{i}.py"), "w").close()
        self.assertLessEqual(scan_repo(tmp, max_files=50)["file_count"], 50)

    def test_deeply_nested_tree(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp
        for i in range(40):
            path = os.path.join(path, f"d{i}")
        os.makedirs(path, exist_ok=True)
        open(os.path.join(path, "x.py"), "w").close()
        self.assertEqual(scan_repo(tmp)["file_count"], 1)

    def test_unicode_filenames(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        written = 0
        for name in ("한글파일.py", "日本語.py", "spaces in name.py"):
            try:
                open(os.path.join(tmp, name), "w").close()
                written += 1
            except OSError:
                pass
        self.assertEqual(scan_repo(tmp)["file_count"], written)

    def test_empty_repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        inv = scan_repo(tmp)
        self.assertEqual(inv["file_count"], 0)
        self.assertEqual(inv["languages"], {})


if __name__ == "__main__":
    unittest.main()
