"""What a `dobby` command pays before it does anything.

`dobby/cli.py` reached into `dobby.project.inquiry` for one tuple of stage
names -- while BUILDING ITS ARGUMENT PARSER, so every command paid it, not just
the one that uses stages. `dobby.project.__init__` imported all fourteen of its
submodules to present a flat API, and those pulled in `dobby.runtime`, which
imported twelve of its own. Measured on this machine:

    dobby.cli                  0.09s
    dobby.project              0.47s   ->  0.01s
    dobby.project.inquiry      0.72s   ->  0.18s
    dobby.runtime              0.55s   ->  0.01s

    dobby doctor               6.43s   ->  2.50s
    20 CLI commands, once     34.8s    -> 16.5s
    tests.test_doctor_damage   578s    ->  303s
    tests.test_install         512s    ->  299s

PEP 562 `__getattr__`, so the public names are unchanged: `from dobby.project
import ProjectStore` works, `from dobby.project import workorder` works because
the import system falls back to importing the submodule when `__getattr__`
raises, and star imports work because `__all__` is declared. Checked before
doing it: no module under either package runs anything at import time, so there
is no registration lost by deferring.

The first version of the map stored only the ALIAS. `from .metrics import
report as metrics_report` binds `metrics_report` here and `report` there, so
the lookup went hunting for `metrics_report` inside `metrics` and raised
ImportError on a name that had worked for a year. The map now carries
`(module, original_name)` and this file asserts every alias resolves.

These tests are about a COST, so they assert the mechanism rather than a
wall-clock number: a timing threshold measured on one machine is a flake
somewhere else, and the mechanism is what a later edit would remove by
accident.
"""

import importlib
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

LAZY_PACKAGES = ("dobby.project", "dobby.runtime")


class TheMapIsComplete(unittest.TestCase):
    """Every name the package promises has to resolve to something real."""

    def test_every_public_name_resolves(self):
        for name in LAZY_PACKAGES:
            package = importlib.import_module(name)
            for public in package.__all__:
                self.assertTrue(hasattr(package, public),
                                f"{name}.{public} is in __all__ and cannot be "
                                f"reached")

    def test_every_alias_points_at_its_original(self):
        """The defect the first version shipped with."""
        for name in LAZY_PACKAGES:
            package = importlib.import_module(name)
            for public, (module, original) in package._LAZY.items():
                target = importlib.import_module(f".{module}", name)
                self.assertTrue(
                    hasattr(target, original),
                    f"{name}.{public} maps to {module}.{original}, which does "
                    f"not exist")

    def test_an_unknown_name_still_raises_attribute_error(self):
        for name in LAZY_PACKAGES:
            package = importlib.import_module(name)
            with self.assertRaises(AttributeError):
                getattr(package, "definitely_not_a_real_name")

    def test_dir_lists_the_lazy_names(self):
        """Otherwise tab completion and `help()` show an empty package."""
        for name in LAZY_PACKAGES:
            package = importlib.import_module(name)
            listed = set(dir(package))
            self.assertTrue(set(package._LAZY) <= listed, name)


class TheImportFormsAllStillWork(unittest.TestCase):
    """Run in a fresh interpreter, because this process has them cached."""

    def run_snippet(self, snippet):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, "-c", snippet], cwd=REPO,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def test_a_name_import(self):
        code, out = self.run_snippet(
            "from dobby.project import ProjectStore, STAGE_KEYS\n"
            "from dobby.runtime import Runner, RunBudget\n"
            "print('ok')")
        self.assertEqual(code, 0, out)

    def test_an_aliased_name_import(self):
        code, out = self.run_snippet(
            "from dobby.runtime import metrics_report, flywheel_report\n"
            "print(metrics_report.__name__)")
        self.assertEqual(code, 0, out)

    def test_a_submodule_import(self):
        """`__getattr__` raising is what lets the import system fall back."""
        code, out = self.run_snippet(
            "from dobby.project import workorder, models\n"
            "from dobby.runtime import graph, contracts\n"
            "print('ok')")
        self.assertEqual(code, 0, out)

    def test_a_star_import(self):
        code, out = self.run_snippet(
            "from dobby.project import *\n"
            "print('ProjectStore' in dir())")
        self.assertEqual(code, 0, out)
        self.assertIn("True", out)


class NothingIsImportedUntilItIsAskedFor(unittest.TestCase):
    """The property, stated as what is NOT loaded rather than as a duration."""

    def loaded_after(self, snippet):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "-c",
             snippet + "\nimport sys\n"
             "print('|'.join(sorted(m for m in sys.modules "
             "if m.startswith('dobby.'))))"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return set(proc.stdout.strip().rsplit("\n", 1)[-1].split("|"))

    def test_importing_the_package_loads_no_submodule(self):
        for name in LAZY_PACKAGES:
            loaded = self.loaded_after(f"import {name}")
            heavy = {m for m in loaded
                     if m.startswith(name + ".") and not m.endswith("__")}
            self.assertEqual(heavy, set(),
                             f"{name} still imports {sorted(heavy)[:5]}")

    #: A leaf whose only content is a tuple of strings and whose only
    #: imports are stdlib. Loading it costs what reading a tuple costs,
    #: which is why the stage names were moved there.
    LEAVES = {"dobby.project.stages"}

    def test_building_the_parser_does_not_load_the_project_stack(self):
        """The specific cost: one help string, the whole subsystem."""
        loaded = self.loaded_after(
            "from dobby.cli import build_parser" + chr(10) +
            "build_parser()")
        offenders = sorted(m for m in loaded - self.LEAVES
                           if m.startswith("dobby.project.")
                           or m.startswith("dobby.runtime."))
        self.assertEqual(
            offenders, [],
            "building the argument parser pulled in " + str(offenders[:6]))

    def test_the_leaf_really_is_a_leaf(self):
        """Otherwise the exemption above quietly readmits everything."""
        loaded = self.loaded_after("import dobby.project.stages")
        heavy = sorted(m for m in loaded
                       if m.startswith("dobby.") and m not in self.LEAVES
                       and m not in ("dobby", "dobby.project"))
        self.assertEqual(heavy, [], f"the leaf pulled in {heavy[:6]}")

    def test_the_two_stage_lists_are_the_same(self):
        """`inquiry` asserts this at import; asserted here too so a
        failure names the mismatch instead of arriving as an
        AssertionError from a module somebody imported for another
        reason."""
        from dobby.project.inquiry import STAGE_KEYS as from_inquiry
        from dobby.project.stages import STAGE_KEYS as from_leaf

        self.assertEqual(from_inquiry, from_leaf)

    def test_asking_for_a_name_does_load_it(self):
        """The other half: lazy must not mean absent."""
        loaded = self.loaded_after(
            "from dobby.runtime import Runner\nRunner")
        self.assertIn("dobby.runtime.runner", loaded)


if __name__ == "__main__":
    unittest.main()
