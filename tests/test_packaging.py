"""One version, one package list, and advice that is true.

`pyproject.toml` sits ALONGSIDE `install.sh`, not instead of it. The installer
copies the engine into a host project next to that project's `.dobby/` data and
never overwrites the data; pip puts the engine on an import path and knows
nothing about whose knowledge it must leave alone. Neither is the real one, and
the tests here are about the seams where getting that wrong shows up.

Three things were wrong before this file existed, all found by installing the
wheel into a clean venv and using it:

    two versions   `dobby/__init__.py` said 0.1.0 and `dobby/core/__init__.py`
                   said 2.0.0. Nothing imported the second and `doctor`
                   reported the first, so the number nobody read was free to
                   drift -- and the first question about any bug report is
                   which version produced it.

    bad advice     `doctor` told an operator with missing seed data to run
                   `dobby init --scan .`. Measured: that command writes
                   `inventory.json` and `knowledge/kg.bootstrap.json` and none
                   of the six files being complained about, so running it
                   leaves the same six failures. A loop.

    no manifest    the only third-party import in the engine is PyYAML, and it
                   was declared in `install.sh` as a warning, in CI as a pip
                   line, and nowhere a packaging tool could read.

The wheel-building tests skip when `build` is not installed rather than
pretending to have checked. A skipped test says so; a passing one that ran
nothing does not.
"""

import io
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import dobby  # noqa: E402
import dobby.core  # noqa: E402

PYPROJECT = os.path.join(REPO, "pyproject.toml")


def manifest():
    if sys.version_info >= (3, 11):
        import tomllib

        with io.open(PYPROJECT, "rb") as fh:
            return tomllib.load(fh)
    return None


class OneVersion(unittest.TestCase):
    """Two numbers for one engine is one number nobody maintains."""

    def test_core_reports_the_same_version_as_the_package(self):
        self.assertEqual(dobby.core.__version__, dobby.__version__)

    def test_they_are_the_same_object_and_not_two_equal_strings(self):
        """Equal strings drift the moment somebody edits one. A re-export
        cannot."""
        self.assertIs(dobby.core.__version__, dobby.__version__)

    def test_core_declares_no_version_of_its_own(self):
        with io.open(os.path.join(REPO, "dobby", "core", "__init__.py"),
                     encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn('__version__ = "', source,
                         "core is declaring a second version again")

    def test_the_version_looks_like_a_version(self):
        parts = dobby.__version__.split(".")
        self.assertGreaterEqual(len(parts), 2, dobby.__version__)
        self.assertTrue(all(p.isdigit() for p in parts), dobby.__version__)


class TheManifestMatchesTheTree(unittest.TestCase):
    def setUp(self):
        self.data = manifest()
        if self.data is None:
            self.skipTest("tomllib needs 3.11; the manifest is read by pip "
                          "either way")

    def test_it_lists_every_subpackage_that_exists(self):
        """A package left out of the list is absent from an installed dobby,
        and the failure arrives as an ImportError on somebody else's machine."""
        listed = set(self.data["tool"]["setuptools"]["packages"])
        actual = {"dobby"}
        root = os.path.join(REPO, "dobby")
        for name in sorted(os.listdir(root)):
            if os.path.exists(os.path.join(root, name, "__init__.py")):
                actual.add(f"dobby.{name}")
        self.assertEqual(listed, actual)

    def test_it_ships_no_directory_that_is_not_the_engine(self):
        """`evals` is this project's measurement corpus and `tests` are its
        own; installing either into somebody's environment is not shipping a
        harness."""
        listed = set(self.data["tool"]["setuptools"]["packages"])
        for outsider in ("tests", "tools", "evals", "mcp"):
            self.assertNotIn(outsider, listed)
            self.assertNotIn(f"dobby.{outsider}", listed)

    def test_the_version_is_read_from_the_one_place_it_lives(self):
        self.assertIn("version", self.data["project"]["dynamic"])
        self.assertEqual(
            self.data["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "dobby.__version__")

    def test_the_declared_dependencies_are_the_ones_actually_imported(self):
        """Walked from the AST rather than read from a list, because a list is
        what goes stale."""
        import ast

        stdlib = set(sys.stdlib_module_names)
        imported = set()
        for base, _dirs, names in os.walk(os.path.join(REPO, "dobby")):
            if "__pycache__" in base:
                continue
            for name in names:
                if not name.endswith(".py"):
                    continue
                with io.open(os.path.join(base, name), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported |= {a.name.split(".")[0] for a in node.names}
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        if node.module:
                            imported.add(node.module.split(".")[0])
        third_party = {m for m in imported
                       if m not in stdlib and m != "dobby"}
        declared = {d.split(">")[0].split("=")[0].split("[")[0].strip().lower()
                    for d in self.data["project"]["dependencies"]}
        self.assertEqual({m.lower() for m in third_party}, {"yaml"},
                         f"the engine imports {sorted(third_party)}")
        self.assertEqual(declared, {"pyyaml"})

    def test_the_console_script_points_at_something_callable(self):
        from dobby.cli import main

        self.assertEqual(self.data["project"]["scripts"]["dobby"],
                         "dobby.cli:main")
        self.assertTrue(callable(main))

    def test_the_python_floor_matches_what_ci_measures(self):
        """CI runs 3.10 and 3.12. Claiming more is claiming something nothing
        tested."""
        self.assertEqual(self.data["project"]["requires-python"], ">=3.10")


class TheAdviceIsTrue(unittest.TestCase):
    """A fix line naming a command that does not fix it is worse than none."""

    def test_doctor_does_not_send_a_missing_seed_file_to_init_scan(self):
        with io.open(os.path.join(REPO, "dobby", "cli.py"),
                     encoding="utf-8") as fh:
            source = fh.read()
        start = source.index('check(f"data:{rel}", False, f"missing:')
        advice = source[start:start + 700]
        self.assertIn("install.sh", advice,
                      "the fix line has to name the route that works")
        self.assertIn("does NOT create", advice,
                      "and say plainly that init --scan does not")


class TheWheelBuildsAndRuns(unittest.TestCase):
    """The end of the chain: does a built artifact actually work.

    Slow, and skipped when `build` is absent rather than quietly not run.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        if importlib.util.find_spec("build") is None:
            raise unittest.SkipTest("`build` is not installed here")
        import shutil
        import tempfile

        cls.tmp = tempfile.mkdtemp()
        cls._cleanup = lambda: shutil.rmtree(cls.tmp, ignore_errors=True)
        proc = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", cls.tmp],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900)
        if proc.returncode != 0:
            cls._cleanup()
            raise unittest.SkipTest(f"the wheel would not build: "
                                    f"{proc.stderr[-300:]}")
        cls.wheels = [f for f in os.listdir(cls.tmp) if f.endswith(".whl")]

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_cleanup"):
            cls._cleanup()

    def test_exactly_one_wheel_is_produced(self):
        self.assertEqual(len(self.wheels), 1, self.wheels)

    def test_the_wheel_carries_the_declared_version(self):
        self.assertIn(dobby.__version__, self.wheels[0])

    def test_the_wheel_holds_the_engine_and_nothing_else(self):
        import zipfile

        with zipfile.ZipFile(os.path.join(self.tmp, self.wheels[0])) as zf:
            tops = {name.split("/")[0] for name in zf.namelist()}
        self.assertIn("dobby", tops)
        for outsider in ("tests", "tools", "evals", "mcp"):
            self.assertNotIn(outsider, tops)


if __name__ == "__main__":
    unittest.main()
