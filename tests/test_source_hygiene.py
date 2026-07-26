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


def _compile_findings(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    rel = os.path.relpath(path, REPO).replace(os.sep, "/")
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


if __name__ == "__main__":
    unittest.main()
