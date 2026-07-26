"""The tamper check must survive a normal `git clone`.

Every CI run of this repository was red, and this was why. The skill origin pin
is a sha256 of the body file, and it was computed over RAW BYTES. git rewrites
line endings on checkout — `core.autocrlf=true` is the Windows default — so a
body pinned at 3325 bytes of LF arrives as 3381 bytes of CRLF and the digest
differs. Measured on a completely untouched file:

    working tree   3325 bytes, LF     sha 13336a690f14
    fresh clone    3381 bytes, CRLF   sha 1beccd111be3b

A control that reports tampering after a normal clone is worse than no control:
it trains everyone to ignore it, and the one real tamper then looks like the
usual noise. Meanwhile the local suite passed, so the failure was invisible from
the machine doing the work — which is exactly why the CI status had to be read
rather than assumed.

The fix normalizes line endings before hashing, so the digest is over CONTENT.
`.gitattributes` makes the checkout deterministic as well, but that is defence in
depth: a user with their own git config still gets a correct verdict.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# `kitonly` sits beside this file. The suite runs both as `tests.test_x`
# and as a bare script; only REPO was on sys.path, so the package form
# raised ModuleNotFoundError.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kitonly import kit_only  # noqa: E402

from dobby.core.platform import child_env
from dobby.core.skills import SkillRegistry, _content_digest

BODY = os.path.join(REPO, ".claude", "skills", "bootstrap-project", "SKILL.md")


def variants(source_bytes: bytes) -> dict:
    lf = source_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return {
        "lf": lf,
        "crlf": lf.replace(b"\n", b"\r\n"),
        "cr": lf.replace(b"\n", b"\r"),
        "tampered": lf + b"rm -rf / # injected step\n",
        "tampered_crlf": (lf + b"rm -rf / # injected step\n").replace(
            b"\n", b"\r\n"),
    }


class TestDigestIgnoresLineEndings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(BODY, "rb") as f:
            self.written = {}
            for name, data in variants(f.read()).items():
                path = os.path.join(self.tmp, f"{name}.md")
                with open(path, "wb") as out:
                    out.write(data)
                self.written[name] = path

    def test_lf_crlf_and_cr_produce_the_same_digest(self):
        digests = {k: _content_digest(self.written[k])
                   for k in ("lf", "crlf", "cr")}
        self.assertEqual(len(set(digests.values())), 1,
                         f"line endings changed the digest: {digests}")

    def test_tampering_is_still_detected(self):
        """Normalizing must not blunt the control it protects."""
        self.assertNotEqual(_content_digest(self.written["tampered"]),
                            _content_digest(self.written["lf"]))

    def test_tampering_is_detected_regardless_of_line_endings(self):
        self.assertNotEqual(_content_digest(self.written["tampered_crlf"]),
                            _content_digest(self.written["lf"]))

    def test_a_crlf_body_still_matches_an_lf_pin(self):
        """The exact failure: pin made on LF, verified against a CRLF checkout."""
        self.assertEqual(_content_digest(self.written["crlf"]),
                         _content_digest(self.written["lf"]))


class TestShippedPinsVerify(unittest.TestCase):
    def test_every_factory_skill_verifies_in_this_checkout(self):
        registry = SkillRegistry(
            os.path.join(REPO, ".dobby", "registry", "skills.json"))
        failures = []
        for entry in registry.index(runtime_gate=False):
            ok, why = registry.verify_origin(entry["name"], repo_root=REPO)
            if not ok:
                failures.append(f"{entry['name']}: {why}")
        self.assertEqual(failures, [], "\n".join(failures))


@kit_only
class TestGitattributesExists(unittest.TestCase):
    """Defence in depth: make the checkout deterministic too."""

    def test_gitattributes_pins_text_files_to_lf(self):
        path = os.path.join(REPO, ".gitattributes")
        self.assertTrue(os.path.exists(path), "no .gitattributes")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("text=auto eol=lf", text)

    def test_windows_scripts_keep_crlf(self):
        """cmd.exe and PowerShell can mis-parse an LF-only script."""
        with open(os.path.join(REPO, ".gitattributes"), encoding="utf-8") as f:
            text = f.read()
        for pattern in ("*.ps1", "*.bat", "*.cmd"):
            self.assertIn(pattern, text)
        self.assertIn("eol=crlf", text)


@unittest.skipUnless(shutil.which("git"), "git not available")
class TestFreshCloneVerifies(unittest.TestCase):
    """The end-to-end shape of the bug: clone the repo, run the check there.

    Skipped when the checkout has uncommitted changes to the files involved,
    because a clone reads committed HEAD and would be testing something other
    than what is on disk.
    """

    def test_a_clone_of_head_passes_the_pin_check(self):
        proc = subprocess.run(
            ["git", "-C", REPO, "status", "--porcelain", "--",
             "dobby/core/skills.py", ".dobby/registry/skills.json",
             ".claude/skills"],
            capture_output=True, text=True, env=child_env())
        if (proc.stdout or "").strip():
            self.skipTest("relevant files have uncommitted changes; a clone "
                          "would test committed HEAD instead")

        target = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, target, True)
        clone = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", REPO,
             os.path.join(target, "c")],
            capture_output=True, text=True, env=child_env(), timeout=300)
        if clone.returncode != 0:
            self.skipTest(f"clone failed: {(clone.stderr or '')[-160:]}")

        run = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_adoptions", "-q"],
            cwd=os.path.join(target, "c"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=child_env(), timeout=300)
        self.assertEqual(run.returncode, 0,
                         "the pin check fails in a fresh clone:\n"
                         + (run.stderr or "")[-700:])


if __name__ == "__main__":
    unittest.main()
