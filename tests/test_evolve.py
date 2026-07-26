import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.evolve import (export_experience, harvest, _retarget,
                            federation_gold_paths, make_federation_fitness)


def make_kit_copy(dst):
    """Minimal kit clone: the generic data layer only."""
    for sub in (".dobby", "evals"):
        shutil.copytree(os.path.join(REPO, sub), os.path.join(dst, sub),
                        ignore=shutil.ignore_patterns("state", "federation",
                                                      "__pycache__"))
    os.makedirs(os.path.join(dst, "reports"), exist_ok=True)


def make_instance(dst, project_dir_name):
    """A deployed instance with local promoted/rejected history."""
    host = os.path.join(dst, project_dir_name)
    kit = os.path.join(host, "agent-harness")
    make_kit_copy(kit)
    imp = os.path.join(kit, ".dobby", "state", "improvement")
    os.makedirs(imp)
    kit_kg = os.path.join(kit, ".dobby", "knowledge", "kg.json")
    with open(os.path.join(imp, "promoted.jsonl"), "w", encoding="utf-8") as f:
        # generic: targets a shipped self-KG node
        f.write(json.dumps({"decision": "promoted", "candidate": {
            "kind": "kg_keyword_add",
            "payload": {"target_file": kit_kg,
                        "node_id": "policy:P-EVIDENCE",
                        "keywords": ["measure", "actual"]},
            "origin_failure": "retrieval miss"}}) + "\n")
        # domain: targets a host-only node id
        f.write(json.dumps({"decision": "promoted", "candidate": {
            "kind": "kg_keyword_add",
            "payload": {"target_file": kit_kg,
                        "node_id": "ds:internal-billing-db",
                        "keywords": ["billing"]},
            "origin_failure": "domain miss"}}) + "\n")
    with open(os.path.join(imp, "rejected.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"kind": "kg_keyword_add",
                            "payload": {"target_file": kit_kg,
                                        "node_id": "policy:P-REPORT",
                                        "keywords": ["stuff"]},
                            "reason": "no gain"}) + "\n")
    traj = os.path.join(kit, ".dobby", "state", "trajectories")
    os.makedirs(traj)
    with open(os.path.join(traj, "t1.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "task_start", "task": "x"}) + "\n")
        f.write(json.dumps({"event": "failure", "level": "retrieval",
                            "symptom": "missed node",
                            "root_cause": "keyword gap"}) + "\n")
    return kit


class TestEvolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kit = os.path.join(self.tmp.name, "kit-template")
        make_kit_copy(self.kit)
        self.instance = make_instance(self.tmp.name, "proj-alpha")

    def tearDown(self):
        self.tmp.cleanup()

    def _stub_fitness(self, promote=True):
        state = {"n": 0}
        def fn(split):
            state["n"] += 1
            base = 0.5
            # after any apply, report gain when promote=True
            score = base + (0.1 if promote and state["n"] > 2 else 0.0)
            return {"score": score, "per_case": {"self/G-1": score}}
        return fn

    def test_export_packet_contents(self):
        p = export_experience(self.instance)
        with open(p, encoding="utf-8") as f:
            packet = json.load(f)
        self.assertEqual(packet["project"], "proj-alpha")
        self.assertEqual(len(packet["promoted"]), 2)
        self.assertEqual(len(packet["rejected"]), 1)
        self.assertEqual(packet["failures"][0]["level"], "retrieval")
        self.assertIn("dev", packet["gold"])
        self.assertIn("lexical", packet["weights"])

    def test_retarget_accepts_generic_rejects_domain(self):
        good = _retarget({"kind": "kg_keyword_add",
                          "payload": {"node_id": "policy:P-EVIDENCE",
                                      "keywords": ["measure"]}}, self.kit)
        self.assertIsNotNone(good)
        self.assertIn(self.kit, good["payload"]["target_file"])
        bad = _retarget({"kind": "kg_keyword_add",
                         "payload": {"node_id": "ds:internal-billing-db",
                                     "keywords": ["billing"]}}, self.kit)
        self.assertIsNone(bad)

    def test_harvest_promotes_generic_skips_domain_merges_negative(self):
        packet = export_experience(self.instance)
        report = harvest(self.kit, [packet], fitness=self._stub_fitness(True))
        self.assertEqual(report["skipped_domain"], 1)
        self.assertGreaterEqual(len(report["promoted"]), 1)
        self.assertEqual(report["negative_merged"], 1)
        self.assertEqual(report["lessons"], 1)
        # generic keywords actually landed in the KIT's kg.json
        with open(os.path.join(self.kit, ".dobby", "knowledge", "kg.json"),
                  encoding="utf-8") as f:
            node = next(n for n in json.load(f)["nodes"]
                        if n["id"] == "policy:P-EVIDENCE")
        self.assertIn("measure", node["keywords"])
        # project gold archived for federation regression
        self.assertTrue(any("proj-alpha" in p
                            for p in federation_gold_paths(self.kit)))

    def test_harvest_rejects_without_gain_and_rolls_back(self):
        packet = export_experience(self.instance)
        kg_path = os.path.join(self.kit, ".dobby", "knowledge", "kg.json")
        before = pathlib.Path(kg_path).read_text(encoding="utf-8")
        report = harvest(self.kit, [packet], fitness=self._stub_fitness(False))
        self.assertEqual(report["promoted"], [])
        after = pathlib.Path(kg_path).read_text(encoding="utf-8")
        self.assertEqual(before, after, "rejected harvest must not change the kit KG")

    def test_harvest_idempotent_negative_memory(self):
        packet = export_experience(self.instance)
        harvest(self.kit, [packet], fitness=self._stub_fitness(False))
        report2 = harvest(self.kit, [packet], fitness=self._stub_fitness(False))
        self.assertEqual(report2["negative_merged"], 0,
                         "same dead end must not merge twice")
        self.assertEqual(report2["lessons"], 0)

    def test_federation_fitness_skips_unknown_domain_cases(self):
        fed = os.path.join(self.kit, "evals", "federation")
        os.makedirs(fed)
        with open(os.path.join(fed, "proj-beta.yaml"), "w",
                  encoding="utf-8") as f:
            f.write("dev:\n- id: B-1\n  task: 'billing check'\n"
                    "  required_nodes: [ds:unknown-domain-node]\n")
        fit = make_federation_fitness(self.kit)
        res = fit("dev")
        self.assertFalse(any(k.startswith("proj-beta/")
                             for k in res["per_case"]),
                         "cases needing domain nodes absent from the kit KG "
                         "must be skipped, not scored as failures")


if __name__ == "__main__":
    unittest.main()
