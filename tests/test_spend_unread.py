"""A spend ledger line nobody could read is spend nobody counted.

`_read_tail` skipped a corrupt line with a comment saying "a corrupt line must
not break the status prompt". That is right. It also made the line disappear,
and this file answers what a run cost. Measured: five lines, two of them
corrupt, and `summarize` reported three calls and `complete: True`.

The same shape the four-axis token accounting was fixed for, in the module that
reports money. This repository already refuses it elsewhere -- `not_run` is not
a pass, an unmeasured token count is not zero, an edge that could not be added
is not an edge nobody declared -- and the spend ledger was the remaining one.

`summarize` now carries `unread_lines` and `ledger_complete` beside
`tokens_complete` and `dollars_complete`, because it is the same question one
level down: those ask whether every counted call reported usage, this asks
whether every recorded call was counted at all. `statusline` says `!N unread`,
next to the `+` that already means "this total is a floor".

Still skipped, deliberately: a corrupt line must not break the status prompt,
and it does not. What changed is that the total no longer claims to be a
measurement when part of the ledger could not be read.
"""

import json
import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.spend import SPEND_FILE, statusline, summarize  # noqa: E402

ENTRY = {"provider": "claude", "role": "implement", "duration_s": 1.0,
         "ok": True, "label": "x", "round_id": "r", "model": "opus",
         "skill": "", "measured": True, "input_tokens": 10,
         "output_tokens": 5, "thinking_tokens": 0, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "cost_usd": 1.25}


class LedgerCase(unittest.TestCase):
    def ledger(self, *, good=3, corrupt=0, wrong_shape=0):
        """A data dir whose ledger holds `good` readable lines and some junk."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        path = os.path.join(tmp, SPEND_FILE)
        os.makedirs(os.path.dirname(path) or tmp, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for _ in range(good):
                fh.write(json.dumps(dict(ENTRY, t=time.time())) + "\n")
            for _ in range(corrupt):
                fh.write("{ this is not json\n")
            for _ in range(wrong_shape):
                fh.write(json.dumps({"unexpected": "shape"}) + "\n")
        return tmp


class TheUnreadLinesAreCounted(LedgerCase):
    def test_a_clean_ledger_is_complete(self):
        out = summarize(self.ledger(good=3))
        self.assertEqual(out["unread_lines"], 0)
        self.assertTrue(out["ledger_complete"])
        self.assertEqual(out["calls"], 3)

    def test_unparseable_json_is_counted(self):
        out = summarize(self.ledger(good=3, corrupt=2))
        self.assertEqual(out["unread_lines"], 2)
        self.assertFalse(out["ledger_complete"])

    def test_a_line_of_the_wrong_shape_is_counted_too(self):
        """Valid JSON that is not an Entry is just as unread."""
        out = summarize(self.ledger(good=3, wrong_shape=2))
        self.assertEqual(out["unread_lines"], 2)
        self.assertFalse(out["ledger_complete"])

    def test_the_readable_lines_are_still_summed(self):
        """Skipping is still the behaviour; only the silence changed."""
        out = summarize(self.ledger(good=3, corrupt=2))
        self.assertEqual(out["calls"], 3)
        self.assertEqual(out["providers"]["claude"]["calls"], 3)

    def test_an_all_corrupt_ledger_does_not_report_zero_spend(self):
        """Zero readable calls and zero spend are different claims."""
        out = summarize(self.ledger(good=0, corrupt=2))
        self.assertEqual(out["calls"], 0)
        self.assertEqual(out["unread_lines"], 2)
        self.assertFalse(out["ledger_complete"])
        self.assertIn("not a measurement", out["note"])

    def test_an_empty_ledger_is_complete_and_says_so_plainly(self):
        out = summarize(self.ledger(good=0))
        self.assertEqual(out["unread_lines"], 0)
        self.assertTrue(out["ledger_complete"])
        self.assertIn("no agent calls recorded", out["note"])

    def test_blank_lines_are_not_unread_lines(self):
        tmp = self.ledger(good=2)
        with open(os.path.join(tmp, SPEND_FILE), "a", encoding="utf-8",
                  newline="\n") as fh:
            fh.write("\n\n   \n")
        out = summarize(tmp)
        self.assertEqual(out["unread_lines"], 0)


class TheStatusLineSaysSo(LedgerCase):
    def test_a_clean_ledger_shows_no_marker(self):
        self.assertNotIn("unread", statusline(self.ledger(good=3)))

    def test_unread_lines_are_marked(self):
        line = statusline(self.ledger(good=3, corrupt=2))
        self.assertIn("!2 unread", line)

    def test_the_totals_are_still_there(self):
        """The marker adds to the line; it does not replace what it qualifies."""
        line = statusline(self.ledger(good=3, corrupt=2))
        self.assertIn("total", line)
        self.assertIn("unread", line)


if __name__ == "__main__":
    unittest.main()
