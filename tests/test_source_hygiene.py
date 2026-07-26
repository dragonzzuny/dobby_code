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

# `kitonly` sits beside this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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




class TestKitOnlyTestsAreGuarded(unittest.TestCase):
    """A test that describes the DISTRIBUTION must not run inside a host.

    The installer copies `tests/` into every project and its closing message tells
    the user to run `python -m unittest discover -s tests`. So anything asserting
    on `install.sh`, `.gitattributes`, `.gitignore`, `tools/` or the kit's own
    knowledge graph runs where those do not exist.

    Measured in a freshly installed host: 3 failures and 3 errors across six tests,
    none of them a defect in the installed harness. `test_install.py` had a guard
    from the start and it was never applied anywhere else, which is how six more
    accumulated. This check is what stops the seventh: the guard existing is not
    the same as the guard being used, and only a scan can tell the difference.
    """

    #: Names that exist in the distribution and not in an installed host.
    KIT_ONLY_MARKERS = ("install.sh", "install.ps1", ".gitattributes",
                        ".gitignore", ".github", "refresh_self_kg", chr(34) + "tools" + chr(34))

    #: Either the shared guard or the older local predicate counts as guarded.
    GUARDS = ("from kitonly import", "_IS_THE_KIT")

    def _test_modules(self):
        directory = os.path.join(REPO, "tests")
        for name in sorted(os.listdir(directory)):
            if name.startswith("test_") and name.endswith(".py"):
                yield name, os.path.join(directory, name)

    def _kit_dependent_lines(self, text):
        """Lines that bind a kit-only name to REPO.

        Mentioning a name is not depending on it, and a first version of this check
        could not tell the difference. It flagged `test_bootstrap.py`, which builds
        a `.github/` fixture inside a TEMP directory, and `test_platform.py`, which
        names `install.sh` in a docstring. Both pass inside a host - verified by
        actually installing and running the suite there - so both were false
        alarms, and a check that cries wolf gets switched off.

        Binding to `REPO` is what means "this file must exist in the distribution",
        which is the property the guard is for.
        """
        hits = []
        for line in text.splitlines():
            if "REPO" not in line:
                continue
            if any(marker in line for marker in self.KIT_ONLY_MARKERS):
                hits.append(line.strip())
        return hits

    def test_every_module_depending_on_kit_only_paths_is_guarded(self):
        offenders = []
        for name, path in self._test_modules():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            depends = self._kit_dependent_lines(text)
            if not depends:
                continue
            if not any(guard in text for guard in self.GUARDS):
                offenders.append(name + ": " + depends[0][:80])
        self.assertEqual(
            offenders, [],
            "these would fail inside an installed host:" + chr(10)
            + chr(10).join(offenders))

    def test_the_check_does_not_fire_on_a_mere_mention(self):
        """The two false alarms, pinned so the rule cannot loosen back."""
        self.assertEqual(
            self._kit_dependent_lines('    """See install.sh line 24."""'), [])
        self.assertEqual(
            self._kit_dependent_lines(
                'os.makedirs(os.path.join(root, ".github", "workflows"))'), [])

    def test_the_check_does_fire_on_a_real_dependency(self):
        self.assertTrue(self._kit_dependent_lines(
            'path = os.path.join(REPO, ".gitattributes")'))

    def test_the_shared_guard_module_exists_and_is_importable(self):
        import kitonly
        self.assertTrue(hasattr(kitonly, "IS_THE_KIT"))
        self.assertTrue(hasattr(kitonly, "kit_only"))

    def test_the_guard_is_true_in_the_distribution(self):
        """Otherwise every guarded test silently skips here too."""
        import kitonly
        self.assertTrue(kitonly.IS_THE_KIT,
                        "the kit markers are missing from this checkout")

    def test_the_marker_files_are_not_installed_into_a_host(self):
        """The predicate only works if the installer really excludes them."""
        with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as handle:
            installer = handle.read()
        # The installer copies named trees; it never copies itself or .gitignore.
        for marker in ("install.sh", ".gitignore"):
            self.assertNotIn(f'cp "$SRC/{marker}"', installer,
                             f"the installer copies {marker} into the host, "
                             f"which would break the kit-only predicate")




class TestNothingWritesIntoTheRepositoryRoot(unittest.TestCase):
    """Two directories appeared in the repo root and nobody noticed for a day.

        dobby_code/한국어한국어...   (x20)
        dobby_code/🔥🔥🔥...        (x20)

    `tools/census.py` calls every public function with degenerate arguments,
    including non-ASCII strings for parameters named `path`. Its docstring claimed
    writers were "excluded by name, not by hope" and that the list made the gap
    visible. The list missed `HierarchicalMemory`, which creates its directory
    tree from the path it is given, so the fuzzer's own inputs became directories
    in the repository.

    They survived because **git does not track empty directories** — `git status`
    was clean the entire time, which is why every check run since then said the
    tree was fine.

    The fix is containment rather than a longer list: census now probes from a
    throwaway cwd and reports whatever appeared there. An enumerated allowlist
    fails this way eventually; a contained blast radius does not.
    """

    def test_the_repository_root_has_no_stray_non_ascii_entries(self):
        strays = [name for name in os.listdir(REPO)
                  if any(ord(char) > 127 for char in name)]
        self.assertEqual(strays, [], f"debris in the repo root: {strays!r}")

    def test_census_probes_from_a_contained_working_directory(self):
        with open(os.path.join(REPO, "tools", "census.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("os.chdir(scratch)", source,
                      "census probes from the repository, so a missed writer "
                      "creates files in it")
        self.assertIn("os.chdir(origin)", source,
                      "census never restores the working directory")

    def test_a_writer_probed_with_a_degenerate_path_cannot_reach_the_repo(self):
        """The exact hazard, reproduced: contained cwd, repo must stay clean."""
        import shutil
        import tempfile

        from dobby.memory import HierarchicalMemory

        before = set(os.listdir(REPO))
        origin = os.getcwd()
        scratch = tempfile.mkdtemp(prefix="dobby-census-test-")
        try:
            os.chdir(scratch)
            for degenerate in ("\U0001f525" * 20, "한국어" * 20):
                try:
                    HierarchicalMemory(os.path.join(degenerate, ".dobby",
                                                    "memory"))
                except Exception:            # noqa: BLE001 - refusal is fine
                    pass
            created = os.listdir(scratch)
        finally:
            os.chdir(origin)
            shutil.rmtree(scratch, ignore_errors=True)

        self.assertTrue(created, "the hazard did not reproduce; if this fails, "
                                 "HierarchicalMemory no longer creates its tree "
                                 "and the containment may be unnecessary")
        self.assertEqual(sorted(set(os.listdir(REPO)) - before), [],
                         "a probed writer reached the repository root")


if __name__ == "__main__":
    unittest.main()
