"""Tests for mechanisms adopted from community orchestrators (v2.3):
OpenClaw requires-gating + origin pinning, OMC friction report,
claude-octopus runner status ledger."""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.skills import SkillRegistry, SkillError, check_requires
from dobby.core.friction import friction_report
from dobby.core.trajectory import Trajectory

META = {"name": "gated-skill", "description": "d",
        "applicable_when": ["demo"], "not_applicable_when": ["never"],
        "inputs": ["x"], "outputs": ["y"], "validation_commands": ["true"],
        "version": "0.1",
        "provenance": {"source": "test", "method": "curated",
                       "date": "2026-07-12", "confidence": "verified"}}


class TestRequiresGating(unittest.TestCase):
    def test_check_requires(self):
        self.assertTrue(check_requires({})[0])
        self.assertTrue(check_requires({"bins": ["python3"]})[0])
        ok, why = check_requires({"bins": ["definitely-not-a-real-bin-xyz"]})
        self.assertFalse(ok)
        self.assertIn("missing binary", why)
        self.assertTrue(check_requires({"any_bins": ["nope-xyz", "python3"]})[0])
        self.assertFalse(check_requires({"env": ["NOT_A_REAL_ENV_VAR_XYZ"]})[0])
        self.assertFalse(check_requires({"os": ["win32-only-xyz"]})[0])

    def test_index_filters_ineligible(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = SkillRegistry(os.path.join(tmp.name, "skills.json"))
        meta = dict(META)
        meta["requires"] = {"bins": ["definitely-not-a-real-bin-xyz"]}
        s = r.register_candidate(meta, "a")
        s["state"] = "active"  # place directly for the gating test
        self.assertEqual(r.index(), [])
        self.assertEqual(len(r.index(runtime_gate=False)), 1)
        snap = r.snapshot()
        self.assertEqual(snap["skills"], [])
        self.assertIn("taken", snap)


class TestOriginPinning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "skills", "demo"))
        self.body = os.path.join("skills", "demo", "SKILL.md")
        with open(os.path.join(self.tmp.name, self.body), "w") as f:
            f.write("# demo skill\nsteps...\n")
        self.r = SkillRegistry(os.path.join(self.tmp.name, "skills.json"))
        meta = dict(META, path=self.body)
        self.r.register_candidate(meta, "a")

    def test_pin_and_verify(self):
        self.r.pin_origin("gated-skill", repo_root=self.tmp.name)
        ok, why = self.r.verify_origin("gated-skill", repo_root=self.tmp.name)
        self.assertTrue(ok, why)

    def test_tamper_detected(self):
        self.r.pin_origin("gated-skill", repo_root=self.tmp.name)
        with open(os.path.join(self.tmp.name, self.body), "a") as f:
            f.write("rm -rf / # injected step\n")
        ok, why = self.r.verify_origin("gated-skill", repo_root=self.tmp.name)
        self.assertFalse(ok)
        self.assertIn("differs", why)

    def test_unpinned_reports_missing(self):
        ok, why = self.r.verify_origin("gated-skill", repo_root=self.tmp.name)
        self.assertFalse(ok)
        self.assertIn("no pinned origin", why)

    def test_factory_skills_are_pinned_and_intact(self):
        real = SkillRegistry(os.path.join(REPO, ".dobby", "registry",
                                          "skills.json"))
        for entry in real.index(runtime_gate=False):
            ok, why = real.verify_origin(entry["name"], repo_root=REPO)
            self.assertTrue(ok, f"{entry['name']}: {why}")


class TestFrictionReport(unittest.TestCase):
    def test_signals_detected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Trajectory(tmp.name, "demo")
        for _ in range(3):
            t.append("execute", {"command": "pytest -x", "exit_code": 1})
        t.append("evidence", {"detail": "x" * 9000})
        t.record_failure("retrieval", "miss", "keyword gap", "e")
        rep = friction_report(tmp.name)
        self.assertEqual(rep["tasks_scanned"], 1)
        self.assertEqual(rep["repeated_commands"][0]["times"], 3)
        self.assertTrue(rep["consecutive_repeats"])
        self.assertTrue(rep["oversized_events"])
        self.assertEqual(rep["failure_hotspots"], {"retrieval": 1})
        self.assertIn(os.path.basename(t.path), rep["handoff_gaps"])

    def test_clean_when_handoff_written(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Trajectory(tmp.name, "demo")
        t.append("evidence", {"detail": "small"})
        t.handoff(["a"], [], [], ["e"], ["n"])
        rep = friction_report(tmp.name)
        self.assertEqual(rep["handoff_gaps"], [])


class TestRunnerLedger(unittest.TestCase):
    def test_handoff_includes_runner_status(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t = Trajectory(tmp.name, "demo")
        p = t.handoff(["a"], [], [], ["e"], ["n"],
                      runners=[{"name": "validator", "status": "ok"},
                               {"name": "judge-model", "status": "degraded",
                                "note": "NOT RUN — no LLM here"}])
        content = open(p, encoding="utf-8").read()
        self.assertIn("## Runners (status ledger)", content)
        self.assertIn("judge-model: degraded — NOT RUN", content)


if __name__ == "__main__":
    unittest.main()
