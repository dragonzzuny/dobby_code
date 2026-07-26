"""Import edges, and the honesty a graph like this has to carry.

`tokens.blast_radius` took an edge list and nothing in the kit produced one — a
recorded gap. `dobby/codegraph.py` closes it with the standard library's parser.

The tests that matter most are not the happy path. They are:

  * relative imports, because `from . import x` resolves from a different base
    depending on whether the importing file is a package `__init__`, and
    `module_name` deliberately collapses that distinction;
  * unreadable files, because a file that contributes no edges makes the radius
    look SMALLER, which is the one direction a review must not be misled in;
  * the caveat, because an import graph is not a call graph and a caller who does
    not know that will over-trust the answer.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.codegraph import (SKIP_DIRS, _resolve_relative, changed_modules,
                             discover, import_edges, module_name, radius_for)


class _Tree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, body=""):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        return path


class TestModuleNaming(_Tree):
    def test_a_plain_module(self):
        path = self.write("pkg/mod.py")
        self.assertEqual(module_name(path, self.root), "pkg.mod")

    def test_a_package_init_is_the_package(self):
        """`pkg.__init__` is not what any importer writes."""
        path = self.write("pkg/__init__.py")
        self.assertEqual(module_name(path, self.root), "pkg")

    def test_a_top_level_module(self):
        path = self.write("solo.py")
        self.assertEqual(module_name(path, self.root), "solo")

    def test_nested_packages(self):
        path = self.write("a/b/c/d.py")
        self.assertEqual(module_name(path, self.root), "a.b.c.d")


class TestDiscovery(_Tree):
    def test_skips_the_directories_that_are_not_this_project(self):
        self.write("keep.py")
        for skipped in ("__pycache__", "node_modules", ".venv", "build",
                        ".git"):
            self.write(f"{skipped}/ignored.py")
        found = [os.path.basename(p) for p in discover(self.root)]
        self.assertEqual(found, ["keep.py"])

    def test_the_skip_list_covers_build_output_and_captured_state(self):
        for expected in ("__pycache__", "node_modules", "build", "dist",
                         ".venv", ".git"):
            self.assertIn(expected, SKIP_DIRS)

    def test_non_python_files_are_not_discovered(self):
        self.write("a.py")
        self.write("b.txt")
        self.write("c.md")
        self.assertEqual(len(discover(self.root)), 1)


class TestRelativeImportResolution(unittest.TestCase):
    """The case `module_name` destroys the information for."""

    def test_one_dot_from_a_module_means_its_package(self):
        # in pkg/sub/mod.py: `from . import x` -> pkg.sub.x
        self.assertEqual(
            _resolve_relative("pkg.sub.mod", False, 1, None), "pkg.sub")
        self.assertEqual(
            _resolve_relative("pkg.sub.mod", False, 1, "helper"),
            "pkg.sub.helper")

    def test_one_dot_from_a_package_init_means_the_package_itself(self):
        # in pkg/sub/__init__.py, whose module name is already `pkg.sub`
        self.assertEqual(
            _resolve_relative("pkg.sub", True, 1, "helper"), "pkg.sub.helper")

    def test_two_dots_walk_one_further_up(self):
        # in pkg/sub/mod.py: `from ..other import y` -> pkg.other
        self.assertEqual(
            _resolve_relative("pkg.sub.mod", False, 2, "other"), "pkg.other")

    def test_two_dots_from_a_package_init(self):
        self.assertEqual(
            _resolve_relative("pkg.sub", True, 2, "other"), "pkg.other")

    def test_walking_past_the_top_does_not_produce_leading_dots(self):
        resolved = _resolve_relative("mod", False, 3, "x")
        self.assertFalse(resolved.startswith("."), resolved)


class TestEdgeExtraction(_Tree):
    def test_a_plain_import(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "")
        edges, _ = import_edges(self.root)
        self.assertIn(("a", "b"), edges)

    def test_from_import_of_a_module(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/one.py", "from pkg import two\n")
        self.write("pkg/two.py", "")
        edges, _ = import_edges(self.root)
        self.assertIn(("pkg.one", "pkg.two"), edges)

    def test_from_import_of_a_symbol_resolves_to_the_longest_real_module(self):
        """`from pkg.deep import Thing` is an edge to pkg.deep, not to pkg."""
        self.write("pkg/__init__.py", "")
        self.write("pkg/deep.py", "class Thing: pass\n")
        self.write("user.py", "from pkg.deep import Thing\n")
        edges, _ = import_edges(self.root)
        self.assertIn(("user", "pkg.deep"), edges)
        self.assertNotIn(("user", "pkg"), edges)

    def test_a_relative_import_becomes_an_absolute_edge(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/core/__init__.py", "")
        self.write("pkg/core/util.py", "")
        self.write("pkg/app/__init__.py", "")
        self.write("pkg/app/main.py", "from ..core.util import helper\n")
        edges, _ = import_edges(self.root)
        self.assertIn(("pkg.app.main", "pkg.core.util"), edges)

    def test_external_imports_are_dropped_by_default(self):
        """Nothing here changes `json`, so it can never originate a radius."""
        self.write("a.py", "import json\nimport os\n")
        edges, report = import_edges(self.root)
        self.assertEqual(edges, [])
        self.assertTrue(report["internal_only"])

    def test_external_imports_can_be_kept_when_asked(self):
        self.write("a.py", "import json\n")
        edges, _ = import_edges(self.root, internal_only=False)
        self.assertIn(("a", "json"), edges)

    def test_a_module_is_never_its_own_dependent(self):
        self.write("pkg/__init__.py", "from pkg import mod\n")
        self.write("pkg/mod.py", "")
        edges, _ = import_edges(self.root)
        self.assertNotIn(("pkg", "pkg"), edges)

    def test_duplicate_imports_produce_one_edge(self):
        self.write("a.py", "import b\nfrom b import x\nimport b\n")
        self.write("b.py", "x = 1\n")
        edges, _ = import_edges(self.root)
        self.assertEqual([e for e in edges if e[0] == "a"], [("a", "b")])


class TestUnparseableFilesAreReported(_Tree):
    """The one direction the answer must not be quietly wrong in."""

    def test_a_syntax_error_is_named_not_swallowed(self):
        self.write("good.py", "import broken\n")
        self.write("broken.py", "def f(:\n")
        edges, report = import_edges(self.root)
        self.assertEqual(len(report["unreadable"]), 1, report["unreadable"])
        self.assertIn("broken.py", report["unreadable"][0]["path"])
        self.assertIn("SyntaxError", report["unreadable"][0]["reason"])

    def test_coverage_falls_below_one_when_a_file_is_skipped(self):
        self.write("a.py", "")
        self.write("b.py", "def f(:\n")
        _, report = import_edges(self.root)
        self.assertEqual(report["files_found"], 2)
        self.assertEqual(report["files_parsed"], 1)
        self.assertEqual(report["coverage"], 0.5)

    def test_a_clean_tree_reports_full_coverage(self):
        self.write("a.py", "")
        _, report = import_edges(self.root)
        self.assertEqual(report["coverage"], 1.0)
        self.assertEqual(report["unreadable"], [])


class TestTheCaveatTravelsWithTheAnswer(_Tree):
    def test_every_report_states_that_this_is_not_a_call_graph(self):
        self.write("a.py", "")
        _, report = import_edges(self.root)
        self.assertIn("not call edges", report["note"])
        self.assertIn("Over-approximates", report["note"])
        self.assertIn("Under-approximates", report["note"])
        self.assertIn("floor, not a ceiling", report["note"])

    def test_the_radius_carries_the_graph_report(self):
        self.write("a.py", "import b\n")
        self.write("b.py", "")
        result = radius_for(self.root, ["b.py"])
        self.assertIn("graph", result)
        self.assertIn("not call edges", result["graph"]["note"])


class TestRadius(_Tree):
    def test_it_finds_dependents_not_dependencies(self):
        """The question is who depends on the change, not what it depends on."""
        self.write("leaf.py", "")
        self.write("mid.py", "import leaf\n")
        self.write("top.py", "import mid\n")
        result = radius_for(self.root, ["leaf.py"])
        self.assertEqual(result["changed"], ["leaf"])
        self.assertIn("mid", result["impacted"])
        self.assertIn("top", result["impacted"])

    def test_a_dependency_of_the_change_is_not_impacted(self):
        self.write("used.py", "")
        self.write("changed.py", "import used\n")
        result = radius_for(self.root, ["changed.py"])
        self.assertNotIn("used", result["impacted"])

    def test_max_hops_is_respected_and_reported(self):
        self.write("l0.py", "")
        self.write("l1.py", "import l0\n")
        self.write("l2.py", "import l1\n")
        self.write("l3.py", "import l2\n")
        result = radius_for(self.root, ["l0.py"], max_hops=1)
        self.assertIn("l1", result["impacted"])
        self.assertNotIn("l2", result["impacted"])
        self.assertEqual(result["max_hops"], 1)

    def test_non_python_changes_say_so_rather_than_returning_an_empty_radius(self):
        """An empty radius reads as "nothing depends on this", which is not the
        claim; the claim is that an import graph cannot answer for a .md file."""
        self.write("a.py", "")
        result = radius_for(self.root, ["README.md", "config.yaml"])
        self.assertEqual(result["changed"], [])
        self.assertIn("cannot say anything", result["note"])
        self.assertNotIn("impacted", result)

    def test_changed_modules_drops_non_python_and_deduplicates(self):
        self.write("pkg/__init__.py", "")
        names = changed_modules(
            ["pkg/__init__.py", "pkg/__init__.py", "notes.md"], self.root)
        self.assertEqual(names, ["pkg"])


class TestAgainstThisRepository(unittest.TestCase):
    """A fixture proves the parser; the repo proves it on real code."""

    @classmethod
    def setUpClass(cls):
        cls.edges, cls.report = import_edges(REPO)

    def test_every_file_in_this_repo_parses(self):
        self.assertEqual(self.report["unreadable"], [],
                         "a file in this repo no longer parses")
        self.assertEqual(self.report["coverage"], 1.0)

    def test_the_relative_imports_in_dobby_resolve(self):
        """dobby/providers/run.py does `from ..core.platform import ...`."""
        self.assertIn(("dobby.providers.run", "dobby.core.platform"),
                      self.edges)

    def test_a_known_dependent_is_found(self):
        """dobby/judge.py imports from dobby.core.security."""
        result = radius_for(REPO, ["dobby/core/security.py"])
        self.assertIn("dobby.judge", result["impacted"])
        self.assertIn("dobby.providers.run", result["impacted"])

    def test_a_module_nothing_imports_has_an_empty_radius(self):
        result = radius_for(REPO, ["dobby/cli.py"], max_hops=1)
        # The CLI is an entry point; tests import it, nothing in dobby/ does.
        self.assertNotIn("dobby.core.kg", result["impacted"])


if __name__ == "__main__":
    unittest.main()
