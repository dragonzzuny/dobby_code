import json
import os
import pathlib
import shutil
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
    pathlib.Path(os.path.join(root, "package.json")).write_text('{"name":"demo"}', encoding="utf-8")
    pathlib.Path(os.path.join(root, "README.md")).write_text("# demo", encoding="utf-8")
    pathlib.Path(os.path.join(root, "AGENTS.md")).write_text("# rules", encoding="utf-8")
    pathlib.Path(os.path.join(root, "src", "index.js")).write_text("//", encoding="utf-8")
    pathlib.Path(os.path.join(root, "tests", "test_a.py")).write_text("#", encoding="utf-8")
    pathlib.Path(os.path.join(root, "scripts", "deploy_check.py")).write_text("#", encoding="utf-8")
    pathlib.Path(os.path.join(root, ".github", "workflows", "ci.yml")).write_text("on: push", encoding="utf-8")


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




class TestTheToolIsNotTheWork(unittest.TestCase):
    """In a host, dobby was scanning and graphing itself.

    Found by installing into two real projects. `dobby init --scan .` runs after
    the engine lands, so it inventoried the harness:

        project content   one JPEG
        bootstrap said    114 files scanned, languages: ['python', 'markdown']
        nodes             area:dobby, area:mcp, area:tests, doc:AGENTS.md
        dobby graph       94 modules, 190 edges - all of dobby's own
        dobby context     "items": []

    That last line is the visible damage. It is step one of the README's
    five-minute walkthrough, and on a fresh install it answered nothing, because
    everything retrievable was about the harness while the task was about the
    project.

    In the KIT those same directories ARE the product, so nothing is excluded
    there. One predicate decides - `core.scan_exclusions` - because the two
    scanners disagreeing is how this survived.
    """

    def _host(self):
        """A directory that looks like an installed host: engine, no install.sh."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("dobby", "mcp", "tests", "evals", "docs", "reports"):
            os.makedirs(os.path.join(root, name), exist_ok=True)
            with open(os.path.join(root, name, "engine.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("x = 1\n")
        os.makedirs(os.path.join(root, "src"), exist_ok=True)
        with open(os.path.join(root, "src", "mine.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("y = 2\n")
        return root

    def test_the_kit_is_recognised_as_the_kit(self):
        from dobby.core import is_kit
        self.assertTrue(is_kit(REPO))

    def test_a_host_is_not_mistaken_for_the_kit(self):
        from dobby.core import is_kit
        self.assertFalse(is_kit(self._host()))

    def test_the_kit_excludes_nothing(self):
        """dobby/ and tests/ are the product here; skipping them would be wrong."""
        from dobby.core import scan_exclusions
        self.assertEqual(scan_exclusions(REPO), frozenset())

    def test_a_host_excludes_the_engine_footprint(self):
        from dobby.core import scan_exclusions
        excluded = scan_exclusions(self._host())
        for name in ("dobby", "mcp", "tests", "evals", "docs", "reports"):
            self.assertIn(name, excluded)

    def test_a_host_does_not_exclude_the_projects_own_directories(self):
        from dobby.core import scan_exclusions
        self.assertNotIn("src", scan_exclusions(self._host()))

    def test_the_graph_in_a_host_ignores_the_engine(self):
        from dobby.codegraph import discover
        host = self._host()
        found = [os.path.relpath(p, host).replace(os.sep, "/")
                 for p in discover(host)]
        self.assertEqual(found, ["src/mine.py"], found)

    def test_the_graph_in_the_kit_still_sees_the_engine(self):
        from dobby.codegraph import discover
        found = discover(REPO)
        self.assertTrue(any(p.replace(os.sep, "/").endswith("dobby/cli.py")
                            for p in found),
                        "the kit must graph its own product")

    def test_the_inventory_reports_what_it_excluded(self):
        """A silent exclusion is how a scanner's scope becomes a mystery."""
        from dobby.core.bootstrap import scan_repo
        inv = scan_repo(self._host())
        self.assertIn("excluded_as_harness", inv)
        self.assertIn("dobby", inv["excluded_as_harness"])
        self.assertEqual(scan_repo(REPO)["excluded_as_harness"], [])

    def test_a_hosts_inventory_counts_only_the_project(self):
        from dobby.core.bootstrap import scan_repo
        inv = scan_repo(self._host())
        self.assertNotIn("dobby", inv["top_dirs"])
        self.assertIn("src", inv["top_dirs"])


if __name__ == "__main__":
    unittest.main()
