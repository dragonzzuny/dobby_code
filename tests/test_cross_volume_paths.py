"""Paths on two Windows volumes, which `relpath` refuses to relate.

The CI runner says so itself, in an annotation nobody had acted on:

    workspace and TEMP are on different volumes; os.path.relpath between them
    raises ValueError on Windows

`os.path.commonpath` raises on the same condition. Eight places in this
repository call one of them and one -- `sandbox.py` -- guards. The others were
surveyed and most are safe by construction, because their input comes from
`os.walk(root)` and cannot leave it. Three were not:

    project/workorder.py   a write_set entry, which is a MODEL'S OUTPUT
    codegraph.py           an absolute path handed in by a caller
    cli.py                 a report field, `.dobby` need not share a volume

The first is the one that matters. That function exists to REFUSE a plan
wanting to write outside the project, and a cross-volume absolute path is the
plainest case of exactly that. Measured, before the fix, with a write_set of
`D:\\elsewhere\\a.py` against a project on C::

    ValueError: Paths don't have the same drive

A bare ValueError slips past every `except PlanNotCompilable` in the loop, so
the guard read as a crash rather than as the refusal it is.

The drive-letter cases are Windows-only, and the first version of this file
claimed otherwise -- "on POSIX there is one volume and the strings are simply
ordinary absolute paths". They are not. On POSIX `D:/elsewhere/a.py` is a
RELATIVE path naming a directory called `D:`, so it lands INSIDE the root and
every assertion here inverts. CI said so on all four POSIX jobs while both
Windows jobs passed, which is the second time this session that a claim about
another platform was wrong in the same direction.

So those cases skip off Windows, and the same-volume cases -- a genuinely
absolute path outside the root -- run everywhere, because that question and its
answer are platform-independent.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.cli import _repo_relative  # noqa: E402
from dobby.codegraph import module_name  # noqa: E402
from dobby.project import architecture as A  # noqa: E402
from dobby.project import workorder as W  # noqa: E402
from dobby.project.models import ProjectManifest, WorkItem  # noqa: E402
from dobby.runtime.contracts import LOCAL_WRITE  # noqa: E402

#: A path on another Windows volume. NOT an absolute path on POSIX -- there it
#: names a directory called `D:` relative to wherever you are.
ELSEWHERE = "D:" + os.sep + "elsewhere" + os.sep + "a.py"

WINDOWS_ONLY = "drive letters mean nothing on POSIX; see the module docstring"


class TheStandardLibraryReallyDoesRefuse(unittest.TestCase):
    """The premise. If this stops holding, the guards below are moot."""

    def test_relpath_or_commonpath_refuses_across_volumes(self):
        if os.name != "nt":
            self.skipTest("one volume on POSIX; the premise is Windows-only")
        with self.assertRaises(ValueError):
            os.path.commonpath([ELSEWHERE, "C:" + os.sep + "proj"])
        with self.assertRaises(ValueError):
            os.path.relpath(ELSEWHERE, "C:" + os.sep + "proj")


class TheCompilerRefusesRatherThanCrashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.manifest = ProjectManifest(project_id="p", root=self.tmp.name,
                                        repo_digest="d",
                                        smoke_checks=("pytest -q",))
        self.item = WorkItem(work_item_id="W1", project_id="p", title="t",
                             outcome="o", acceptance_checks=["pytest -q"])

    def compile_with(self, write_set):
        plan = A.PlanSpec(
            plan_id="pl", work_item_id="W1", objective="o",
            side_effect_class=LOCAL_WRITE,
            execution_steps=({"role": "implement", "objective": "x",
                              "write_set": write_set, "read_set": []},))
        return W.compile_orders(plan, item=self.item, manifest=self.manifest)

    def test_a_cross_volume_write_set_is_a_refusal_and_not_a_ValueError(self):
        if os.name != "nt":
            self.skipTest(WINDOWS_ONLY)
        with self.assertRaises(W.PlanNotCompilable) as caught:
            self.compile_with([ELSEWHERE])
        self.assertIn("outside the project root", str(caught.exception))

    def test_the_refusal_names_the_path_that_caused_it(self):
        if os.name != "nt":
            self.skipTest(WINDOWS_ONLY)
        with self.assertRaises(W.PlanNotCompilable) as caught:
            self.compile_with([ELSEWHERE])
        self.assertIn("elsewhere", str(caught.exception))

    def test_an_ordinary_relative_write_set_still_compiles(self):
        """The guard must not refuse the case it was always meant to allow."""
        orders = self.compile_with(["src"])
        self.assertTrue(orders)

    def test_a_path_outside_the_root_on_the_SAME_volume_is_still_refused(self):
        """The pre-existing behaviour, unchanged."""
        outside = os.path.abspath(os.path.join(self.tmp.name, "..", "x.py"))
        with self.assertRaises(W.PlanNotCompilable):
            self.compile_with([outside])


class TheCodeGraphCallsItOutside(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_a_cross_volume_path_has_no_module_name(self):
        """`""` is what the existing "outside the root" branch returns, so a
        caller sees one answer for one condition."""
        if os.name != "nt":
            self.skipTest(WINDOWS_ONLY)
        self.assertEqual(module_name(ELSEWHERE, self.tmp.name), "")

    def test_a_path_inside_the_root_still_gets_its_name(self):
        inside = os.path.join(self.tmp.name, "pkg", "m.py")
        self.assertEqual(module_name(inside, self.tmp.name), "pkg.m")

    def test_a_path_outside_on_the_same_volume_is_still_outside(self):
        outside = os.path.abspath(os.path.join(self.tmp.name, "..", "m.py"))
        self.assertEqual(module_name(outside, self.tmp.name), "")


class TheReportFallsBackToAnAbsolutePath(unittest.TestCase):
    """A ValueError out of a reporting line is a command dying while saying
    what it did. An absolute path in a report is merely less tidy."""

    def test_a_cross_volume_path_comes_back_absolute(self):
        if os.name != "nt":
            self.skipTest(WINDOWS_ONLY)
        got = _repo_relative(ELSEWHERE, "C:" + os.sep + "proj")
        self.assertTrue(os.path.isabs(got), got)
        self.assertIn("elsewhere", got)

    def test_an_ordinary_path_is_still_relative(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as root:
            inside = os.path.join(root, "sub", "f.txt")
            got = _repo_relative(inside, root)
            self.assertFalse(os.path.isabs(got), got)
            self.assertIn("f.txt", got)


if __name__ == "__main__":
    unittest.main()
