import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.trajectory import Trajectory
from pathlib import Path


class TestTrajectory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_persist_and_resume(self):
        t = Trajectory(self.tmp.name, "demo task")
        t.append("evidence", {"detail": "count=5"})
        t2 = Trajectory.resume(self.tmp.name, t.task_id)
        events = t2.events()
        self.assertEqual(events[0]["event"], "task_start")
        self.assertEqual(t2.task, "demo task")
        self.assertEqual(events[1]["detail"], "count=5")

    def test_failure_levels_enforced(self):
        t = Trajectory(self.tmp.name, "demo")
        with self.assertRaises(ValueError):
            t.record_failure("vibes", "s", "r", "e")
        rec = t.record_failure("retrieval", "missed node", "keyword gap",
                               "per_case score 0.2")
        self.assertEqual(rec["level"], "retrieval")

    def test_handoff_written_and_latest_found(self):
        t = Trajectory(self.tmp.name, "demo")
        p = t.handoff(done=["a"], remaining=["b"],
                      decisions=[{"what": "w", "why": "y"}],
                      evidence=["reports/x.md"], next_steps=["n"])
        self.assertTrue(os.path.exists(p))
        self.assertEqual(Trajectory.latest_handoff(self.tmp.name), p)
        content = Path(p).read_text(encoding="utf-8")
        for section in ("## Done", "## Remaining", "## Decisions",
                        "## Evidence", "## Next steps"):
            self.assertIn(section, content)


if __name__ == "__main__":
    unittest.main()
