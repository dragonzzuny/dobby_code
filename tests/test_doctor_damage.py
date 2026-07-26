"""`doctor` against damaged data — the command whose whole job is saying so.

Three defects, found by breaking each data file three ways (corrupt, empty,
missing) and running the one command that exists to report exactly that:

1. It CRASHED with a JSONDecodeError on a corrupt `config.json`. The diagnostic
   tool failed to diagnose.
2. It reported **"all checks pass"** with a corrupt knowledge graph, a corrupt
   policy book, and a corrupt skill registry, because the checks called
   `os.path.exists` and never parsed anything. That is the same
   existence-is-not-a-measurement mistake found in the installer's interpreter
   probe, the sandbox's unused path guard, and the worktree repo check.
3. It exited **0** while printing "1 check(s) failed", so a script could not act
   on the failure it had just been handed.

Fixing (3) naively would have broken CI: a runner has no agent CLIs installed,
which is a legitimate state and not a defect. Checks therefore carry a severity,
and only blocking ones set the exit code.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import child_env

#: Files whose damage must be reported as BLOCKING.
DATA_FILES = [
    "ontology.json",
    "config.json",
    os.path.join("knowledge", "kg.json"),
    os.path.join("policies", "policies.json"),
    os.path.join("registry", "skills.json"),
    os.path.join("registry", "capabilities.json"),
]

DAMAGE = {"corrupt": "{ this is not json", "empty": "", "missing": None}


def make_repo(*, drop_bootstrap: bool = False) -> str:
    """A minimal runnable copy of the kit."""
    d = tempfile.mkdtemp(prefix="dobby-doctor-")
    for sub in ("dobby", "mcp", "evals", "tests"):
        shutil.copytree(os.path.join(REPO, sub), os.path.join(d, sub))
    shutil.copytree(os.path.join(REPO, ".dobby"), os.path.join(d, ".dobby"))
    if drop_bootstrap:
        boot = os.path.join(d, ".dobby", "knowledge", "kg.bootstrap.json")
        if os.path.exists(boot):
            os.remove(boot)
    return d


def run_doctor(repo: str):
    proc = subprocess.run(
        [sys.executable, "-m", "dobby.cli", "doctor"], cwd=repo,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=child_env(), timeout=180)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = None
    return proc.returncode, payload, proc.stderr


class TestHealthyRepo(unittest.TestCase):
    def test_passes_and_exits_zero(self):
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        rc, payload, err = run_doctor(d)
        self.assertIsNotNone(payload, f"doctor emitted no JSON: {err[-300:]}")
        self.assertEqual(rc, 0, payload["verdict"])
        self.assertEqual(payload["verdict"], "all checks pass")


class TestDamageIsReportedNotCrashed(unittest.TestCase):
    """Every damage shape, every required file. 18 scenarios."""

    def test_no_scenario_crashes(self):
        crashes = []
        for rel in DATA_FILES:
            for kind, content in DAMAGE.items():
                d = make_repo()
                try:
                    path = os.path.join(d, ".dobby", rel)
                    if content is None:
                        os.remove(path)
                    else:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(content)
                    _, payload, err = run_doctor(d)
                    if payload is None or "Traceback" in err:
                        crashes.append(f"{rel}/{kind}: {err.strip()[-160:]}")
                finally:
                    shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(crashes, [], "\n".join(crashes))

    def test_every_damage_is_detected_and_named(self):
        misses = []
        for rel in DATA_FILES:
            for kind, content in DAMAGE.items():
                d = make_repo()
                try:
                    path = os.path.join(d, ".dobby", rel)
                    if content is None:
                        os.remove(path)
                    else:
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(content)
                    rc, payload, _ = run_doctor(d)
                    if payload is None:
                        misses.append(f"{rel}/{kind}: no output")
                        continue
                    named = any(rel in c for c in payload["blocking_failures"])
                    if rc == 0 or not named:
                        misses.append(
                            f"{rel}/{kind}: rc={rc} "
                            f"blocking={payload['blocking_failures']}")
                finally:
                    shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(misses, [], "\n".join(misses))

    def test_corrupt_config_does_not_crash_the_diagnostic(self):
        """The original failure: JSONDecodeError from `doctor` itself."""
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".dobby", "config.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ nope")
        rc, payload, err = run_doctor(d)
        self.assertIsNotNone(payload, f"doctor crashed: {err[-300:]}")
        self.assertNotIn("Traceback", err)
        self.assertIn("config_readable", payload["blocking_failures"])

    def test_parse_failure_is_reported_not_just_existence(self):
        """`os.path.exists` said yes while the file was unreadable JSON."""
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".dobby", "knowledge", "kg.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ broken")
        rc, payload, _ = run_doctor(d)
        self.assertEqual(rc, 1)
        row = next(c for c in payload["checks"] if "kg.json" in c["check"])
        self.assertFalse(row["ok"])
        self.assertIn("NOT valid JSON", row["detail"])

    def test_empty_object_is_not_healthy_data(self):
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".dobby", "policies", "policies.json"), "w",
                  encoding="utf-8") as f:
            f.write("{}")
        rc, payload, _ = run_doctor(d)
        self.assertEqual(rc, 1)


class TestSeverity(unittest.TestCase):
    """Fixing the exit code naively would have made every CI run red."""

    def test_missing_providers_is_advisory_not_blocking(self):
        """A CI runner has no agent CLIs by design. That is thin, not broken."""
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        rc, payload, _ = run_doctor(d)
        for name in ("providers", "multi_agent"):
            row = next(c for c in payload["checks"] if c["check"] == name)
            self.assertFalse(row["blocking"],
                             f"{name} must not set the exit code")

    def test_unbootstrapped_is_advisory(self):
        d = make_repo(drop_bootstrap=True)
        self.addCleanup(shutil.rmtree, d, True)
        rc, payload, _ = run_doctor(d)
        self.assertEqual(rc, 0, payload["verdict"])
        self.assertIn("bootstrapped", payload["advisory_gaps"])
        self.assertEqual(payload["blocking_failures"], [])
        self.assertIn("usable", payload["verdict"])

    def test_corrupt_data_is_blocking(self):
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".dobby", "ontology.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ broken")
        rc, payload, _ = run_doctor(d)
        self.assertEqual(rc, 1)
        self.assertIn("BLOCKING", payload["verdict"])

    def test_every_check_declares_its_severity(self):
        d = make_repo()
        self.addCleanup(shutil.rmtree, d, True)
        _, payload, _ = run_doctor(d)
        for row in payload["checks"]:
            self.assertIn("blocking", row, f"{row['check']} has no severity")

    def test_advisory_gaps_are_still_reported(self):
        """Not setting the exit code must not mean going unmentioned."""
        d = make_repo(drop_bootstrap=True)
        self.addCleanup(shutil.rmtree, d, True)
        _, payload, _ = run_doctor(d)
        self.assertTrue(payload["advisory_gaps"])
        self.assertIn("advisory", payload["verdict"])


if __name__ == "__main__":
    unittest.main()
