"""The gate that turns `write_set` from a comment into a rule.

`WorkOrder.write_set` was recorded and unenforced, and the compiler's own ledger
said so. That is the failure this repository keeps naming in other forms: a field
that reads as a control and behaves as documentation. Nothing about the declared
set changed here — what changed is that something now measures the tree against
it and refuses.

So the tests are about the refusals and about the undo. A merge gate that lets
one undeclared path through is not a gate, and a merge gate that leaves a broken
tree behind when the baseline fails is worse than no gate, because the operator
now has a half-applied change AND a passing report about it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import architecture as A
from dobby.project import loop as L
from dobby.project import workspace as W

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'
FAILING_SMOKE = '{python} -c "import sys; sys.exit(3)"'


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.write("app.py", "print('a')\n")
        git(self.root, "init", "-q")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text, root=None):
        target = os.path.join(root or self.root, path)
        os.makedirs(os.path.dirname(target) or (root or self.root),
                    exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read(self, path, root=None):
        with open(os.path.join(root or self.root, path), encoding="utf-8") as f:
            return f.read()


class TheManifestIsAMeasurement(RepoCase):
    def test_an_added_file_is_seen(self):
        """A diff would report nothing; an added file is what nobody declared."""
        with W.isolated(self.root) as (tree, _):
            self.write("brand-new.txt", "x", root=tree)
            manifest = W.changed_paths(tree)
        self.assertIn("brand-new.txt", manifest.written)

    def test_an_edited_file_is_seen(self):
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "print('a')\nprint('b')\n", root=tree)
            manifest = W.changed_paths(tree)
        self.assertIn("app.py", manifest.written)

    def test_a_deleted_file_is_seen_as_deleted(self):
        with W.isolated(self.root) as (tree, _):
            os.remove(os.path.join(tree, "app.py"))
            manifest = W.changed_paths(tree)
        self.assertEqual(manifest.deleted, ("app.py",))
        self.assertEqual(manifest.written, ())

    def test_an_untouched_tree_reports_nothing(self):
        with W.isolated(self.root) as (tree, _):
            manifest = W.changed_paths(tree)
        self.assertEqual(manifest.paths, ())


class TheGateNamesEveryPathItRefuses(unittest.TestCase):
    def manifest(self, *paths):
        return W.ChangeManifest(written=tuple(paths))

    def test_a_path_outside_the_write_set_is_refused_by_name(self):
        violations = W.gate(self.manifest("app.py", "secrets.env"),
                            allowed=("app.py",), protected=[])
        self.assertEqual(len(violations), 1)
        self.assertIn("secrets.env", violations[0])

    def test_every_offending_path_gets_its_own_line(self):
        """A count without the names is the finding shape this repo rejects."""
        violations = W.gate(self.manifest("a.py", "b.py", "c.py"),
                            allowed=(), protected=[])
        self.assertEqual(len(violations), 3)

    def test_a_directory_in_the_write_set_covers_what_is_under_it(self):
        self.assertEqual(W.gate(self.manifest("src/deep/x.py"),
                                allowed=("src",), protected=[]), [])

    def test_a_sibling_with_the_same_prefix_is_not_covered(self):
        """`src` must not authorise `srcfake/`."""
        violations = W.gate(self.manifest("srcfake/x.py"), allowed=("src",),
                            protected=[])
        self.assertEqual(len(violations), 1)

    def test_a_protected_path_is_refused_even_when_declared(self):
        violations = W.gate(self.manifest(".git/config"),
                            allowed=(".git/config",),
                            protected=[r"\.git/"])
        self.assertEqual(len(violations), 1)
        self.assertIn("protected", violations[0])

    def test_a_clean_change_passes(self):
        self.assertEqual(W.gate(self.manifest("app.py"), allowed=("app.py",),
                                protected=[]), [])


class TheMergeRefusesRatherThanApproximates(RepoCase):
    def test_an_undeclared_write_set_is_refused(self):
        """Isolation whose output cannot be gated is unisolated with extra steps."""
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "changed\n", root=tree)
            with self.assertRaises(W.MergeRefused) as caught:
                W.merge(W.changed_paths(tree), worktree=tree, root=self.root,
                        allowed=(), protected=[])
        self.assertIn("no write set", str(caught.exception))

    def test_a_violation_merges_nothing_at_all(self):
        original = self.read("app.py")
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "changed\n", root=tree)
            self.write("sneaky.txt", "x", root=tree)
            with self.assertRaises(W.MergeRefused):
                W.merge(W.changed_paths(tree), worktree=tree, root=self.root,
                        allowed=("app.py",), protected=[])
        self.assertEqual(self.read("app.py"), original,
                         "a refused merge wrote the allowed path anyway")
        self.assertFalse(os.path.exists(os.path.join(self.root, "sneaky.txt")))

    def test_a_clean_merge_copies_edits_additions_and_deletions(self):
        self.write("doomed.txt", "bye\n")
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "print('a')\nprint('b')\n", root=tree)
            self.write("added/new.py", "n=1\n", root=tree)
            report = W.merge(W.changed_paths(tree), worktree=tree,
                             root=self.root,
                             allowed=("app.py", "added"), protected=[])
        self.assertTrue(report["merged"], report)
        self.assertIn("print('b')", self.read("app.py"))
        self.assertTrue(os.path.exists(os.path.join(self.root, "added",
                                                    "new.py")))

    def test_a_deletion_is_carried_across(self):
        self.write("doomed.txt", "bye\n")
        git(self.root, "add", "-A")
        git(self.root, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "add doomed")
        with W.isolated(self.root) as (tree, _):
            os.remove(os.path.join(tree, "doomed.txt"))
            report = W.merge(W.changed_paths(tree), worktree=tree,
                             root=self.root, allowed=("doomed.txt",),
                             protected=[])
        self.assertTrue(report["merged"], report)
        self.assertFalse(os.path.exists(os.path.join(self.root, "doomed.txt")))


class AFailedBaselineUndoesTheWholeMerge(RepoCase):
    def test_every_path_is_byte_identical_to_what_it_was(self):
        """The threshold declared before the experiment: byte-identical, not close."""
        before_app = self.read("app.py")
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "print('broken')\n", root=tree)
            self.write("added.txt", "new\n", root=tree)
            report = W.merge(W.changed_paths(tree), worktree=tree,
                             root=self.root, allowed=("app.py", "added.txt"),
                             protected=[], smoke=(FAILING_SMOKE,))
        self.assertFalse(report["merged"], report)
        self.assertEqual(self.read("app.py"), before_app,
                         "an edited path survived a reverted merge")
        self.assertFalse(os.path.exists(os.path.join(self.root, "added.txt")),
                         "a file added by a reverted merge was left behind")

    def test_a_passing_baseline_keeps_the_change(self):
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "print('fine')\n", root=tree)
            report = W.merge(W.changed_paths(tree), worktree=tree,
                             root=self.root, allowed=("app.py",), protected=[],
                             smoke=(PASSING_SMOKE,))
        self.assertTrue(report["merged"], report)
        self.assertIn("fine", self.read("app.py"))

    def test_no_smoke_checks_is_reported_as_unverified_not_as_verified(self):
        with W.isolated(self.root) as (tree, _):
            self.write("app.py", "print('x')\n", root=tree)
            report = W.merge(W.changed_paths(tree), worktree=tree,
                             root=self.root, allowed=("app.py",), protected=[])
        self.assertTrue(report["merged"])
        self.assertIn("nothing verified", report["note"])


class TheLoopSaysWhenItCouldNotIsolate(RepoCase):
    def setUp(self):
        super().setUp()
        self.data = os.path.join(self.tmp.name, ".dobby")

    def init(self, checks):
        report = initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                            item_specs=[{"outcome": "change the app",
                                         "acceptance_checks": checks}])
        self.project_id = report["project_id"]

    def test_an_item_with_no_write_set_stops_rather_than_running_unisolated(self):
        """Silently downgrading leaves the operator worse off than being told."""
        self.init([PASSING_SMOKE])
        result = L.advance(self.data, isolate=True)
        self.assertEqual(result["stopped"], L.ISOLATION_UNAVAILABLE, result)
        self.assertEqual(result["iterations"], [],
                         "a run started after isolation was found impossible")

    def test_the_stop_reason_is_a_declared_one(self):
        self.init([PASSING_SMOKE])
        self.assertIn(L.advance(self.data, isolate=True)["stopped"],
                      L.STOP_REASONS)

    def test_an_isolated_run_reports_its_merge(self):
        self.init([PASSING_SMOKE])
        A.request_architecture(
            self.data, "W001", project_id=self.project_id,
            propose=lambda _r: {
                "objective": "edit the app",
                "proposed_acceptance_checks": [PASSING_SMOKE],
                "side_effect_class": "LOCAL_WRITE",
                "execution_steps": [{"role": "implement",
                                     "objective": "edit it",
                                     "write_set": ["app.py"]}]})
        result = L.advance(self.data, isolate=True, compile_plans=True)
        step = result["iterations"][0]
        self.assertIsNotNone(step["workspace"], step)
        self.assertTrue(step["workspace"]["merged"], step["workspace"])

    def test_the_write_set_is_read_from_the_compiled_plan(self):
        self.init([PASSING_SMOKE])
        A.request_architecture(
            self.data, "W001", project_id=self.project_id,
            propose=lambda _r: {
                "objective": "edit the app",
                "proposed_acceptance_checks": [PASSING_SMOKE],
                "side_effect_class": "LOCAL_WRITE",
                "execution_steps": [{"role": "implement", "objective": "edit",
                                     "write_set": ["app.py", "docs"]}]})
        store = ProjectStore(self.data)
        item = store.load_project(self.project_id)["portfolio"].get("W001")
        self.assertEqual(W.declared_write_set(store, self.project_id, item),
                         ("app.py", "docs"))

    def test_an_unplanned_item_declares_nothing(self):
        self.init([PASSING_SMOKE])
        store = ProjectStore(self.data)
        item = store.load_project(self.project_id)["portfolio"].get("W001")
        self.assertEqual(W.declared_write_set(store, self.project_id, item), ())


if __name__ == "__main__":
    unittest.main()
