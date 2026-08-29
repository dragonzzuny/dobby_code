"""`dobby runtime levels`: what a plan could prove, before anyone pays for it.

The ladder in `runtime/contracts.py` grades a contract. This is its consumer,
and without one it would be another `claude_quota` -- 392 lines nothing
imported -- or another `style.py`, which had a single caller in the whole
repository and so could describe generated prose without ever stopping any.

What it answers: on `django__django-11138` the dobby arm localised all three
gold files, wrote a patch, broke four `timezones` tests and PROMOTED. Its one
acceptance check asked whether the changed files still parse. Nothing was
bypassed and the report was accurate; the check was rung 2 and the defect was
rung 4. A run tells you that after you have paid for it. This tells you first.

Two properties under test, and the second is the one that nearly shipped wrong:

- `--require` is demanded only of nodes that CHANGE something. A planning node
  returning JSON has nothing to prove end-to-end, and demanding it there would
  teach people to pass a flag they do not believe.
- It EXITS non-zero. The first version returned 1 from the handler, `main`
  called `args.fn(args)` and dropped the value, and the command printed the
  node it had caught and exited 0 -- a gate that describes instead of stopping,
  which is the defect the rest of this module exists to catch.
"""

import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime.contracts import (ArtifactContract, LOCAL_WRITE,  # noqa: E402
                                     V_BEHAVIOR, V_EXISTENCE, level_name)


