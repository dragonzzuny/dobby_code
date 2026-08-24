"""The four-axis split, and the two places it refuses to flatter a run."""

import unittest

from dobby.providers.usage import Usage
from dobby.providers.usage_axes import (AXES, CALL_FLOOR_TOKENS,
                                        TOOL_SURCHARGE, axes,
                                        axes_for_record, compare, per_unit)


def record(**providers):
    return {"providers": {
        pid: {"calls_total": row.pop("calls", 1), "usage": row}
        for pid, row in providers.items()}}


class TestAxes(unittest.TestCase):
    def test_a_usage_envelope_splits_four_ways(self):
        split = axes({"input_tokens": 10, "output_tokens": 200,
                      "thinking_tokens": 50, "cache_read_tokens": 30000,
                      "cache_creation_tokens": 22000})
        self.assertEqual(split["fresh_input"], 10)
        self.assertEqual(split["generated"], 250)
        self.assertEqual(split["prefix_reread"], 30000)
        self.assertEqual(split["prefix_write"], 22000)
        self.assertEqual(split["total"], 52260)

    def test_thinking_is_generated_not_input(self):
        split = axes({"output_tokens": 1, "thinking_tokens": 999})
        self.assertEqual(split["generated"], 1000)
        self.assertEqual(split["fresh_input"], 0)

    def test_a_missing_envelope_is_zero_not_a_crash(self):
        self.assertEqual(axes(None)["total"], 0)
        self.assertEqual(axes({})["total"], 0)

    def test_a_dataclass_envelope_is_accepted(self):
        split = axes(Usage(provider="claude", input_tokens=5, output_tokens=7))
        self.assertEqual(split["fresh_input"], 5)
        self.assertEqual(split["generated"], 7)


class TestRecords(unittest.TestCase):
    def test_providers_are_summed_and_kept_apart(self):
        got = axes_for_record(record(
            claude={"calls": 2, "calls_measured": 2, "input_tokens": 10,
                    "output_tokens": 100, "cache_creation_tokens": 60000},
            codex={"calls": 3, "calls_measured": 3, "input_tokens": 150000,
                   "output_tokens": 2000}))
        self.assertEqual(got["per_provider"]["claude"]["prefix_write"], 60000)
        self.assertEqual(got["per_provider"]["codex"]["fresh_input"], 150000)
        self.assertEqual(got["totals"]["fresh_input"], 150010)
        self.assertEqual(got["calls"], 5)
        self.assertTrue(got["complete"])
        self.assertEqual(got["note"], "")

    def test_an_unmeasured_call_makes_the_total_a_floor(self):
        got = axes_for_record(record(
            agy={"calls": 4, "calls_measured": 1, "input_tokens": 900}))
        self.assertFalse(got["complete"])
        self.assertIn("FLOOR", got["note"])
        self.assertIn("3 of 4", got["note"])


class TestPerUnit(unittest.TestCase):
    def test_division_is_by_successes_not_attempts(self):
        totals = {a: 100 for a in AXES}
        totals["total"] = 400
        self.assertEqual(per_unit(totals, 4)["total"], 100.0)

    def test_zero_successes_is_none_rather_than_zero(self):
        """Zero would sort as the cheapest arm in a table. It solved nothing."""
        got = per_unit({a: 999 for a in AXES}, 0)
        self.assertIsNone(got["total"])


class TestCompare(unittest.TestCase):
    def _pair(self):
        return {
            "B_codex": {"totals": {"prefix_write": 0, "prefix_reread": 100000,
                                   "fresh_input": 20000, "generated": 4000,
                                   "total": 124000}, "units": 10},
            "D_dobby": {"totals": {"prefix_write": 300000,
                                   "prefix_reread": 500000,
                                   "fresh_input": 1000, "generated": 8000,
                                   "total": 809000}, "units": 10},
        }

    def test_ratios_are_against_the_named_baseline(self):
        got = compare(self._pair(), baseline="B_codex")
        self.assertEqual(got["baseline"], "B_codex")
        self.assertEqual(got["arms"]["B_codex"]["ratio_to_baseline"]["total"], 1.0)
        self.assertEqual(got["arms"]["D_dobby"]["ratio_to_baseline"]["total"],
                         round(809000 / 124000, 2))

    def test_an_axis_the_baseline_never_spent_is_none_not_infinity(self):
        got = compare(self._pair(), baseline="B_codex")
        self.assertIsNone(
            got["arms"]["D_dobby"]["ratio_to_baseline"]["prefix_write"],
            "codex reports no cache write, so the ratio has no denominator "
            "and must not be rendered as a win or a loss")

    def test_solving_fewer_tasks_does_not_read_as_cheaper(self):
        pair = self._pair()
        pair["D_dobby"]["units"] = 2          # same spend, a fifth of the work
        got = compare(pair, baseline="B_codex")
        self.assertGreater(got["arms"]["D_dobby"]["ratio_to_baseline"]["total"],
                           compare(self._pair(), baseline="B_codex")
                           ["arms"]["D_dobby"]["ratio_to_baseline"]["total"])

    def test_an_unknown_baseline_is_an_error(self):
        with self.assertRaises(KeyError):
            compare(self._pair(), baseline="A_claude")


class TestMeasuredConstants(unittest.TestCase):
    def test_the_tool_surcharges_are_ordered_by_surface(self):
        self.assertEqual(TOOL_SURCHARGE[""], 0)
        self.assertLess(TOOL_SURCHARGE["Read"], TOOL_SURCHARGE["Read,Edit,Bash"])
        self.assertLess(TOOL_SURCHARGE["Read,Edit,Bash"],
                        TOOL_SURCHARGE["default"])

    def test_the_floor_dwarfs_every_tool_set(self):
        """The point of the number: most of a call's prefix is not tools."""
        self.assertGreater(CALL_FLOOR_TOKENS, 2 * TOOL_SURCHARGE["default"])


if __name__ == "__main__":
    unittest.main()
