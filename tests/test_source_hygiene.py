"""Repo-wide source properties that no unit test would otherwise notice.

Both checks here exist because the defects were found by accident. Four invalid
escape sequences surfaced as `DeprecationWarning` lines in the output of an
unrelated test run — `"Q:\\work"` in a value, and `C:\\Windows` inside two
docstrings that were not raw. They are warnings today and a `SyntaxError` in a
future Python, so they are defects with a deadline rather than style notes, and
nothing in the suite was looking for them.

Compiling with warnings promoted is the only reliable detector: the warning comes
from the compiler, not from any linter this project depends on — and this project
depends on PyYAML and nothing else.
"""

import os
import sys
import unittest
import warnings
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

SKIP_DIRS = frozenset({
    "__pycache__", ".git", ".venv", "venv", "node_modules", "build", "dist",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
})


def _sources() -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def _label(path: str) -> str:
    """A short name for `path`, falling back to the absolute one.

    `os.path.relpath` raises ValueError on Windows when the two paths are on
    different VOLUMES, and a GitHub Windows runner puts the workspace on `D:` while
    TEMP is on `C:`. The planted-defect test below writes into a temp directory, so
    this raised there and only there — green on this machine, where both are `C:`,
    and an ERROR on both Windows jobs in CI.

    The same hazard is already guarded in `dobby/sandbox.py`, documented, and
    tested. Reintroducing it in a test helper is the more instructive half: a
    cross-volume `relpath` is not a Windows curiosity to remember once, it is a
    default that has to be designed against every time. The label is for display,
    so failing to shorten it must not fail anything.
    """
    try:
        return os.path.relpath(path, REPO).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def _compile_findings(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    rel = _label(path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            compile(source, path, "exec")
        except SyntaxError as exc:
            return [f"{rel}: SyntaxError: {exc}"]
        return [f"{rel}:{w.lineno} {w.message}" for w in caught
                if "escape sequence" in str(w.message)]


class TestEverySourceFileCompiles(unittest.TestCase):
    def test_no_syntax_errors_anywhere(self):
        """A file that does not compile is invisible to every other test here."""
        broken = [f for path in _sources() for f in _compile_findings(path)
                  if "SyntaxError" in f]
        self.assertEqual(broken, [], "\n".join(broken))

    def test_the_scan_actually_found_files(self):
        """A check that silently examines nothing always passes."""
        self.assertGreater(len(_sources()), 50, len(_sources()))


class TestNoInvalidEscapeSequences(unittest.TestCase):
    r"""A Windows path in a non-raw string is the whole failure mode.

    `"C:\Windows"` contains `\W`, which is not a recognised escape. Python accepts
    it today with a warning and will not accept it forever. This repository writes
    Windows paths constantly - in docstrings explaining the WSL launcher, in test
    values for the command guard - so the hazard is structural rather than
    occasional.

    The fix is a raw string or a forward slash, both of which say what was meant.
    """

    def test_no_file_has_an_invalid_escape(self):
        findings = [f for path in _sources() for f in _compile_findings(path)
                    if "escape sequence" in f]
        self.assertEqual(
            findings, [],
            "invalid escape sequences (a SyntaxError in a future Python); use a "
            "raw string or a forward slash:\n" + "\n".join(findings))

    def test_the_detector_catches_a_planted_one(self):
        """Otherwise a detector that never fires reads as a clean repository."""
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            planted = os.path.join(directory, "planted.py")
            with open(planted, "w", encoding="utf-8") as handle:
                handle.write('path = "C:' + chr(92) + 'Windows"\n')
            findings = _compile_findings(planted)
        self.assertTrue(any("escape sequence" in f for f in findings), findings)




class TestCrossVolumeLabelling(unittest.TestCase):
    r"""The defect that turned both Windows CI jobs red while this machine stayed green.

    A GitHub Windows runner puts the workspace on `D:` and TEMP on `C:`. The
    planted-defect test writes into a temp directory, so `os.path.relpath(path,
    REPO)` crossed volumes and raised ValueError. Here both are `C:`, so it could
    not happen locally — the annotation's own context line is what named it:

        win32 py3.12 enc=cp1252 cwd=D:\a\dobby_code\dobby_code
        tmp=C:\Users\RUNNER~1\AppData\Local\Temp

    `dobby/sandbox.py` already guards this and documents it. Reintroducing it in a
    test helper is the instructive part: a cross-volume relpath is a default to
    design against, not a fact to remember once.
    """

    def test_relpath_across_volumes_really_does_raise(self):
        """Establish the hazard rather than trusting the report."""
        if os.name != "nt":
            self.skipTest("POSIX has no volumes to cross")
        with self.assertRaises(ValueError):
            os.path.relpath(r"C:\Temp\x.py", r"D:\repo")

    def test_the_label_falls_back_instead_of_raising(self):
        import tests.test_source_hygiene as module
        with mock.patch.object(module, "REPO", "Q:/somewhere-else"
                               if os.name == "nt" else "/somewhere/else"):
            label = module._label(os.path.join(REPO, "tests",
                                              "test_source_hygiene.py"))
        self.assertTrue(label)
        self.assertIn("test_source_hygiene.py", label)

    def test_the_planted_detector_works_from_another_volume(self):
        """The exact CI failure, reproduced by moving REPO rather than TEMP."""
        import tempfile

        import tests.test_source_hygiene as module
        with tempfile.TemporaryDirectory() as directory:
            planted = os.path.join(directory, "planted.py")
            with open(planted, "w", encoding="utf-8") as handle:
                handle.write('path = "C:' + chr(92) + 'Windows"\n')
            with mock.patch.object(module, "REPO", "Q:/elsewhere"
                                   if os.name == "nt" else "/elsewhere"):
                findings = module._compile_findings(planted)
        self.assertTrue(any("escape sequence" in f for f in findings), findings)

    def test_a_label_inside_the_repo_is_still_short(self):
        label = _label(os.path.join(REPO, "dobby", "cli.py"))
        self.assertEqual(label, "dobby/cli.py")


if __name__ == "__main__":
    unittest.main()
