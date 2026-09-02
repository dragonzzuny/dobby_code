"""A knowledge graph that loses edges has to say which ones.

`bootstrap.merged_graph` merged `kg.json` and `kg.bootstrap.json` and dropped
any edge the ontology refused, under this comment:

    pass  # edge to a node that lost the merge; drop silently but count

Nothing counted. The function returns a graph and nothing else, so a caller had
no way to learn that what it received was thinner than the files it was built
from. Measured on a two-node graph with three declared edges, one of them
valid: one loaded, two vanished, and `merged_graph` returned a graph that
looked complete.

That is the shape this repository refuses everywhere else -- a missing check
reading exactly like a passing one. `not_run` is not a pass, an unmeasured
token count is not zero, and an edge that could not be added is not an edge
that was never declared.

The loss rides ON the graph rather than in the return type, because eight call
sites unpack `merged_graph` and none asked for a tuple. It is a list of
`(edge, reason)` and not a number, so a report can name what was lost.

`dobby doctor` reports it as ADVISORY. A graph missing some edges still answers
most questions, and refusing to start a machine that is merely thinner than its
files would be the wrong trade -- but it is now impossible for the thinning to
happen with nobody able to see it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.bootstrap import merged_graph  # noqa: E402
from dobby.core.kg import KnowledgeGraph, Ontology  # noqa: E402


def real_ontology_path():
    return os.path.join(REPO, ".dobby", "ontology.json")


def real_graph_path():
    return os.path.join(REPO, ".dobby", "knowledge", "kg.json")


class TheGraphCarriesItsLosses(unittest.TestCase):
    """Built from the project's OWN ontology and edge shape.

    A hand-written fixture would be a guess about what an edge looks like, and
    the first version of this probe guessed wrong -- it used `kind` where the
    schema wants `rel`, so every edge was refused and the measurement said
    "4 of 4 lost" about a fixture rather than about the code.
    """

    def setUp(self):
        for path in (real_ontology_path(), real_graph_path()):
            if not os.path.exists(path):
                self.skipTest(f"{path} is not in this checkout")
        with open(real_graph_path(), encoding="utf-8") as fh:
            self.real = json.load(fh)
        if not self.real.get("edges"):
            self.skipTest("the project graph declares no edges to model on")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.knowledge = os.path.join(self.tmp, "knowledge")
        os.makedirs(self.knowledge)
        shutil.copy(real_ontology_path(), self.tmp)
        self.ontology = Ontology.load(os.path.join(self.tmp, "ontology.json"))

    def write(self, *, ghosts):
        """One valid edge plus `ghosts` edges pointing at absent nodes."""
        edge = self.real["edges"][0]
        by_id = {n["id"]: n for n in self.real["nodes"]}
        nodes = [by_id[edge["src"]], by_id[edge["dst"]]]
        edges = [dict(edge)] + [dict(edge, dst=f"GHOST_{i}")
                                for i in range(ghosts)]
        with open(os.path.join(self.knowledge, "kg.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"nodes": nodes, "edges": edges}, fh, ensure_ascii=False)
        return len(edges)

    def test_a_clean_graph_drops_nothing(self):
        self.write(ghosts=0)
        graph = merged_graph(self.ontology, self.tmp)
        self.assertEqual(graph.dropped_edges, [])
        self.assertEqual(len(graph.edges), 1)

    def test_the_dropped_edges_are_reported(self):
        declared = self.write(ghosts=2)
        graph = merged_graph(self.ontology, self.tmp)
        self.assertEqual(len(graph.edges), 1)
        self.assertEqual(len(graph.dropped_edges), 2)
        self.assertEqual(len(graph.edges) + len(graph.dropped_edges), declared)

    def test_each_loss_names_the_edge_and_a_reason(self):
        """A count would say three were lost; this says which three."""
        self.write(ghosts=2)
        graph = merged_graph(self.ontology, self.tmp)
        for edge, reason in graph.dropped_edges:
            self.assertIn("GHOST", edge["dst"])
            self.assertTrue(reason, "an edge dropped for no stated reason")

    def test_the_valid_edge_is_still_there(self):
        """The guard must not turn a partial loss into a total one."""
        self.write(ghosts=2)
        graph = merged_graph(self.ontology, self.tmp)
        self.assertNotIn("GHOST", graph.edges[0]["dst"])


class AGraphBuiltDirectlyHasTheAttribute(unittest.TestCase):
    """`dropped_edges` is on the class, so every graph answers the question."""

    def test_a_fresh_graph_reports_no_losses(self):
        if not os.path.exists(real_ontology_path()):
            self.skipTest("no ontology in this checkout")
        graph = KnowledgeGraph(Ontology.load(real_ontology_path()))
        self.assertEqual(graph.dropped_edges, [])


class DoctorReportsIt(unittest.TestCase):
    """The consumer. Without one this is another field nobody reads."""

    def setUp(self):
        if not os.path.isdir(os.path.join(REPO, ".dobby")):
            self.skipTest("no .dobby in this checkout")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(os.path.join(REPO, ".dobby"),
                        os.path.join(self.tmp, ".dobby"))
        self.kg = os.path.join(self.tmp, ".dobby", "knowledge", "kg.json")

    def damage(self, ghosts):
        with open(self.kg, encoding="utf-8") as fh:
            blob = json.load(fh)
        if not blob.get("edges"):
            self.skipTest("the project graph declares no edges")
        edge = blob["edges"][0]
        blob["edges"] = blob["edges"] + [dict(edge, dst=f"GHOST_{i}")
                                         for i in range(ghosts)]
        with open(self.kg, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(blob, fh, ensure_ascii=False)

    def doctor(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = REPO
        proc = subprocess.run([sys.executable, "-m", "dobby.cli", "doctor"],
                              cwd=self.tmp, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=600)
        report = json.loads(proc.stdout)
        check = next(c for c in report["checks"] if c["check"] == "kg_edges")
        return proc.returncode, check, report

    def test_an_undamaged_graph_passes_the_check(self):
        code, check, _ = self.doctor()
        self.assertTrue(check["ok"], check)
        self.assertEqual(code, 0)

    def test_a_damaged_graph_is_reported_with_the_edges_named(self):
        self.damage(2)
        _, check, _ = self.doctor()
        self.assertFalse(check["ok"])
        self.assertIn("2 edge(s) dropped", check["detail"])
        self.assertIn("GHOST", check["detail"])

    def test_it_is_advisory_and_not_blocking(self):
        """A thinner graph still answers most questions. Refusing to start over
        it would be the wrong trade; saying nothing was the old one."""
        self.damage(2)
        code, check, report = self.doctor()
        self.assertFalse(check["blocking"])
        self.assertEqual(code, 0, "an advisory gap must not fail the command")
        self.assertIn("kg_edges", report["advisory_gaps"])


if __name__ == "__main__":
    unittest.main()
