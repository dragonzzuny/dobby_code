"""A root has no basename, and the graph built from one would not load.

`bootstrap` named the repository node with
`os.path.basename(os.path.abspath(repo))`. For a drive or filesystem root that
is the empty string:

    os.path.basename("C:/")  ->  ""
    os.path.basename("/")    ->  ""

The ontology requires `name`. So `dobby init --scan C:/ --overwrite` wrote
`kg.bootstrap.json` with `{"id": "repo", "name": ""}`, and after that
`merged_graph` raised `OntologyError: node repo: missing 'name'` from EVERY
entry point that builds the graph -- permanently, until somebody rescanned
with a different root. `inventory_to_kg`'s own docstring said it produced
"ontology-valid nodes/edges".

Found by breaking this machine with it. A hostile-argument probe against the
`bootstrap_scan` capability resolved to a root, the scan reported 20000 files,
and the MCP gateway stopped starting:

    repo.name    ''
    provenance   'bootstrap scan of '
    Gateway('.') OntologyError: node repo: missing 'name'

Recovered by regenerating -- `kg.bootstrap.json` and `inventory.json` are
generated files and gitignored, so the fix was to rescan, not to hand-edit.
The tracked knowledge (`kg.json`, `ontology.json`) was never touched.

A name is substituted rather than an exception raised. The scan itself
succeeded and its files are real; refusing to name the root would throw away a
good inventory over a cosmetic field. The absolute path is preferred over the
generic fallback because "C:/" says which root was scanned.
"""

import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.bootstrap import inventory_to_kg  # noqa: E402
from dobby.core.kg import KnowledgeGraph, Ontology  # noqa: E402

#: The inventory's real shape, keyed as `scan_repo` emits it. Written out
#: rather than loaded from `.dobby/inventory.json` so the test does not depend
#: on this repository having been scanned -- and every list is empty because
#: what is under test is the repository node's NAME, not its neighbours.
INVENTORY = {"file_count": 20000, "languages": {"java": 9, "yaml": 3},
             "top_dirs": [], "skills": [], "scripts": [], "rules": [],
             "tests": [], "build": [], "ci": [], "instructions": [],
             "excluded_as_harness": [], "generated_hint": []}


class Case(unittest.TestCase):
    def setUp(self):
        path = os.path.join(REPO, ".dobby", "ontology.json")
        if not os.path.exists(path):
            self.skipTest("no ontology in this checkout")
        self.ontology = Ontology.load(path)

    def repo_node(self, name):
        kg = inventory_to_kg(INVENTORY, name)
        return next(n for n in kg["nodes"] if n["id"] == "repo")

    def accepts(self, node):
        KnowledgeGraph(self.ontology).add_node(node)
        return True


class EveryNameProducesALoadableGraph(Case):
    def test_an_ordinary_name_is_untouched(self):
        self.assertEqual(self.repo_node("dobby")["name"], "dobby")

    def test_a_non_ascii_name_is_untouched(self):
        """The name that was actually here."""
        self.assertEqual(self.repo_node("도비코드")["name"], "도비코드")

    def test_the_empty_name_a_root_produces_is_replaced(self):
        self.assertTrue(self.accepts(self.repo_node("")))

    def test_whitespace_is_not_a_name_either(self):
        self.assertTrue(self.accepts(self.repo_node("   ")))

    def test_none_does_not_become_the_word_none(self):
        """`str(None)` passes the ontology and reads like a name somebody
        chose. A missing name has to look missing."""
        self.assertNotEqual(self.repo_node(None)["name"], "None")
        self.assertTrue(self.accepts(self.repo_node(None)))

    def test_the_substitute_says_it_is_a_substitute(self):
        self.assertIn("unnamed", self.repo_node("")["name"])

    def test_the_promise_in_the_docstring_holds_for_every_case(self):
        for name in ("dobby", "", "   ", None, "도비코드", "C:\\"):
            with self.subTest(name=name):
                kg = inventory_to_kg(INVENTORY, name)
                graph = KnowledgeGraph(self.ontology)
                for node in kg["nodes"]:
                    graph.add_node(node)


class TheRootItselfIsABetterName(Case):
    """`bootstrap` prefers the absolute path over the generic fallback."""

    def test_basename_of_a_root_is_empty_on_this_platform(self):
        """The premise. If this ever stops being true the fix is moot."""
        roots = ["C:/", "/"] if os.name == "nt" else ["/"]
        for root in roots:
            with self.subTest(root=root):
                self.assertEqual(os.path.basename(os.path.abspath(root)), "")

    def test_bootstrap_names_a_root_by_its_path(self):
        import shutil
        import tempfile

        from dobby.core.bootstrap import bootstrap

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("x = 1\n")

        # A directory whose abspath ends in a separator has no basename, the
        # same shape a drive root has, without scanning a real one.
        data = os.path.join(tmp, ".dobby")
        bootstrap(tmp + os.sep, data_dir=data, overwrite=True)
        with open(os.path.join(data, "knowledge", "kg.bootstrap.json"),
                  encoding="utf-8") as fh:
            kg = json.load(fh)
        node = next(n for n in kg["nodes"] if n["id"] == "repo")
        self.assertTrue(node["name"], "the repository node has no name")
        self.assertTrue(self.accepts(node))

    def test_a_scanned_tree_still_loads_end_to_end(self):
        """The whole point: what bootstrap writes, merged_graph reads."""
        import shutil
        import tempfile

        from dobby.core.bootstrap import bootstrap, merged_graph

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a.py"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("x = 1\n")
        data = os.path.join(tmp, ".dobby")
        bootstrap(tmp + os.sep, data_dir=data, overwrite=True)
        shutil.copy(os.path.join(REPO, ".dobby", "ontology.json"),
                    os.path.join(data, "ontology.json"))
        merged_graph(Ontology.load(os.path.join(data, "ontology.json")), data)


if __name__ == "__main__":
    unittest.main()
