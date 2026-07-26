"""The shipped self-knowledge graph must describe the code that exists.

It had drifted, and the drift was invisible because nothing checked it:

- 9 of 40 nodes recorded paths under a `harness/` directory that no longer
  exists, and 8 were still named for the pre-rename CLI.
- Thirteen subsystems added since — providers, swarm, memory tiers, sandbox,
  review, mlops, tokens, research, design, search, spend, progress, style — had
  no node at all. `dobby context "compression leakage"` retrieved nothing about
  compression or leakage: the harness could not find itself.

Stale paths stopped being merely dead weight when `_lexical_score` began scoring
the `path` field. A wrong path is now a wrong signal.

These tests are the guard. `tools/refresh_self_kg.py` is the fix.
"""

import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# `kitonly` sits beside this file. The suite runs both as `tests.test_x`
# and as a bare script; only REPO was on sys.path, so the package form
# raised ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitonly import IS_THE_KIT, SKIP_REASON  # noqa: E402

from dobby.core.kg import KnowledgeGraph, Ontology
from pathlib import Path

KG_PATH = os.path.join(REPO, ".dobby", "knowledge", "kg.json")
ONTOLOGY_PATH = os.path.join(REPO, ".dobby", "ontology.json")

#: Subsystems a user will ask about by name. Each must be reachable.
SUBSYSTEM_QUERIES = [
    "compression leakage",
    "sandbox output capture",
    "multi-agent panel",
    "provider fleet",
    "diversity collapse",
    "ml leakage holdout",
    "code review perspective",
    "prompt ambiguity",
    "specialization domain expert",
    "token budget snapshot",
    "solution tree search",
    "claim citation verification",
]

#: The same questions in Korean. The tokenizer regression that made every
#: non-Latin query score as identical was found in this repository; retrieval
#: must be checked in both languages or the next one goes unnoticed too.
KOREAN_QUERIES = ["압축 누수", "멀티에이전트 패널", "머신러닝 누수", "코드리뷰"]


def load_raw() -> dict:
    with open(KG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_graph() -> KnowledgeGraph:
    onto = Ontology.load(ONTOLOGY_PATH)
    raw = load_raw()
    return KnowledgeGraph(onto, raw["nodes"], raw["edges"])


@unittest.skipUnless(IS_THE_KIT, SKIP_REASON)
class TestPathsAreReal(unittest.TestCase):
    def test_every_recorded_path_exists(self):
        """A path that does not resolve is a wrong retrieval signal."""
        missing = []
        for node in load_raw()["nodes"]:
            path = node.get("path")
            if path and not os.path.exists(os.path.join(REPO, path)):
                missing.append(f"{node['id']} -> {path}")
        self.assertEqual(missing, [],
                         "nodes point at files that do not exist:\n"
                         + "\n".join(missing))

    def test_no_node_still_names_the_old_cli(self):
        stale = [n["id"] for n in load_raw()["nodes"]
                 if n.get("name", "").startswith("harness ")]
        self.assertEqual(stale, [], f"nodes named for the pre-rename CLI: {stale}")

    def test_no_path_points_into_the_old_package(self):
        offenders = [f"{n['id']} -> {n['path']}" for n in load_raw()["nodes"]
                     if (n.get("path") or "").startswith("harness/")]
        self.assertEqual(offenders, [])


@unittest.skipUnless(IS_THE_KIT, SKIP_REASON)
class TestGraphIsValid(unittest.TestCase):
    def test_loads_through_the_ontology(self):
        graph = load_graph()
        self.assertGreater(len(graph.nodes), 40)

    def test_every_edge_connects_known_nodes(self):
        raw = load_raw()
        ids = {n["id"] for n in raw["nodes"]}
        dangling = [f"{e['src']}-{e['rel']}->{e['dst']}" for e in raw["edges"]
                    if e["src"] not in ids or e["dst"] not in ids]
        self.assertEqual(dangling, [])

    def test_no_duplicate_node_ids(self):
        ids = [n["id"] for n in load_raw()["nodes"]]
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dupes, set())

    def test_model_assertions_are_never_verified(self):
        """The kit's core provenance rule, enforced on its own data."""
        offenders = []
        for node in load_raw()["nodes"]:
            prov = node.get("provenance", {})
            if (prov.get("method") == "model_assertion"
                    and prov.get("confidence") == "verified"):
                offenders.append(node["id"])
        self.assertEqual(offenders, [])


@unittest.skipUnless(IS_THE_KIT, SKIP_REASON)
class TestTheHarnessCanFindItself(unittest.TestCase):
    """Before the refresh every one of these returned zero items."""

    def setUp(self):
        self.graph = load_graph()

    def test_every_subsystem_query_retrieves_something(self):
        empty = []
        for query in SUBSYSTEM_QUERIES:
            pack = self.graph.context_pack(query)
            if not pack["items"]:
                empty.append(query)
        self.assertEqual(empty, [],
                         f"queries that retrieve nothing: {empty}")

    def test_korean_queries_retrieve_something(self):
        empty = [q for q in KOREAN_QUERIES
                 if not self.graph.context_pack(q)["items"]]
        self.assertEqual(empty, [], f"Korean queries returning nothing: {empty}")

    def test_single_word_module_names_retrieve(self):
        """`router` matched ZERO nodes before `path` was scored."""
        for word in ("router", "optimizer", "trajectory", "bootstrap"):
            pack = self.graph.context_pack(word)
            self.assertTrue(pack["items"], f"'{word}' retrieves nothing")

    def test_the_top_hit_is_the_relevant_subsystem(self):
        expectations = {
            "sandbox output capture": "sandbox",
            "provider fleet": "provider",
            "diversity collapse": "diversity",
            "ml leakage holdout": "ml",
        }
        for query, expect in expectations.items():
            top = self.graph.context_pack(query)["items"][0]["id"]
            self.assertIn(expect, top,
                          f"{query!r} ranked {top} first, expected something "
                          f"containing {expect!r}")


@unittest.skipUnless(IS_THE_KIT, SKIP_REASON)
class TestRefreshScript(unittest.TestCase):
    def test_the_script_exists_and_is_idempotent_in_shape(self):
        """Re-running must be safe: the fix has to survive the next rename."""
        script = os.path.join(REPO, "tools", "refresh_self_kg.py")
        self.assertTrue(os.path.exists(script))
        text = Path(script).read_text(encoding="utf-8")
        # Guards that make a second run harmless and an unverifiable path
        # impossible to record.
        self.assertIn("if nid in by_id:", text)
        self.assertIn("os.path.exists", text)
        self.assertIn("refused", text)


if __name__ == "__main__":
    unittest.main()
