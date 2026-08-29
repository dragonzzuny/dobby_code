"""What may be written back into the project, and what may not.

`workspace.gate` is the only check between an isolated run's output and
`shutil.copy2` writing into the project tree. It compared STRINGS, and the
compiler that produces the allow set RESOLVES paths -- two enforcement points
with different notions of "inside", which is the shape this repository keeps
finding. Measured against an allow set of `["src"]`:

    src/a.py               allowed, correctly
    src/../etc/passwd      ALLOWED -- it starts with "src/"
    src/../../outside.txt  ALLOWED -- so did this
    ./src/a.py             REFUSED -- and it is the same file
    src//a.py              allowed by accident

and against `protected_paths` of `^\\.dobby/`:

    .dobby/config.json     refused, correctly
    ./.dobby/config.json   ALLOWED -- the regex did not match the ./ form

`git status --porcelain` does not emit those forms, so nothing was reaching them
today. That is a property of one producer. `gate` is a public function, `merge`
trusts it as its only check, and the allow set itself comes from a PLAN -- which
is to say from a model. A check that is correct only because of who happens to
call it is the artifact-store hole one layer over.

Two changes, and the second is deliberately redundant:

    normalise()   both sides are resolved before comparison, and a path that
                  leaves the tree is a violation with its own message
    _inside()     the absolute target is re-checked at the WRITE, with realpath,
                  because a symlink is invisible to any string comparison

NOT VERIFIED HERE: the symlink case. Creating one on this machine needs
privileges this process does not have (`OSError` from `os.symlink`), so
`_inside` is exercised through `..` escapes, which realpath resolves the same
way. Whether it stops a symlink escape on a machine that can make one is
untested, and is stated rather than assumed.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project.workspace import (ChangeManifest, Escapes,  # noqa: E402
                                     MergeRefused, _inside, gate, merge,
                                     normalise)

PROTECTED = [r"^\.dobby/"]


def changed(*written, deleted=()):
    return ChangeManifest(written=tuple(written), deleted=tuple(deleted))


class Normalising(unittest.TestCase):
    def test_the_ordinary_forms_of_one_path_are_one_path(self):
        for raw in ("src/a.py", "./src/a.py", "src//a.py", "src\\a.py",
                    "src/./a.py", "src/b/../a.py"):
            self.assertEqual(normalise(raw), "src/a.py", raw)

    def test_the_root_itself_normalises_to_a_dot(self):
        for raw in (".", "./", "src/.."):
            self.assertEqual(normalise(raw), ".", raw)

    def test_anything_leaving_the_tree_is_refused(self):
        for raw in ("..", "../x", "src/../../x", "a/../../b"):
            with self.assertRaises(Escapes, msg=raw):
                normalise(raw)

    def test_absolute_paths_are_refused_on_both_platforms(self):
        for raw in ("/etc/passwd", "C:/Windows/System32", "C:\\Windows"):
            with self.assertRaises(Escapes, msg=raw):
                normalise(raw)

    def test_an_empty_path_is_refused_rather_than_treated_as_the_root(self):
        for raw in ("", "   "):
            with self.assertRaises(Escapes, msg=repr(raw)):
                normalise(raw)


class TheWriteSetGate(unittest.TestCase):
    def allowed(self, path, allow=("src",)):
        return gate(changed(path), allowed=list(allow)) == []

    def test_a_declared_path_passes(self):
        self.assertTrue(self.allowed("src/a.py"))
        self.assertTrue(self.allowed("src"))
        self.assertTrue(self.allowed("src/deep/b.py"))

    def test_the_same_file_written_differently_still_passes(self):
        """It used to be refused, which is the other half of the same bug."""
        self.assertTrue(self.allowed("./src/a.py"))
        self.assertTrue(self.allowed("src//a.py"))

    def test_a_sibling_with_a_shared_prefix_is_refused(self):
        self.assertFalse(self.allowed("srcx/a.py"))

    def test_the_measured_escapes_are_refused(self):
        self.assertFalse(self.allowed("src/../etc/passwd"))
        self.assertFalse(self.allowed("src/../../outside.txt"))
        self.assertFalse(self.allowed("../outside.txt"))
        self.assertFalse(self.allowed("/etc/passwd"))

    def test_an_allow_set_of_the_root_allows_anything_inside_it(self):
        self.assertTrue(self.allowed("anywhere/at/all.py", allow=(".",)))
        self.assertFalse(self.allowed("../outside.txt", allow=(".",)))

    def test_the_refusal_names_the_path_and_never_a_count(self):
        violations = gate(changed("a.py", "b.py"), allowed=["src"])
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("a.py" in v for v in violations))
        self.assertTrue(any("b.py" in v for v in violations))

    def test_a_deleted_path_is_gated_like_a_written_one(self):
        self.assertEqual(gate(changed(deleted=("src/a.py",)),
                              allowed=["src"]), [])
        self.assertNotEqual(gate(changed(deleted=("other/a.py",)),
                                 allowed=["src"]), [])


class ProtectedPaths(unittest.TestCase):
    def refused(self, path):
        return gate(changed(path), allowed=["."], protected=PROTECTED) != []

    def test_a_protected_path_is_refused_even_inside_the_write_set(self):
        self.assertTrue(self.refused(".dobby/config.json"))

    def test_the_dot_slash_form_no_longer_walks_past_the_pattern(self):
        """The measured bypass."""
        self.assertTrue(self.refused("./.dobby/config.json"))

    def test_a_traversal_landing_on_it_is_refused_too(self):
        self.assertTrue(self.refused("x/../.dobby/config.json"))

    def test_an_unrelated_file_is_untouched(self):
        self.assertFalse(self.refused("src/a.py"))


class ContainmentAtTheWrite(unittest.TestCase):
    """`_inside` is the second check, and it is meant to be redundant."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_an_ordinary_path_resolves_inside(self):
        target = _inside(self.root, "src/a.py")
        self.assertTrue(target.startswith(os.path.realpath(self.root)))

    def test_a_traversal_is_refused_at_the_write_even_if_a_gate_let_it_by(self):
        for path in ("../outside.txt", "src/../../x", ".." + os.sep + "y"):
            with self.assertRaises(MergeRefused, msg=path):
                _inside(self.root, path)

    def test_the_root_itself_is_allowed(self):
        self.assertEqual(_inside(self.root, "."), os.path.realpath(self.root))


class MergeRefusesRatherThanWriting(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.work = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        for d in (self.root, self.work, self.outside):
            self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(self.work, "src"))
        with open(os.path.join(self.work, "src", "a.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("written by the isolated run\n")
        self.victim = os.path.join(self.outside, "victim.txt")
        with open(self.victim, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("untouched\n")

    def test_a_declared_change_merges(self):
        report = merge(changed("src/a.py"), worktree=self.work, root=self.root,
                       allowed=["src"], protected=[])
        self.assertTrue(report["merged"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "src", "a.py")))

    def test_an_escaping_path_is_refused_and_writes_nothing(self):
        relative = os.path.relpath(self.victim, self.root).replace("\\", "/")
        with self.assertRaises(MergeRefused):
            merge(changed(relative), worktree=self.work, root=self.root,
                  allowed=["."], protected=[])
        with open(self.victim, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "untouched\n")

    def test_no_write_set_is_refused_before_anything_is_read(self):
        with self.assertRaises(MergeRefused) as caught:
            merge(changed("src/a.py"), worktree=self.work, root=self.root,
                  allowed=[], protected=[])
        self.assertIn("write set", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
