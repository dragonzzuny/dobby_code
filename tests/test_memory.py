import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.memory import MemoryStore, MemoryError_


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = MemoryStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_authority_rule_blocks_unverified_supersede(self):
        old = self.m.add("semantic", "service A exposes 12 endpoints",
                         verification="verified", source="census run")
        with self.assertRaises(MemoryError_):
            self.m.add("semantic", "service A exposes 7 endpoints",
                       verification="unverified", supersedes=old["id"])

    def test_verified_supersede_hides_old(self):
        old = self.m.add("semantic", "count=1", verification="verified")
        new = self.m.add("semantic", "count=2", verification="verified",
                         supersedes=old["id"])
        alive = self.m.recall("semantic", "count", k=10)
        ids = [i["id"] for i in alive]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old["id"], ids)
        # audit trail retained on disk
        self.assertEqual(self.m.get("semantic", old["id"])["superseded_by"],
                         new["id"])

    def test_episodic_surface_cap(self):
        for i in range(10):
            self.m.add("episodic", f"lesson {i}", verification="verified")
        self.assertLessEqual(len(self.m.recall("episodic", "lesson")), 3)

    def test_verified_ranks_above_relevant_unverified(self):
        self.m.add("semantic", "build root is the nested app dir",
                   verification="verified")
        self.m.add("semantic", "build root guess guess build root guess",
                   verification="unverified")
        top = self.m.recall("semantic", "build root", k=1)[0]
        self.assertEqual(top["verification"], "verified")

    def test_expiry_compact(self):
        self.m.add("working", "temp note", expires_days=-1)  # already expired
        self.m.add("working", "fresh note")
        res = self.m.compact("working")
        self.assertEqual(res["after"], 1)

    def test_unknown_kind_rejected(self):
        with self.assertRaises(MemoryError_):
            self.m.add("magic", "x")


if __name__ == "__main__":
    unittest.main()
