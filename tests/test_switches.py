"""The environment variables this engine reads, and whether it admits to them.

`dobby doctor` reported the platform, the files, and the fleet -- everything
except the part a human chose. Three switches change how the engine behaves and
two of them appeared in no document at all:

    DOBBY_SQLITE_SYNCHRONOUS      commit durability; NORMAL is ~15x faster and
                                  loses recent commits on an OS crash
    DOBBY_REQUIRE_PINNED_MODEL    a substituted model fails the node
    DOBBY_APPROVAL_DIR            where gate approvals are read and written

A machine behaving unlike the defaults is the first thing a diagnosis needs,
and the switch is the only part somebody decided. `platform.SWITCHES` is the
one place they are declared and `doctor` reports the ones that are SET.

A table of switches is worth exactly as much as its agreement with the code, so
this file holds it to both directions:

    every name in the table is really read by the module that claims it
    every DOBBY_* name the source reads is really in the table

Without the second, the table decays the way documentation decays -- by being
right about the day it was written.
"""

import ast
import io
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import SWITCHES, switches  # noqa: E402


def dobby_sources():
    for base, _dirs, names in os.walk(os.path.join(REPO, "dobby")):
        if "__pycache__" in base:
            continue
        for name in names:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def names_read_in(path):
    """Every `DOBBY_*` literal this file passes to `os.environ`.

    Read from the SOURCE and not by importing, because a name reached through a
    constant -- `os.environ.get(STRICT_ENV)` -- is invisible to a regex over
    the call itself. Both forms are collected: the literal in the call, and the
    literal a module-level constant is bound to.
    """
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    found = set(re.findall(
        r'environ(?:\.get)?[\(\[]\s*["\'](DOBBY_[A-Z0-9_]+)["\']', src))
    found |= set(re.findall(r'^[A-Z][A-Z0-9_]*\s*=\s*["\'](DOBBY_[A-Z0-9_]+)["\']',
                            src, re.M))
    return found


class TheTableIsHonest(unittest.TestCase):
    def test_every_declared_switch_is_read_where_it_says(self):
        for name, where, _default, _what in SWITCHES:
            path = os.path.join(REPO, where.replace("/", os.sep))
            self.assertTrue(os.path.exists(path), f"{name}: no {where}")
            self.assertIn(name, names_read_in(path),
                          f"{name} claims to be read by {where} and is not")

    def test_every_switch_the_source_reads_is_declared(self):
        declared = {name for name, _w, _d, _s in SWITCHES}
        found = {}
        for path in dobby_sources():
            for name in names_read_in(path):
                found.setdefault(name, os.path.relpath(path, REPO))
        undeclared = {n: w for n, w in found.items() if n not in declared}
        self.assertEqual(
            undeclared, {},
            "these are read and not in platform.SWITCHES, so `doctor` would "
            "not report a machine running with them set")

    def test_each_entry_says_what_it_changes(self):
        for name, where, default, what in SWITCHES:
            self.assertTrue(default, f"{name}: no default stated")
            self.assertGreater(len(what), 30,
                               f"{name}: the description says nothing useful")

    def test_the_names_are_unique(self):
        names = [name for name, _w, _d, _s in SWITCHES]
        self.assertEqual(len(names), len(set(names)))


class ItReportsWhatIsActuallySet(unittest.TestCase):
    def setUp(self):
        self.original = {name: os.environ.get(name)
                         for name, _w, _d, _s in SWITCHES}
        self.addCleanup(self.restore)
        for name in self.original:
            os.environ.pop(name, None)

    def restore(self):
        for name, value in self.original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nothing_set_means_nothing_reported_as_set(self):
        self.assertEqual([s for s in switches() if s["set"]], [])

    def test_a_set_switch_is_reported_with_its_value(self):
        os.environ["DOBBY_SQLITE_SYNCHRONOUS"] = "NORMAL"
        row = next(s for s in switches()
                   if s["name"] == "DOBBY_SQLITE_SYNCHRONOUS")
        self.assertTrue(row["set"])
        self.assertEqual(row["value"], "NORMAL")

    def test_an_unset_switch_carries_no_value(self):
        row = next(s for s in switches()
                   if s["name"] == "DOBBY_SQLITE_SYNCHRONOUS")
        self.assertFalse(row["set"])
        self.assertIsNone(row["value"],
                          "an unset switch must not look like it has a value")

    def test_an_empty_string_counts_as_set(self):
        """Setting a variable to "" is a choice somebody made, and several of
        these read it as false -- which is exactly the confusion a diagnosis
        needs to see rather than have hidden."""
        os.environ["DOBBY_REQUIRE_PINNED_MODEL"] = ""
        row = next(s for s in switches()
                   if s["name"] == "DOBBY_REQUIRE_PINNED_MODEL")
        self.assertTrue(row["set"])
        self.assertEqual(row["value"], "")

    def test_every_row_names_where_it_is_read(self):
        for row in switches():
            self.assertTrue(row["read_by"].endswith(".py"), row)


if __name__ == "__main__":
    unittest.main()