def levels(*args):
    """Run the command. Returns (exit_code, parsed_json)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "dobby.cli", "runtime", "levels", *args],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=180)
    try:
        parsed = json.loads(proc.stdout)
    except ValueError:
        parsed = {"_stdout": proc.stdout, "_stderr": proc.stderr}
    return proc.returncode, parsed


class ItGradesTheShape(unittest.TestCase):
    def test_it_works_without_naming_a_worker(self):
        """`default_graph` refuses a graph with no worker, correctly. But the
        SHAPE of the contracts does not depend on who runs them, and refusing
        here would mean you can only ask what your gate proves after deciding
        who to pay."""
        code, out = levels()
        self.assertEqual(code, 0, out)
        self.assertIn("graph", out)

    def test_every_node_reports_a_rung(self):
        _, out = levels()
        for row in out["graph"]:
            self.assertIn(row["verifiable_at"],
                          {"NONE", "EXISTENCE", "STRUCTURE", "CONTRACT",
                           "BEHAVIOR"}, row)

    def test_it_says_which_declaration_bought_the_rung(self):
        """A number nobody can argue with is not the point."""
        _, out = levels()
        plan = next(r for r in out["graph"] if r["node"] == "plan")
        self.assertTrue(any("output_schema" in b for b in plan["from"]))

    def test_the_writing_node_is_identified_as_writing(self):
        _, out = levels()
        execute = next(r for r in out["graph"] if r["node"] == "execute")
        self.assertTrue(execute["changes_things"])
        self.assertEqual(execute["side_effect_class"], LOCAL_WRITE)


class TheFloorStops(unittest.TestCase):
    def test_a_floor_the_writing_node_cannot_reach_exits_one(self):
        """The default graph's `execute` node reaches EXISTENCE: it declares a
        LOCAL_WRITE and nothing else. That is the 11138 shape."""
        code, out = levels("--require", "BEHAVIOR")
        self.assertEqual(code, 1, out)
        self.assertEqual([r["node"] for r in out["below_the_floor"]],
                         ["execute"])
        self.assertEqual(out["required_of_writing_nodes"], "BEHAVIOR")

    def test_a_floor_it_does_reach_exits_zero(self):
        code, out = levels("--require", "EXISTENCE")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["below_the_floor"], [])

    def test_no_floor_never_fails(self):
        """The default has to stay silent, or turning this on would break every
        existing caller of a command that did not exist yesterday."""
        code, out = levels()
        self.assertEqual(code, 0)
        self.assertIsNone(out["required_of_writing_nodes"])

    def test_an_unknown_level_is_refused_rather_than_ignored(self):
        code, out = levels("--require", "VERY-STRONG")
        self.assertEqual(code, 2, out)
        self.assertIn("expected", out)

    def test_the_floor_is_not_demanded_of_non_writing_nodes(self):
        """`plan`, `verify` and `report` reach STRUCTURE and are never listed,
        even under a BEHAVIOR floor."""
        _, out = levels("--require", "BEHAVIOR")
        listed = {r["node"] for r in out["below_the_floor"]}
        self.assertNotIn("plan", listed)
        self.assertNotIn("report", listed)


class TheFloorCanActuallyBeSatisfied(unittest.TestCase):
    """The defect this class exists for: the gate always failed.

    The default graph puts the acceptance checks on `verify` and the writing on
    `execute`, so the first version of `--require BEHAVIOR` failed no matter
    which test suite the caller declared -- the floor was asked of a node that
    structurally cannot carry a check. Every case had been tested EXCEPT the one
    that matters, whether a caller doing everything right can pass. A gate that
    always fires teaches people to stop passing the flag, which ends in the same
    place as a gate that never fires.

    Two rules now decide it, and both are visible in the output rather than
    inferred by the reader:

    - a dependent's ACCEPTANCE CHECKS grade the node it depends on, because a
      shell command runs against the tree that node changed;
    - a dependent's schema and grounding do NOT, because they grade that
      dependent's own payload.
    """

    def test_a_declared_suite_downstream_satisfies_the_floor(self):
        code, out = levels("--check", "pytest -q", "--checks-at", "BEHAVIOR",
                           "--require", "BEHAVIOR")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["below_the_floor"], [])

    def test_and_the_output_says_which_node_did_the_grading(self):
        """Otherwise the pass reads as magic.

        `execute` is EXISTENCE on its own here: with no `--execute` command it
        carries an empty schema and a LOCAL_WRITE, and nothing else. The whole
        distance to BEHAVIOR is bought by a node downstream of it, and the row
        has to name which one.
        """
        _, out = levels("--check", "pytest -q", "--checks-at", "BEHAVIOR")
        execute = next(r for r in out["graph"] if r["node"] == "execute")
        self.assertEqual(execute["verifiable_at"], "EXISTENCE")
        self.assertEqual(execute["effective_at"], "BEHAVIOR")
        self.assertEqual(execute["checked_downstream_by"],
                         {"verify": "BEHAVIOR"})

    def test_a_deterministic_execute_command_raises_its_own_rung(self):
        """With `--execute` the node gets a test_report schema of its own, so
        it is STRUCTURE before anything downstream is counted. Kept as its own
        case because the previous test's numbers came from a run WITH this flag
        and were pasted into a run without it."""
        _, out = levels("--execute", "make build")
        execute = next(r for r in out["graph"] if r["node"] == "execute")
        self.assertEqual(execute["verifiable_at"], "STRUCTURE")

    def test_an_undeclared_suite_does_not_satisfy_it(self):
        """The same `pytest -q`, with nobody saying what it reaches. This is the
        case that has to keep failing, or `--checks-at` is decoration."""
        code, out = levels("--check", "pytest -q", "--require", "BEHAVIOR")
        self.assertEqual(code, 1, out)
        execute = next(r for r in out["graph"] if r["node"] == "execute")
        self.assertEqual(execute["checked_downstream_by"],
                         {"verify": "EXISTENCE"})

    def test_no_checks_at_all_still_fails(self):
        code, _ = levels("--require", "BEHAVIOR")
        self.assertEqual(code, 1)

    def test_a_downstream_schema_does_not_lift_an_upstream_node(self):
        """`report` declares a schema and prose_at and no acceptance check, so
        it grades itself and contributes nothing upstream."""
        _, out = levels()
        execute = next(r for r in out["graph"] if r["node"] == "execute")
        self.assertNotIn("report", execute["checked_downstream_by"])

    def test_a_bad_checks_at_is_refused_by_name(self):
        code, out = levels("--check", "x", "--checks-at", "HUGE")
        self.assertEqual(code, 2, out)
        self.assertIn("--checks-at", out["error"])

    def test_effective_is_never_below_own(self):
        _, out = levels("--check", "pytest -q", "--checks-at", "BEHAVIOR")
        order = ["NONE", "EXISTENCE", "STRUCTURE", "CONTRACT", "BEHAVIOR"]
        for row in out["graph"]:
            self.assertGreaterEqual(order.index(row["effective_at"]),
                                    order.index(row["verifiable_at"]), row)


class TheLadderItReads(unittest.TestCase):
    """The command's answer must match the contract's own property."""

    def test_a_write_only_contract_is_existence(self):
        self.assertEqual(
            ArtifactContract(side_effect_class=LOCAL_WRITE).declared_level,
            V_EXISTENCE)

    def test_declaring_the_check_lifts_it(self):
        contract = ArtifactContract(side_effect_class=LOCAL_WRITE,
                                    acceptance_checks=["pytest -q"],
                                    checks_at=V_BEHAVIOR)
        self.assertEqual(contract.declared_level, V_BEHAVIOR)
        self.assertEqual(level_name(contract.declared_level), "BEHAVIOR")


class ARealProjectWorkOrder(unittest.TestCase):
    """`dobby project levels`: the graph that will actually run.

    `runtime levels` grades the default four-node shape. A project runs what
    `workorder.choose_graph` picks -- the compiled plan when there is one, the
    generic shape otherwise -- and the answer differs between them. Grading a
    shape that resembles the one somebody will run is how a report ends up true
    of a graph nobody executed.

    Writing this found a defect of exactly the kind it is for. `WorkItem`
    gained `checks_at` and it was threaded into `compile_graph`, which is the
    PLANNED path. The five `make_graph` call sites that produce the GENERIC
    shape passed `acceptance_checks` and not `checks_at`, so an item declaring
    BEHAVIOR ran a verify node at EXISTENCE. The command reported
    `item_checks_at=BEHAVIOR` beside `effective_at=EXISTENCE` on its first run,
    which is the disagreement it exists to surface, and it surfaced its own.
    """

    ITEMS = {"items": [
        {"work_item_id": "W1", "title": "weakly checked",
         "outcome": "make the endpoint paginate",
         "acceptance_checks": ["python -c \"import sys; sys.exit(0)\""]},
        {"work_item_id": "W2", "title": "strongly checked",
         "outcome": "support ?sort=",
         "acceptance_checks": ["python -m unittest discover -s tests"],
         "checks_at": 4}]}

    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile

        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.root)
        with open(os.path.join(cls.root, "app.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("x = 1\n")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        for argv in (["git", "init", "-q", "."],
                     ["git", "config", "user.email", "t@t"],
                     ["git", "config", "user.name", "t"],
                     ["git", "add", "-A"],
                     ["git", "commit", "-qm", "init"]):
            subprocess.run(argv, cwd=cls.root, capture_output=True, text=True,
                           env=env, timeout=120)
        cls.items_path = os.path.join(cls.tmp, "items.json")
        with open(cls.items_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cls.ITEMS, fh, ensure_ascii=False)
        cls._cleanup = lambda: shutil.rmtree(cls.tmp, ignore_errors=True)

        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "project", "init",
             "--repo", cls.root, "--root", cls.root,
             "--items", cls.items_path, "--no-baseline",
             "--smoke", 'python -c "import sys; sys.exit(0)"'],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=600)
        assert proc.returncode == 0, proc.stderr

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    def levels(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "project", "levels",
             "--repo", self.root, *args],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=600)
        try:
            return proc.returncode, json.loads(proc.stdout)
        except ValueError:
            return proc.returncode, {"_stdout": proc.stdout,
                                     "_stderr": proc.stderr}

    def item(self, out, work_item_id):
        return next(i for i in out["items"]
                    if i["work_item_id"] == work_item_id)

    def writing_node(self, item):
        return next(r for r in item["graph"] if r["changes_things"])

    def test_it_reports_every_open_item(self):
        code, out = self.levels()
        self.assertEqual(code, 0, out)
        self.assertEqual({i["work_item_id"] for i in out["items"]},
                         {"W1", "W2"})

    def test_it_names_the_shape_that_would_run(self):
        _, out = self.levels()
        for item in out["items"]:
            self.assertTrue(item["shape"], item)

    def test_the_items_declaration_reaches_the_graph(self):
        """The defect. `item_checks_at` and the graph used to disagree."""
        _, out = self.levels()
        strong = self.item(out, "W2")
        self.assertEqual(strong["item_checks_at"], "BEHAVIOR")
        self.assertEqual(self.writing_node(strong)["effective_at"], "BEHAVIOR")

    def test_a_weakly_checked_item_stays_weak(self):
        _, out = self.levels()
        weak = self.item(out, "W1")
        self.assertEqual(weak["item_checks_at"], "EXISTENCE")
        self.assertEqual(self.writing_node(weak)["effective_at"], "EXISTENCE")

    def test_the_floor_separates_the_two_items(self):
        code, out = self.levels("--require", "BEHAVIOR")
        self.assertEqual(code, 1, out)
        self.assertEqual([b["work_item_id"] for b in out["below_the_floor"]],
                         ["W1"])

    def test_a_floor_both_reach_exits_zero(self):
        code, out = self.levels("--require", "EXISTENCE")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["below_the_floor"], [])

    def test_no_floor_never_fails(self):
        code, out = self.levels()
        self.assertEqual(code, 0)
        self.assertIsNone(out["required_of_writing_nodes"])

    def test_an_unknown_level_is_refused(self):
        code, out = self.levels("--require", "VERY-STRONG")
        self.assertEqual(code, 2, out)
        self.assertIn("expected", out)


if __name__ == "__main__":
    unittest.main()
