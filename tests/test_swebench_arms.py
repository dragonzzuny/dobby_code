"""The five-arm runner's decisions, tested without a provider or a network.

Everything here is the part that decides WHAT gets run and how a row is scored
when it comes back. The provider calls themselves are not simulated: the runner
delegates those to `providers.run`, which has its own tests.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "evals", "swebench"))

import runner_arms as R  # noqa: E402

CHECK = R.CHECK_SCRIPT


def instance(instance_id="x__y-1", patch="", **kw):
    base = {"instance_id": instance_id, "repo": "x/y", "patch": patch,
            "problem_statement": "something is wrong", "base_commit": "0" * 40,
            "difficulty": "<15 min fix"}
    base.update(kw)
    return base


def patch_for(*paths):
    return "".join(f"--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-x\n+y\n" for p in paths)


class TestArms(unittest.TestCase):
    def test_fable_is_the_claude_cli_with_only_the_model_changed(self):
        """Otherwise a difference between the rows is not about the model."""
        self.assertEqual(R.SOLO[R.ARM_CLAUDE][0], R.SOLO[R.ARM_FABLE][0])
        self.assertIsNone(R.SOLO[R.ARM_CLAUDE][1])
        self.assertEqual(R.SOLO[R.ARM_FABLE][1], "claude-fable-5")

    def test_every_arm_is_either_solo_or_the_loop(self):
        self.assertEqual(set(R.ARMS), set(R.SOLO) | {R.ARM_DOBBY})


class TestSelect(unittest.TestCase):
    def _pool(self):
        return [instance("one", patch_for("a.py")),
                instance("two", patch_for("a.py", "b.py")),
                instance("three", patch_for("a.py", "b.py", "c.py")),
                instance("four", patch_for("a.py", "b.py", "c.py", "d.py"))]

    def test_single_file_instances_are_excluded(self):
        """A one-file fix has nothing to decompose; the old corpus proved it."""
        got = R.select(self._pool(), limit=10)
        self.assertNotIn("one", [i["instance_id"] for i in got])
        self.assertEqual(len(got), 3)

    def test_the_widest_instances_come_first(self):
        got = R.select(self._pool(), limit=2)
        self.assertEqual([i["instance_id"] for i in got], ["four", "three"])

    def test_the_threshold_is_adjustable(self):
        got = R.select(self._pool(), limit=10, min_gold_files=4)
        self.assertEqual([i["instance_id"] for i in got], ["four"])

    def test_ties_break_on_id_so_a_rerun_picks_the_same_set(self):
        pool = [instance("b", patch_for("a.py", "b.py")),
                instance("a", patch_for("a.py", "b.py"))]
        self.assertEqual([i["instance_id"] for i in R.select(pool, limit=2)],
                         ["a", "b"])
        self.assertEqual([i["instance_id"] for i in R.select(pool[::-1], limit=2)],
                         ["a", "b"])


class TestMarkVoid(unittest.TestCase):
    def test_an_arm_that_made_no_call_is_void_not_a_zero(self):
        rows = R.mark_void([{"arm": "A", "calls_total": 0}])
        self.assertTrue(rows[0]["void"])
        self.assertIn("never ran", rows[0]["note"])

    def test_an_arm_that_called_is_left_alone(self):
        rows = R.mark_void([{"arm": "A", "calls_total": 2, "note": "fine"}])
        self.assertIsNone(rows[0].get("void"))
        self.assertEqual(rows[0]["note"], "fine")

    def test_an_already_void_row_keeps_its_own_reason(self):
        rows = R.mark_void([{"arm": "A", "void": True, "note": "isolation"}])
        self.assertEqual(rows[0]["note"], "isolation")


class TestHarnessArtefacts(unittest.TestCase):
    """The measuring apparatus must not appear in the measurement.

    Regression for the first pilot: A_claude edited exactly the five gold files
    and scored precision 0.556 because four `.omc/` session files counted as
    scope violations, and D_dobby's plan was rejected with a ReadOnlyViolation
    whose whole evidence was five `.omc/` files.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = self.dir.name
        subprocess.run(["git", "-C", self.repo, "init", "-q"],
                       capture_output=True)
        with open(os.path.join(self.repo, "src.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "base"],
                       capture_output=True)
        R._exclude_harness_artefacts(self.repo)

    def tearDown(self):
        self.dir.cleanup()

    def _porcelain(self):
        proc = subprocess.run(
            ["git", "-C", self.repo, "status", "--porcelain", "-uall"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return (proc.stdout or "").strip()

    def _drop(self, rel):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")

    def test_every_declared_artefact_directory_is_invisible(self):
        for entry in R.HARNESS_ARTEFACTS:
            self._drop(entry + "deep/state.json")
        self.assertEqual(self._porcelain(), "",
                         "harness state moved the tree and would be scored as "
                         "the agent's work")

    def test_a_real_source_edit_is_still_seen(self):
        self._drop(".omc/state.json")
        with open(os.path.join(self.repo, "src.py"), "a", encoding="utf-8") as fh:
            fh.write("y = 2\n")
        self.assertIn("src.py", self._porcelain())

    def test_the_exclude_file_is_local_and_uncommitted(self):
        """`.git/info/exclude` never enters the tree the instance is scored on."""
        self.assertTrue(os.path.exists(
            os.path.join(self.repo, ".git", "info", "exclude")))
        self.assertNotIn("exclude", self._porcelain())


class TestSyntaxCheck(unittest.TestCase):
    """The D arm's acceptance check, which must not pass an arm that idled."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = self.dir.name
        for args in (("init", "-q"), ("add", "-A")):
            subprocess.run(["git", "-C", self.repo, *args], capture_output=True)
        self._write("kept.py", "x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"],
                       capture_output=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "base"],
                       capture_output=True)

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, name, text):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def _run(self):
        proc = subprocess.run([sys.executable, CHECK], cwd=self.repo,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)
        return proc.returncode, (proc.stdout or "")

    def test_an_untouched_tree_fails(self):
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("nothing was done", out)

    def test_a_valid_edit_passes(self):
        self._write("kept.py", "x = 2\n")
        rc, out = self._run()
        self.assertEqual(rc, 0, out)
        self.assertIn("parse", out)

    def test_a_new_file_counts_as_a_change(self):
        self._write("added.py", "y = 3\n")
        rc, _ = self._run()
        self.assertEqual(rc, 0)

    def test_a_syntax_error_fails_and_names_the_line(self):
        self._write("kept.py", "x = 1\ndef broken(:\n")
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("kept.py:2", out)

    def test_a_non_python_change_alone_is_not_a_change(self):
        """Editing a README is not doing the item, and must not pass the gate."""
        self._write("README.md", "hello\n")
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("nothing was done", out)


if __name__ == "__main__":
    unittest.main()
