import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.kg import Ontology, KnowledgeGraph, OntologyError

ONTO = Ontology.load(os.path.join(REPO, ".dobby", "ontology.json"))
PROV = {"source": "test", "method": "curated", "date": "2026-07-12",
        "confidence": "verified"}


def node(nid="n1", ntype="Tool", **kw):
    d = {"id": nid, "type": ntype, "name": nid, "summary": "s",
         "provenance": dict(PROV)}
    d.update(kw)
    return d


class TestOntologyValidation(unittest.TestCase):
    def test_unknown_node_type_rejected(self):
        with self.assertRaises(OntologyError):
            KnowledgeGraph(ONTO, [node(ntype="Widget")])

    def test_provenance_required(self):
        bad = node()
        del bad["provenance"]
        with self.assertRaises(OntologyError):
            KnowledgeGraph(ONTO, [bad])

    def test_model_assertion_never_verified(self):
        bad = node()
        bad["provenance"] = dict(PROV, method="model_assertion",
                                 confidence="verified")
        with self.assertRaises(OntologyError):
            KnowledgeGraph(ONTO, [bad])

    def test_edge_endpoints_must_exist(self):
        g = KnowledgeGraph(ONTO, [node("a"), node("b")])
        with self.assertRaises(OntologyError):
            g.add_edge({"src": "a", "rel": "invokes", "dst": "ghost",
                        "provenance": dict(PROV)})

    def test_unknown_relation_rejected(self):
        g = KnowledgeGraph(ONTO, [node("a"), node("b")])
        with self.assertRaises(OntologyError):
            g.add_edge({"src": "a", "rel": "likes", "dst": "b",
                        "provenance": dict(PROV)})


class TestRetrieval(unittest.TestCase):
    def setUp(self):
        from dobby.core.bootstrap import merged_graph
        self.g = merged_graph(ONTO, os.path.join(REPO, ".dobby"))

    def test_task_start_query_hits_skill_and_policy(self):
        ids = [h.node["id"] for h in self.g.retrieve(
            "How should I start working on a new multi-step task?", k=10)]
        self.assertIn("skill:ledgered-task", ids)
        self.assertIn("policy:P-DECOMPOSE", ids)

    def test_unverified_nodes_penalized(self):
        g = KnowledgeGraph(ONTO, [
            node("v", keywords=["query target"], authority=0.5),
            node("u", keywords=["query target"], authority=0.5,
                 provenance=dict(PROV, method="model_assertion",
                                 confidence="weakly_inferred")),
        ])
        hits = {h.node["id"]: h.score for h in g.retrieve("query target", k=5)}
        self.assertGreater(hits["v"], hits["u"])

    def test_context_pack_respects_budget(self):
        pack = self.g.context_pack("validate outputs before reporting done",
                                   token_budget=120)
        self.assertLessEqual(pack["approx_tokens"], 120)

    def test_retrieval_deterministic(self):
        a = [h.node["id"] for h in self.g.retrieve("bootstrap a new repository", k=8)]
        b = [h.node["id"] for h in self.g.retrieve("bootstrap a new repository", k=8)]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
