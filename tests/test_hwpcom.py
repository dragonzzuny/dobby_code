"""Driving 한글 over COM: the refusals, and the one guard that cannot be skipped.

WHAT CAN AND CANNOT BE TESTED WITHOUT 한글

Nothing here can open a document on a machine without Windows and an installed
한글, and pretending otherwise would make a green suite that proves nothing. So
this file splits in two.

The first half tests what is testable anywhere: the refusals. Every one of them
exists because the alternative is a corrupted document or a silent no-op, and
each is reachable without touching COM because the check happens in Python
before the driver is ever spawned.

The second half runs against real `.hwp` files if any are present and 한글 is
drivable, and SKIPS otherwise. A skip is recorded as a skip.

THE ASCII TEST IS NOT COSMETIC

Windows PowerShell 5.1 reads a `.ps1` as ANSI, not UTF-8. A single Korean
character added to the embedded script turns the whole file into mojibake and
the driver dies with a parser error that says nothing about encoding. The
payload therefore travels through a UTF-8 file and the script stays ASCII. That
invariant is one commit away from being broken by someone adding a helpful
comment, so it is asserted here rather than trusted.
"""

from __future__ import annotations

import glob
import os
import unittest

from dobby import hwpcom


class Refusals(unittest.TestCase):
    """Every path that declines to act, and the reason it gives."""

    def test_embedded_powershell_is_ascii(self):
        # See the module docstring. This is the guard, not a style check.
        hwpcom._PS.encode("ascii")

    def test_unmatchable_characters_are_refused_not_attempted(self):
        # An en dash inside the needle silently fails to match through COM.
        # Failing loudly at the boundary beats reporting the string as absent.
        with self.assertRaises(hwpcom.HwpComError) as caught:
            hwpcom.replace("x.hwp", [("z = 3.49–4.35", "z")], out="y.hwp")
        msg = str(caught.exception)
        self.assertIn("U+2013", msg)
        self.assertIn("split_at_unmatchable", msg)

    def test_split_at_unmatchable_returns_the_runs_between(self):
        self.assertEqual(
            hwpcom.split_at_unmatchable("z = 3.49–4.35"),
            ["z = 3.49", "4.35"])
        self.assertEqual(hwpcom.split_at_unmatchable("plain"), ["plain"])
        self.assertEqual(hwpcom.split_at_unmatchable("—"), [])

    def test_source_is_never_overwritten_by_default(self):
        with self.assertRaises(hwpcom.HwpComError) as caught:
            hwpcom.replace("x.hwp", [("a", "b")])
        self.assertIn("never writes over the source", str(caught.exception))

    def test_empty_pair_list_is_refused(self):
        with self.assertRaises(hwpcom.HwpComError):
            hwpcom.replace("x.hwp", [], out="y.hwp")

    def test_available_names_what_is_missing(self):
        info = hwpcom.available()
        self.assertIn("ok", info)
        self.assertIn("missing", info)
        # The contract that matters: not-ok always says why, in words.
        if not info["ok"]:
            self.assertTrue(info["missing"])
            self.assertTrue(all(isinstance(m, str) and m for m in info["missing"]))


class DriverContract(unittest.TestCase):
    """Shape of the result, checked without a document."""

    def test_missing_file_is_named(self):
        if not hwpcom.available()["ok"]:
            self.skipTest("한글 automation unavailable; refusal path untestable here")
        with self.assertRaises(hwpcom.HwpComError) as caught:
            hwpcom.page_count("no-such-document-12345.hwp")
        self.assertIn("no such file", str(caught.exception))


def _sample_hwp():
    """A real `.hwp` placed here on purpose, or None. Never ships one.

    Deliberately NOT a recursive search of the home directory: an early version
    did that and turned a fast suite into one that walked every file the user
    owns before deciding to skip. Put a document in `tests/data/` or point
    DOBBY_HWP_SAMPLE at one.
    """
    env = os.environ.get("DOBBY_HWP_SAMPLE")
    if env and os.path.exists(env):
        return env
    for pattern in ("tests/data/*.hwp", "*.hwp"):
        for hit in sorted(glob.glob(pattern)):
            if os.path.getsize(hit) > 4096:
                return hit
    return None


class RealDocument(unittest.TestCase):
    """Runs where 한글 is drivable and a document exists; skips where not."""

    @classmethod
    def setUpClass(cls):
        info = hwpcom.available()
        if not info["ok"]:
            raise unittest.SkipTest("; ".join(info["missing"]))
        cls.path = _sample_hwp()
        if not cls.path:
            raise unittest.SkipTest("no .hwp on this machine to measure against")

    def test_page_count_is_a_positive_integer(self):
        pages = hwpcom.page_count(self.path)
        self.assertIsInstance(pages, int)
        self.assertGreater(pages, 0)

    def test_body_scan_terminates_and_does_not_repeat_its_tail(self):
        # SetPos clamps rather than failing. A scan bounded by its return value
        # runs to the cap and pads the result with the last paragraph repeated.
        shapes = hwpcom.paragraph_shapes(self.path, list_id=0)
        self.assertTrue(shapes)
        self.assertLess(len(shapes), 2000)
        self.assertEqual([p["index"] for p in shapes], list(range(len(shapes))))

    def test_dry_run_writes_nothing(self):
        text = next((p["text"] for p in hwpcom.paragraph_shapes(self.path)
                     if len(p["text"].strip()) > 8), None)
        if not text:
            self.skipTest("no paragraph long enough to use as a needle")
        needle = text.strip()[:8]
        out = self.path + ".dryrun-should-not-exist"
        result = hwpcom.replace(self.path, [(needle, needle)], out=out, apply=False)
        self.assertIsNone(result["written"])
        self.assertFalse(os.path.exists(out))

    def test_a_replacement_that_matches_nothing_writes_nothing(self):
        # Absence and refusal are different answers to "did it work", and a
        # save that changed nothing is a lie told by a timestamp.
        out = self.path + ".absent-should-not-exist"
        result = hwpcom.replace(
            self.path, [("�this string is not in any document�", "x")],
            out=out)
        self.assertEqual(result["applied"], 0)
        self.assertIsNone(result["written"])
        self.assertFalse(os.path.exists(out))
        self.assertIn("nothing was replaced", result.get("why", ""))


if __name__ == "__main__":
    unittest.main()
