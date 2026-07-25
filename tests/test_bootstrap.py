import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.bootstrap import bootstrap, scan_repo
from dobby.core.kg import Ontology, KnowledgeGraph


def make_foreign_repo(root):
    """A node.js-ish repo the harness has never seen (generalization check)."""
    os.makedirs(os.path.join(root, "src"))
    os.makedirs(os.path.join(root, "tests"))
    os.makedirs(os.path.join(root, "scripts"))
    os.makedirs(os.path.join(root, ".github", "workflows"))
    open(os.path.join(root, "package.json"), "w").write('{"name":"demo"}')
    open(os.path.join(root, "README.md"), "w").write("# demo")
    open(os.path.join(root, "AGENTS.md"), "w").write("# rules")
    open(os.path.join(root, "src", "index.js"), "w").write("//")
    open(os.path.join(root, "tests", "test_a.py"), "w").write("#")
    open(os.path.join(root, "scripts", "deploy_check.py"), "w").write("#")
    open(os.path.join(root, ".github", "workflows", "ci.yml"), "w").write("on: push")


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_foreign_repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_evidence(self):
        inv = scan_repo(self.tmp.name)
        self.assertIn("javascript", inv["languages"])
        self.assertEqual(inv["build"][0]["system"], "npm")
        self.assertTrue(inv["ci"])
        self.assertIn("AGENTS.md", inv["instructions"])
        self.assertTrue(any("deploy_check" in s for s in inv["scripts"]))

    def test_bootstrap_writes_ontology_valid_graph(self):
        res = bootstrap(self.tmp.name)
        with open(res["kg"], encoding="utf-8") as f:
            data = json.load(f)
        onto = Ontology.load(os.path.join(REPO, ".dobby", "ontology.json"))
        g = KnowledgeGraph(onto, data["nodes"], data["edges"])  # validates
        self.assertIn("doc:AGENTS.md", g.nodes)
        self.assertIn("tool:script:deploy_check.py", g.nodes)
        # existence facts are verified; inferred conventions are not
        self.assertEqual(g.nodes["doc:AGENTS.md"]["provenance"]["confidence"],
                         "verified")

    def test_bootstrap_never_clobbers_without_overwrite(self):
        bootstrap(self.tmp.name)
        with self.assertRaises(FileExistsError):
            bootstrap(self.tmp.name)
        bootstrap(self.tmp.name, overwrite=True)  # explicit refresh ok

    def test_bootstrap_writes_to_bootstrap_file_not_curated(self):
        res = bootstrap(self.tmp.name)
        self.assertTrue(res["kg"].endswith("kg.bootstrap.json"))
        curated = os.path.join(self.tmp.name, ".dobby", "knowledge", "kg.json")
        self.assertFalse(os.path.exists(curated))


if __name__ == "__main__":
    unittest.main()
