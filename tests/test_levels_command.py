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


if __name__ == "__main__":
    unittest.main()
