"""The scoring record, which was wrong while the console was right.

Two defects, both found by running a five-instance experiment and then looking
at what it left on disk:

1. `score_one.py` keyed its output file by ARM alone. Fifteen scoring runs left
   three rows, one per arm, and all three were the last instance scored. Nothing
   errored; the console printed the correct figure each time and the file kept
   only the last one. A record that disagrees with the run it records is worse
   than no record, because it is the thing a later reader trusts.

2. `local_resolve.apply_patch` raised on a patch already applied. Scoring is a
   separate script from the arm run precisely so a scoring bug can be fixed and
   the scoring repeated without buying another set of provider calls -- and it
   could not be repeated, because the second run re-applied the test patch to a
   tree that already carried it.

Both are about repeating a measurement. The experiment they were found in
(`reports/LEDGER_three_arm_regression.md`) cost fifteen provider runs, and the
first scoring pass over it produced a file with three rows in it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "evals", "swebench"))

import local_resolve as LR  # noqa: E402


def git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class ApplyPatchIsRepeatable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        self.path = os.path.join(self.repo, "a.txt")
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("one\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-qm", "base")
        self.patch = ("diff --git a/a.txt b/a.txt\n"
                      "--- a/a.txt\n+++ b/a.txt\n"
                      "@@ -1 +1 @@\n-one\n+two\n")

    def read(self):
        with open(self.path, encoding="utf-8") as fh:
            return fh.read()

    def test_it_applies_the_first_time(self):
        LR.apply_patch(self.repo, self.patch, label="test_patch")
        self.assertEqual(self.read(), "two\n")

    def test_applying_it_twice_is_not_an_error(self):
        LR.apply_patch(self.repo, self.patch, label="test_patch")
        LR.apply_patch(self.repo, self.patch, label="test_patch")
        self.assertEqual(self.read(), "two\n", "and does not apply it twice")

    def test_a_patch_that_genuinely_does_not_apply_still_raises(self):
        """The skip must be `already in`, not `any failure`."""
        with self.assertRaises(RuntimeError):
            LR.apply_patch(self.repo,
                           "diff --git a/gone.txt b/gone.txt\n"
                           "--- a/gone.txt\n+++ b/gone.txt\n"
                           "@@ -1 +1 @@\n-x\n+y\n", label="test_patch")

    def test_it_leaves_no_diff_file_behind_on_either_path(self):
        LR.apply_patch(self.repo, self.patch, label="test_patch")
        LR.apply_patch(self.repo, self.patch, label="test_patch")
        self.assertEqual(
            [n for n in os.listdir(self.repo) if n.endswith(".diff")], [])


class TheRecordKeepsEveryRow(unittest.TestCase):
    """Read from the source rather than asserted about a fixture.

    The bug was a single subscript, `existing[arm]`, and the thing that makes it
    a bug is that the key omits the instance. A test that ran the whole scorer
    would need a checkout, a calibration and a test suite; what it would
    actually be checking is this key.
    """

    def source(self):
        path = os.path.join(REPO, "evals", "swebench", "score_one.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_output_key_carries_the_instance(self):
        self.assertIn('existing[f"{instance_id}::{arm}"] = result',
                      self.source())

    def test_the_arm_only_key_is_gone(self):
        self.assertNotIn("existing[arm] = result", self.source())

    def test_two_arms_on_two_instances_are_four_distinct_keys(self):
        keys = {f"{i}::{a}" for i in ("dj-1", "dj-2")
                for a in ("A_claude", "B_codex")}
        self.assertEqual(len(keys), 4)


class TheCalibrationIsWriteOnce(unittest.TestCase):
    """A ceiling rewritten after arms are scored against it is a fitted ceiling."""

    def test_the_driver_refuses_to_overwrite(self):
        path = os.path.join(REPO, "evals", "swebench", "calibrate_one.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("refusing to", source)
        self.assertIn("if os.path.exists(out):", source)


if __name__ == "__main__":
    unittest.main()
