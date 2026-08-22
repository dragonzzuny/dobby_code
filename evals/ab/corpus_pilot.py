"""The pilot corpus: three tasks, each with the reason it was chosen recorded.

Selection ran against `DESIGN.md`'s criteria, which were written before any
candidate was looked at. Every task here is `one_shot_plausible: True` — a
competent single call could finish it. That direction is deliberate and it biases
the corpus AGAINST the structured arms: criterion 6 forbids choosing tasks for
having sub-steps, because multi-step work favours the arm that structures work
and a corpus of those would rig the result.

Each task ships its own failing test, and that test is IMMUTABLE. An agent with
write rights to the tree can make any check pass by editing the check;
`docs/FAILURE_CATALOG.md` calls that Evaluation Gaming and treats it as task
failure. The runner hashes these files before and after and voids a pass that
modified one — for every arm equally, including the one that has no gate.

The bug in each fixture is a real shape, not a puzzle: an off-by-one in a slice,
a missing guard on an input, and a total that ignores a field. None of them is a
failure mode dobby classifies specially (criterion 7), and none is drawn from
this repository's recent work (criterion 8).
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("evals", 1)[0])

from runner import PilotTask  # noqa: E402  (same directory)

CHECK = '{python} -m unittest discover -s . -p "test_*.py" -q'


PAGINATE_SRC = '''"""Paginate a list of records."""


def page(items, page_number, per_page):
    """Return the items on `page_number`, counting pages from 1."""
    start = page_number * per_page
    return items[start:start + per_page]
'''

PAGINATE_TEST = '''import unittest

from paginate import page


class PageBoundaries(unittest.TestCase):
    def test_the_first_page_starts_at_the_beginning(self):
        self.assertEqual(page(list(range(10)), 1, 3), [0, 1, 2])

    def test_the_second_page_follows_the_first(self):
        self.assertEqual(page(list(range(10)), 2, 3), [3, 4, 5])

    def test_a_page_past_the_end_is_empty(self):
        self.assertEqual(page(list(range(5)), 9, 3), [])


if __name__ == "__main__":
    unittest.main()
'''


DISCOUNT_SRC = '''"""Apply a percentage discount to a price."""


def discounted(price, percent):
    """Return `price` reduced by `percent`."""
    return price * (1 - percent / 100)
'''

DISCOUNT_TEST = '''import unittest

from discount import discounted


class DiscountRules(unittest.TestCase):
    def test_a_normal_discount_applies(self):
        self.assertAlmostEqual(discounted(200.0, 10), 180.0)

    def test_a_negative_percentage_is_refused(self):
        with self.assertRaises(ValueError):
            discounted(200.0, -5)

    def test_a_percentage_over_one_hundred_is_refused(self):
        with self.assertRaises(ValueError):
            discounted(200.0, 120)


if __name__ == "__main__":
    unittest.main()
'''


INVOICE_SRC = '''"""Total an invoice."""


def total(lines):
    """Sum every line. Each line is {"amount": float, "tax": float}."""
    return sum(line["amount"] for line in lines)
'''

INVOICE_TEST = '''import unittest

from invoice import total


class InvoiceTotals(unittest.TestCase):
    def test_it_includes_tax(self):
        lines = [{"amount": 100.0, "tax": 10.0},
                 {"amount": 50.0, "tax": 5.0}]
        self.assertAlmostEqual(total(lines), 165.0)

    def test_an_empty_invoice_totals_zero(self):
        self.assertAlmostEqual(total([]), 0.0)

    def test_a_line_without_tax_is_treated_as_untaxed(self):
        self.assertAlmostEqual(total([{"amount": 20.0}]), 20.0)


if __name__ == "__main__":
    unittest.main()
'''


def pilot_corpus() -> list:
    return [
        PilotTask(
            task_id="paginate_offbyone",
            prompt=("The tests in test_paginate.py fail. Fix paginate.py so "
                    "every test passes. Do not modify test_paginate.py."),
            check=CHECK,
            fixture={"paginate.py": PAGINATE_SRC,
                     "test_paginate.py": PAGINATE_TEST},
            expected_paths=["paginate.py"],
            immutable=["test_paginate.py"],
            one_shot_plausible=True,
            rationale=("off-by-one in a slice: page 1 returns items 3-5. A "
                       "single competent call plausibly fixes it, which is why "
                       "it is in a corpus meant not to favour the structured "
                       "arms")),
        PilotTask(
            task_id="discount_validation",
            prompt=("The tests in test_discount.py fail. Fix discount.py so "
                    "every test passes. Do not modify test_discount.py."),
            check=CHECK,
            fixture={"discount.py": DISCOUNT_SRC,
                     "test_discount.py": DISCOUNT_TEST},
            expected_paths=["discount.py"],
            immutable=["test_discount.py"],
            one_shot_plausible=True,
            rationale=("a missing input guard: the function accepts any "
                       "percentage. Two of the three tests fail, so a partial "
                       "fix is possible and visible")),
        PilotTask(
            task_id="invoice_missing_field",
            prompt=("The tests in test_invoice.py fail. Fix invoice.py so "
                    "every test passes. Do not modify test_invoice.py."),
            check=CHECK,
            fixture={"invoice.py": INVOICE_SRC,
                     "test_invoice.py": INVOICE_TEST},
            expected_paths=["invoice.py"],
            immutable=["test_invoice.py"],
            one_shot_plausible=True,
            rationale=("a total that ignores a field, plus an optional-key case "
                       "that punishes a naive fix. The naive `line['tax']` "
                       "raises KeyError on the third test, so the task "
                       "distinguishes a read of the failure from a guess")),
    ]
