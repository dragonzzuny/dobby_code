"""The audit log recorded what was attempted and never what happened.

`mcp/dobby_mcp_server.py` says at the top that "every call is audit-logged",
and it wrote one line per call -- BEFORE making it. Measured on this
repository's own audit, which is the only evidence store here that is still
growing:

    324 entries, kinds {'invoke': 243, 'context_pack': 81}
    keys ['args', 'id', 'kind', 'level', 't', 'task']
    entries recording success or failure: 0

A capability that failed left a line indistinguishable from one that worked.
Nothing downstream could learn anything from a file that records only
intentions -- and this repository has five self-improvement tools whose whole
job is to learn from recorded history.

A `result` line now follows every invocation with `ok` and, when it failed, the
error. The result ITSELF is not logged, only its shape: a capability's output
can be a whole file and this log is append-only.

The related finding, fixed in the same pass: `flywheel.report` said "nothing
has failed twice the same way -- check the run count before reading it as a
healthy system" and did not carry the run count, while the walk that produced
the note had just read every run there was. On this repository the answer was
ZERO runs, so the honest reading was "nothing to learn from yet" and the report
made the reader go and find that out.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

import dobby_mcp_server as M  # noqa: E402

from dobby.runtime.flywheel import report as flywheel_report  # noqa: E402
from dobby.runtime.store import RunStore  # noqa: E402


class GatewayCase(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir(os.path.join(REPO, ".dobby")):
            self.skipTest("no .dobby in this checkout")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(os.path.join(REPO, ".dobby"),
                        os.path.join(self.tmp, ".dobby"))
        self.gateway = M.Gateway(self.tmp)
        self.audit = os.path.join(self.tmp, ".dobby", "state", "audit.jsonl")
        self.mark = self.count()

    def count(self):
        if not os.path.exists(self.audit):
            return 0
        with io.open(self.audit, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def new_lines(self):
        """The lines written since `setUp`, or none if there is no log.

        The absence guard is not defensive padding. Without it this class
        passed here and failed on every clean checkout: a developer machine
        has an `audit.jsonl` with real history in it, and CI has a repository
        where nothing has ever called the gateway. The one test that asserts
        NOTHING was written is exactly the one that never creates the file, so
        it was the one that broke -- an assertion about an empty log that
        could only run where the log was not empty.
        """
        if not os.path.exists(self.audit):
            return []
        with io.open(self.audit, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return rows[self.mark:]

    def a_capability_needing_no_args(self):
        for cid, cap in self.gateway.capabilities.items():
            keys = [k for k in re.findall(r"\{(\w+)\}",
                                          cap.get("command_template", ""))
                    if k != "python"]
            if not keys:
                return cid
        self.skipTest("every capability here needs arguments")

    def a_capability_needing_args(self):
        for cid, cap in self.gateway.capabilities.items():
            keys = [k for k in re.findall(r"\{(\w+)\}",
                                          cap.get("command_template", ""))
                    if k != "python"]
            if keys:
                return cid
        self.skipTest("no capability here takes arguments")


class EveryCallRecordsItsOutcome(GatewayCase):
    def test_a_successful_call_writes_a_result_line(self):
        self.gateway.invoke_capability(self.a_capability_needing_no_args(), {})
        kinds = [r["kind"] for r in self.new_lines()]
        self.assertIn("invoke", kinds)
        self.assertIn("result", kinds, "the outcome was not recorded")

    def test_the_result_line_says_it_succeeded(self):
        cid = self.a_capability_needing_no_args()
        self.gateway.invoke_capability(cid, {})
        result = next(r for r in self.new_lines() if r["kind"] == "result")
        self.assertEqual(result["id"], cid)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

    def test_a_failed_call_says_so_and_says_why(self):
        cid = self.a_capability_needing_args()
        self.gateway.invoke_capability(cid, {})          # arguments missing
        result = next(r for r in self.new_lines() if r["kind"] == "result")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"], "a failure with no reason recorded")

    def test_success_and_failure_are_distinguishable(self):
        """The whole point. Before this they were the same line."""
        self.gateway.invoke_capability(self.a_capability_needing_no_args(), {})
        self.gateway.invoke_capability(self.a_capability_needing_args(), {})
        outcomes = [r["ok"] for r in self.new_lines() if r["kind"] == "result"]
        self.assertEqual(sorted(outcomes), [False, True])

    def test_an_unknown_capability_is_not_logged_as_a_call(self):
        """It never ran, so there is no outcome to record."""
        self.gateway.invoke_capability("definitely_not_a_capability", {})
        self.assertEqual([r for r in self.new_lines()
                          if r["kind"] in ("invoke", "result")], [])

    def test_the_output_itself_is_not_written_to_the_log(self):
        """An append-only log is the wrong place for a capability's payload."""
        self.gateway.invoke_capability(self.a_capability_needing_no_args(), {})
        result = next(r for r in self.new_lines() if r["kind"] == "result")
        self.assertEqual(sorted(result), ["error", "id", "kind", "ok", "t"])


class AnEmptyFindingCarriesItsSampleSize(unittest.TestCase):
    """`flywheel.report` on a store with no runs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = RunStore(os.path.join(self.tmp, "d"))

    def test_the_report_says_how_many_runs_it_examined(self):
        out = flywheel_report(self.store, self.tmp)
        self.assertIn("runs_examined", out)
        self.assertEqual(out["runs_examined"], 0)

    def test_an_empty_store_is_not_reported_as_a_healthy_system(self):
        out = flywheel_report(self.store, self.tmp)
        self.assertIn("0 recorded run", out["note"])
        self.assertIn("not a finding about the system", out["note"])

    def test_the_note_names_the_number_rather_than_asking_for_it(self):
        """It used to say "check the run count" and not carry it."""
        out = flywheel_report(self.store, self.tmp)
        self.assertNotIn("check the run count", out["note"])

    def test_candidates_are_still_empty_and_still_say_so(self):
        out = flywheel_report(self.store, self.tmp)
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["min_occurrences"], 2)


if __name__ == "__main__":
    unittest.main()
