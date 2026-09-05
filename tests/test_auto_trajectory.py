"""The improvement loop is fed by asking, not by remembering to record.

`record_evidence` and `handoff` are the only live path that writes a
trajectory -- `dobby exec-slice` is the other and it needs a scenario in
`slice_plans.json`. Measured on this repository's gateway log: 394 entries
across a month, nine capabilities registered, and those two invoked ZERO
times. So the corpus `friction-report` reads stopped on 2026-08-18 while the
log ran to 2026-09-04, and every downstream report was about a fortnight that
had ended.

A loop fed only by an explicit call nobody makes is not a loop. A context pack
request IS a task starting -- that is what its argument says -- so it is the
honest place to begin recording, and it now does.

This makes a read-looking call write a file, which is why `Gateway` grew a
`data` override and why `tests/test_mcp_server.py` now drives a copy. Without
that, a test run would seed the improvement corpus with its own noise and then
report on it -- which it did, once, before the isolation landed: one
trajectory in the real `.dobby` carrying `test_context_pack_routes`' task
string, deleted when found.

One trajectory per task. Asking twice about the same task appends rather than
starting a rival file; asking about a different one starts a new file. The
alternative -- a file per call -- would have turned this repository's 87
context-pack requests into 87 single-line corpora, which is a different way of
having no evidence.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

import dobby_mcp_server as M  # noqa: E402

from dobby.core.friction import friction_report  # noqa: E402


class GatewayCase(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir(os.path.join(REPO, ".dobby")):
            self.skipTest("no .dobby in this checkout")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "state-elsewhere")
        shutil.copytree(os.path.join(REPO, ".dobby"), self.data)
        self.gateway = M.Gateway(REPO, self.data)
        self.traj_dir = os.path.join(self.data, "state", "trajectories")

    def files(self):
        if not os.path.isdir(self.traj_dir):
            return []
        return sorted(f for f in os.listdir(self.traj_dir)
                      if f.endswith(".jsonl"))

    def events(self, name):
        with io.open(os.path.join(self.traj_dir, name), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def new_files(self, before):
        return [f for f in self.files() if f not in before]


class AskingForContextStartsTheRecord(GatewayCase):
    def test_a_context_pack_opens_a_trajectory(self):
        before = self.files()
        self.gateway.get_context_pack("write the migration script")
        self.assertEqual(len(self.new_files(before)), 1)

    def test_it_records_the_task_it_was_asked_about(self):
        before = self.files()
        self.gateway.get_context_pack("write the migration script")
        events = self.events(self.new_files(before)[0])
        self.assertEqual(events[0]["event"], "task_start")
        self.assertEqual(events[0]["task"], "write the migration script")

    def test_it_records_the_routing_decision_it_just_made(self):
        """The plan was computed either way; not recording it threw it away."""
        before = self.files()
        plan = self.gateway.get_context_pack("write the migration script")
        route = [e for e in self.events(self.new_files(before)[0])
                 if e["event"] == "route"]
        self.assertEqual(len(route), 1)
        self.assertEqual(route[0]["level"], plan["level"])
        self.assertEqual(route[0]["policies"], plan["policies"])

    def test_the_plan_returned_is_unchanged_by_the_recording(self):
        """A gateway that answers differently because it took notes is worse
        than one that takes none."""
        quiet = M.Gateway(REPO, self.data)
        quiet.get_context_pack("write the migration script")
        loud = self.gateway.get_context_pack("write the migration script")
        self.assertEqual(
            loud, M.Gateway(REPO, self.data).router.route(
                "write the migration script").to_dict())

    def test_asking_twice_about_one_task_appends_rather_than_forking(self):
        before = self.files()
        self.gateway.get_context_pack("write the migration script")
        self.gateway.get_context_pack("write the migration script")
        made = self.new_files(before)
        self.assertEqual(len(made), 1, "the same task started two corpora")
        self.assertEqual(
            len([e for e in self.events(made[0]) if e["event"] == "route"]), 2)

    def test_a_different_task_starts_a_different_trajectory(self):
        before = self.files()
        self.gateway.get_context_pack("write the migration script")
        self.gateway.get_context_pack("audit the retry policy")
        self.assertEqual(len(self.new_files(before)), 2)

    def test_the_audit_line_is_still_written(self):
        """The trajectory is additional evidence, not a replacement."""
        path = os.path.join(self.data, "state", "audit.jsonl")
        before = 0
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as fh:
                before = sum(1 for line in fh if line.strip())
        self.gateway.get_context_pack("write the migration script")
        with io.open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(rows[-1]["kind"], "context_pack")
        self.assertEqual(len(rows), before + 1)


class RecordEvidenceJoinsTheSameFile(GatewayCase):
    """It used to open a rival trajectory called `(mcp session)`."""

    def test_evidence_lands_in_the_trajectory_the_pack_opened(self):
        before = self.files()
        self.gateway.get_context_pack("write the migration script")
        self.gateway.invoke_capability(
            "record_evidence", {"detail": "the schema has 12 columns"})
        made = self.new_files(before)
        self.assertEqual(len(made), 1)
        kinds = [e["event"] for e in self.events(made[0])]
        self.assertEqual(kinds, ["task_start", "route", "evidence"])

    def test_evidence_without_a_pack_still_opens_one(self):
        """The old path has to keep working for a client that never asks."""
        before = self.files()
        self.gateway.invoke_capability("record_evidence", {"detail": "x"})
        self.assertEqual(len(self.new_files(before)), 1)


class TheDataOverrideIsRealIsolation(GatewayCase):
    def test_the_default_is_still_the_repository(self):
        """Omitting the override must resolve exactly where it always did.

        Asserted on a gateway built from the real repository rather than an
        empty directory: `Gateway.__init__` loads the ontology, so a directory
        with no `.dobby` raises before `self.data` can be read, and the test
        would be about that instead.
        """
        self.assertEqual(M.Gateway(REPO).data,
                         os.path.join(os.path.abspath(REPO), ".dobby"))

    def test_an_override_is_used_for_both_stores(self):
        self.assertEqual(self.gateway.data, os.path.abspath(self.data))
        self.assertTrue(self.gateway.audit_path.startswith(
            os.path.abspath(self.data)))

    def test_nothing_is_written_to_the_real_repository(self):
        """The assertion the earlier pollution earns."""
        real = os.path.join(REPO, ".dobby", "state", "trajectories")
        before = sorted(os.listdir(real)) if os.path.isdir(real) else []
        self.gateway.get_context_pack("write the migration script")
        self.gateway.invoke_capability("record_evidence", {"detail": "x"})
        after = sorted(os.listdir(real)) if os.path.isdir(real) else []
        self.assertEqual(before, after)


class RecordingIsSecondaryToAnswering(GatewayCase):
    """The regression the recording itself introduced, and its guard.

    Measured with a file sitting where `state/trajectories` has to be a
    directory -- the shape a read-only mount or a full disk produces:

        before   FileExistsError out of get_context_pack, no plan at all
        after    the plan, and `record_failed` on the audit

    `get_context_pack` is the first call every client makes. Losing the
    recording costs a line in the improvement corpus. Losing the answer costs
    the session.
    """

    def block_recording(self):
        """Make the trajectory directory impossible to create."""
        shutil.rmtree(self.traj_dir, ignore_errors=True)
        with io.open(self.traj_dir, "w", encoding="utf-8") as fh:
            fh.write("not a directory")

    def audit_rows(self):
        """Only the rows this test wrote.

        The copied state carries the real repository's audit history -- 40
        result lines at the time of writing -- so an unfiltered count is a
        count of somebody else's calls.
        """
        path = os.path.join(self.data, "state", "audit.jsonl")
        if not os.path.exists(path):
            return []
        with io.open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return rows[self.mark:]

    def setUp(self):
        super().setUp()
        path = os.path.join(self.data, "state", "audit.jsonl")
        self.mark = 0
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as fh:
                self.mark = sum(1 for line in fh if line.strip())

    def test_the_plan_still_comes_back(self):
        self.block_recording()
        plan = self.gateway.get_context_pack("write the migration script")
        self.assertIn("level", plan)
        self.assertIn("policies", plan)

    def test_the_plan_is_the_same_one_it_would_have_given(self):
        self.block_recording()
        got = self.gateway.get_context_pack("write the migration script")
        self.assertEqual(got, M.Gateway(REPO, self.data).router.route(
            "write the migration script").to_dict())

    def test_the_failure_is_recorded_rather_than_swallowed(self):
        """A corpus that stops growing should have a reason on the record."""
        self.block_recording()
        self.gateway.get_context_pack("write the migration script")
        failed = [r for r in self.audit_rows() if r["kind"] == "record_failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["error"])

    def test_a_later_call_tries_again_rather_than_reusing_a_broken_one(self):
        self.block_recording()
        self.gateway.get_context_pack("write the migration script")
        self.assertIsNone(self.gateway.trajectory)
        os.remove(self.traj_dir)
        self.gateway.get_context_pack("write the migration script")
        self.assertIsNotNone(self.gateway.trajectory)
        self.assertEqual(len(self.files()), 1)

    def test_evidence_recording_is_not_silently_guarded(self):
        """record_evidence is a call whose ONLY purpose is to record. Failing
        quietly there would report success for work not done -- the guard on
        `get_context_pack` is for a call that has another job."""
        self.block_recording()
        result = self.gateway.invoke_capability("record_evidence",
                                                {"detail": "x"})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)

    def test_a_raising_capability_is_recorded_as_a_failure(self):
        """It used to leave the `invoke` line and nothing else -- the same
        intent-without-outcome shape the result line exists to fix, on the
        one failure most worth learning from."""
        self.block_recording()
        self.gateway.invoke_capability("record_evidence", {"detail": "x"})
        results = [r for r in self.audit_rows() if r["kind"] == "result"]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("FileExistsError", results[0]["error"])

    def test_a_raising_capability_reaches_the_friction_report(self):
        self.block_recording()
        self.gateway.invoke_capability("record_evidence", {"detail": "x"})
        failures = friction_report(self.data)["capability_failures"]
        self.assertEqual([f["capability"] for f in failures],
                         ["record_evidence"])


class TheLoopStopsBeingUnfed(GatewayCase):
    """End to end: the report that said UNFED now has something to read."""

    def test_a_pack_and_a_handoff_feed_the_friction_report(self):
        self.assertTrue(friction_report(self.data)["unfed"],
                        "the copied state should start starving")
        self.gateway.get_context_pack("write the migration script")
        self.gateway.invoke_capability(
            "record_evidence", {"detail": "the schema has 12 columns"})
        self.gateway.invoke_capability(
            "handoff", {"done": ["read the schema"], "remaining": [],
                        "decisions": [], "evidence": ["12 columns"],
                        "next_steps": []})
        out = friction_report(self.data)
        self.assertFalse(out["unfed"], out["verdict"])
        self.assertFalse(out["stale"], out["window"])

    def test_the_window_now_reaches_today(self):
        self.gateway.get_context_pack("write the migration script")
        out = friction_report(self.data)
        self.assertEqual(out["window"]["days_since_newest"], 0)


if __name__ == "__main__":
    unittest.main()
