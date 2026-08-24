"""S1 and S2: twelve tasks, written before any arm was wired.

S1 is six one-shot single-file bugs. It is here to be LOST by the decomposing
arm, and the proposal says so in advance: planning a one-line fix costs a call
that buys nothing, and the earlier pilot already measured exactly that. A corpus
that quietly dropped its easy stratum would report an aggregate win that came
entirely from the hard one.

S2 is six changes that must land in three or four files and stay consistent
across them. Each one has a shape where a single call plausibly fixes the first
file and forgets the third — a validator updated and its serialiser not, a
constant renamed at the definition and not at both call sites. That is the
smallest honest test of whether deciding the split first is worth a call.

Every task ships its own failing tests, and those test files are IMMUTABLE: the
runner hashes them before and after and voids a pass that edited one. An agent
with write access to the tree can make any check pass by editing the check.
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("corpus_s1_s2", 1)[0])

from runner import PilotTask  # noqa: E402

CHECK = '{python} -m unittest discover -s . -p "test_*.py" -q'

#: task_id -> (module filename, module source, its failing test source)
S1_SOURCES = {
    's1_slice': (
        'window.py',
        '"""Take a sliding window of a sequence."""\n\n\ndef window(items, index, size):\n    """Return the `index`-th window, counting from 1."""\n    start = index * size\n    return items[start:start + size]\n',
        """import unittest

from window import window


class Windows(unittest.TestCase):
    def test_the_first_window_starts_at_the_beginning(self):
        self.assertEqual(window(list(range(9)), 1, 3), [0, 1, 2])

    def test_the_second_follows_the_first(self):
        self.assertEqual(window(list(range(9)), 2, 3), [3, 4, 5])

    def test_past_the_end_is_empty(self):
        self.assertEqual(window(list(range(4)), 9, 3), [])
""",
    ),
    's1_guard': (
        'rate.py',
        '"""Convert a rate to a multiplier."""\n\n\ndef multiplier(rate_percent):\n    """A rate of 10 means 1.10."""\n    return 1 + rate_percent / 100\n',
        """import unittest

from rate import multiplier


class Rates(unittest.TestCase):
    def test_a_normal_rate(self):
        self.assertAlmostEqual(multiplier(10), 1.10)

    def test_a_rate_below_minus_one_hundred_is_refused(self):
        with self.assertRaises(ValueError):
            multiplier(-150)

    def test_a_non_numeric_rate_is_refused(self):
        with self.assertRaises(TypeError):
            multiplier("ten")
""",
    ),
    's1_default': (
        'settings.py',
        '"""Read a setting with a fallback."""\n\n\ndef setting(config, name, default=None):\n    """Return config[name], or `default` when it is absent."""\n    return config[name]\n',
        """import unittest

from settings import setting


class Settings(unittest.TestCase):
    def test_a_present_key_is_returned(self):
        self.assertEqual(setting({"a": 1}, "a"), 1)

    def test_a_missing_key_falls_back(self):
        self.assertEqual(setting({}, "a", 7), 7)

    def test_a_present_key_holding_none_is_not_the_fallback(self):
        self.assertIsNone(setting({"a": None}, "a", 7))
""",
    ),
    's1_sort': (
        'ranking.py',
        '"""Rank rows by score."""\n\n\ndef ranked(rows):\n    """Highest score first; ties broken by name, ascending."""\n    return sorted(rows, key=lambda r: r["score"])\n',
        """import unittest

from ranking import ranked


class Ranking(unittest.TestCase):
    def test_highest_first(self):
        rows = [{"name": "a", "score": 1}, {"name": "b", "score": 9}]
        self.assertEqual([r["name"] for r in ranked(rows)], ["b", "a"])

    def test_ties_break_by_name(self):
        rows = [{"name": "z", "score": 5}, {"name": "a", "score": 5}]
        self.assertEqual([r["name"] for r in ranked(rows)], ["a", "z"])

    def test_an_empty_input_is_empty(self):
        self.assertEqual(ranked([]), [])
""",
    ),
    's1_strip': (
        'tags.py',
        '"""Normalise a comma-separated tag string."""\n\n\ndef tags(raw):\n    """Lowercase, trimmed, no empties, order preserved, no duplicates."""\n    return raw.split(",")\n',
        """import unittest

from tags import tags


class Tags(unittest.TestCase):
    def test_it_trims_and_lowercases(self):
        self.assertEqual(tags(" A , b "), ["a", "b"])

    def test_it_drops_empties(self):
        self.assertEqual(tags("a,,b,"), ["a", "b"])

    def test_it_drops_duplicates_keeping_order(self):
        self.assertEqual(tags("b,a,b"), ["b", "a"])
""",
    ),
    's1_money': (
        'money.py',
        '"""Split an amount into equal parts."""\n\n\ndef split(amount_cents, parts):\n    """Split evenly; the remainder goes to the earliest parts."""\n    each = amount_cents // parts\n    return [each] * parts\n',
        """import unittest

from money import split


class Splits(unittest.TestCase):
    def test_an_even_split(self):
        self.assertEqual(split(900, 3), [300, 300, 300])

    def test_the_remainder_is_distributed_not_lost(self):
        self.assertEqual(split(1000, 3), [334, 333, 333])

    def test_zero_parts_is_refused(self):
        with self.assertRaises(ValueError):
            split(100, 0)
""",
    ),
}

S2_ORDER = {
    'models.py': '"""The order record."""\n\n\nclass Order:\n    def __init__(self, order_id, amount_cents):\n        self.order_id = order_id\n        self.amount_cents = amount_cents\n',
    'validate.py': '"""Validate an order before it is stored."""\n\n\ndef validate(order):\n    """Raise ValueError if the order is not storable."""\n    if not order.order_id:\n        raise ValueError("order_id is required")\n    if order.amount_cents < 0:\n        raise ValueError("amount_cents may not be negative")\n    return True\n',
    'serialise.py': '"""Turn an order into the wire shape."""\n\n\ndef to_wire(order):\n    return {"id": order.order_id, "amount": order.amount_cents}\n',
    'test_order.py': 'import unittest\n\nfrom models import Order\nfrom serialise import to_wire\nfrom validate import validate\n\n\nclass CurrencyIsCarriedEverywhere(unittest.TestCase):\n    """A currency field has to reach the model, the validator and the wire."""\n\n    def test_the_model_carries_a_currency(self):\n        order = Order("o-1", 500, "KRW")\n        self.assertEqual(order.currency, "KRW")\n\n    def test_the_validator_refuses_an_unknown_currency(self):\n        with self.assertRaises(ValueError):\n            validate(Order("o-1", 500, "XYZ"))\n\n    def test_the_validator_accepts_a_known_one(self):\n        self.assertTrue(validate(Order("o-1", 500, "USD")))\n\n    def test_the_wire_shape_includes_it(self):\n        self.assertEqual(to_wire(Order("o-1", 500, "KRW")),\n                         {"id": "o-1", "amount": 500, "currency": "KRW"})\n\n    def test_the_old_rules_still_hold(self):\n        with self.assertRaises(ValueError):\n            validate(Order("", 500, "USD"))\n        with self.assertRaises(ValueError):\n            validate(Order("o-1", -1, "USD"))\n',
}

S2_RENAME = {
    'limits.py': '"""Configured ceilings."""\n\nMAX_ITEMS = 50\n\n\ndef cap():\n    return MAX_ITEMS\n',
    'cart.py': '"""A shopping cart."""\n\nfrom limits import MAX_ITEMS\n\n\ndef can_add(current_count):\n    return current_count < MAX_ITEMS\n',
    'report.py': '"""Report on cart limits."""\n\nfrom limits import MAX_ITEMS\n\n\ndef describe():\n    return f"at most {MAX_ITEMS} items"\n',
    'test_rename.py': 'import unittest\n\nimport cart\nimport limits\nimport report\n\n\nclass TheConceptIsRenamedEverywhere(unittest.TestCase):\n    """MAX_ITEMS becomes MAX_LINE_ITEMS at the definition and both call sites."""\n\n    def test_the_new_name_exists(self):\n        self.assertEqual(limits.MAX_LINE_ITEMS, 50)\n\n    def test_the_old_name_is_gone(self):\n        self.assertFalse(hasattr(limits, "MAX_ITEMS"),\n                         "the old name was left behind as an alias")\n\n    def test_the_cart_still_works(self):\n        self.assertTrue(cart.can_add(1))\n        self.assertFalse(cart.can_add(50))\n\n    def test_the_report_still_works(self):\n        self.assertIn("50", report.describe())\n\n    def test_the_accessor_still_works(self):\n        self.assertEqual(limits.cap(), 50)\n',
}

S2_ERRORS = {
    'parser.py': '"""Parse a duration string."""\n\n\ndef parse_duration(text):\n    """`"90s"` -> 90. Raises on anything else."""\n    return int(text[:-1])\n',
    'errors.py': '"""Domain errors."""\n\n\nclass DomainError(Exception):\n    pass\n',
    'api.py': '"""The caller."""\n\nfrom parser import parse_duration\n\n\ndef timeout_for(text):\n    return parse_duration(text)\n',
    'test_errors.py': 'import unittest\n\nfrom api import timeout_for\nfrom errors import DomainError, ParseError\n\n\nclass ParsingFailuresAreDomainErrors(unittest.TestCase):\n    """A ParseError has to exist, subclass DomainError, and reach the caller."""\n\n    def test_parse_error_is_a_domain_error(self):\n        self.assertTrue(issubclass(ParseError, DomainError))\n\n    def test_a_bad_unit_raises_it(self):\n        with self.assertRaises(ParseError):\n            timeout_for("90x")\n\n    def test_an_empty_string_raises_it(self):\n        with self.assertRaises(ParseError):\n            timeout_for("")\n\n    def test_a_good_value_still_parses(self):\n        self.assertEqual(timeout_for("90s"), 90)\n\n    def test_the_message_names_the_input(self):\n        try:\n            timeout_for("nope")\n        except ParseError as exc:\n            self.assertIn("nope", str(exc))\n',
}

S2_PAGING = {
    'query.py': '"""Build a query."""\n\n\ndef build(table, where=None):\n    clause = f" WHERE {where}" if where else ""\n    return f"SELECT * FROM {table}{clause}"\n',
    'repo.py': '"""Run a query."""\n\nfrom query import build\n\n\ndef fetch(table, where=None):\n    return build(table, where)\n',
    'test_paging.py': 'import unittest\n\nfrom query import build\nfrom repo import fetch\n\n\nclass PagingReachesBothLayers(unittest.TestCase):\n    """limit/offset must exist in the builder AND be passed through."""\n\n    def test_the_builder_appends_limit_and_offset(self):\n        self.assertEqual(build("t", limit=10, offset=20),\n                         "SELECT * FROM t LIMIT 10 OFFSET 20")\n\n    def test_limit_without_offset(self):\n        self.assertEqual(build("t", limit=5), "SELECT * FROM t LIMIT 5")\n\n    def test_where_still_comes_first(self):\n        self.assertEqual(build("t", where="a=1", limit=5),\n                         "SELECT * FROM t WHERE a=1 LIMIT 5")\n\n    def test_the_repo_passes_them_through(self):\n        self.assertEqual(fetch("t", limit=3, offset=6),\n                         "SELECT * FROM t LIMIT 3 OFFSET 6")\n\n    def test_no_paging_is_unchanged(self):\n        self.assertEqual(fetch("t"), "SELECT * FROM t")\n',
}

S2_NORMALISE = {
    'clean.py': '"""Clean user input."""\n\n\ndef clean_name(raw):\n    return raw.strip()\n',
    'store.py': '"""Store a user."""\n\nfrom clean import clean_name\n\n\ndef save(users, raw_name):\n    users.append(clean_name(raw_name))\n    return users\n',
    'lookup.py': '"""Find a user."""\n\nfrom clean import clean_name\n\n\ndef find(users, raw_name):\n    return clean_name(raw_name) in users\n',
    'test_normalise.py': 'import unittest\n\nfrom clean import clean_name\nfrom lookup import find\nfrom store import save\n\n\nclass NormalisationIsConsistentOnBothSides(unittest.TestCase):\n    """Case folding has to happen in one place and reach store AND lookup."""\n\n    def test_cleaning_folds_case(self):\n        self.assertEqual(clean_name("  Ada  "), "ada")\n\n    def test_a_stored_name_is_folded(self):\n        self.assertEqual(save([], " Ada "), ["ada"])\n\n    def test_lookup_folds_too(self):\n        self.assertTrue(find(save([], "Ada"), "  ADA "))\n\n    def test_an_unrelated_name_is_not_found(self):\n        self.assertFalse(find(save([], "Ada"), "Grace"))\n\n    def test_internal_spacing_is_collapsed(self):\n        self.assertEqual(clean_name("Ada   Lovelace"), "ada lovelace")\n',
}

S2_AUDIT = {
    'account.py': '"""An account."""\n\n\nclass Account:\n    def __init__(self, balance=0):\n        self.balance = balance\n\n    def deposit(self, amount):\n        self.balance += amount\n\n    def withdraw(self, amount):\n        self.balance -= amount\n',
    'audit.py': '"""An audit trail."""\n\n\nclass Audit:\n    def __init__(self):\n        self.entries = []\n',
    'test_audit.py': 'import unittest\n\nfrom account import Account\nfrom audit import Audit\n\n\nclass EveryMovementIsAudited(unittest.TestCase):\n    """The trail needs a record method and both movements must use it."""\n\n    def test_a_deposit_is_recorded(self):\n        trail = Audit()\n        Account(0, trail).deposit(10)\n        self.assertEqual(trail.entries, [("deposit", 10)])\n\n    def test_a_withdrawal_is_recorded(self):\n        trail = Audit()\n        Account(50, trail).withdraw(10)\n        self.assertEqual(trail.entries, [("withdraw", 10)])\n\n    def test_the_balance_still_moves(self):\n        trail = Audit()\n        account = Account(50, trail)\n        account.deposit(10)\n        account.withdraw(20)\n        self.assertEqual(account.balance, 40)\n\n    def test_an_account_without_a_trail_still_works(self):\n        account = Account(5)\n        account.deposit(5)\n        self.assertEqual(account.balance, 10)\n\n    def test_an_overdraft_is_refused_and_not_recorded(self):\n        trail = Audit()\n        account = Account(5, trail)\n        with self.assertRaises(ValueError):\n            account.withdraw(50)\n        self.assertEqual(trail.entries, [])\n',
}


def _s1(task_id: str) -> PilotTask:
    """One file, one defect, its own failing test — the stratum that must be LOST.

    The prompt is generated rather than written per task on purpose: six tasks
    phrased six ways would put wording differences into a comparison that is
    supposed to be about decomposition.
    """
    name, source, test_source = S1_SOURCES[task_id]
    test_name = "test_" + name
    return PilotTask(
        task_id=task_id,
        prompt=(f"The tests in {test_name} fail. Fix {name} so every test "
                f"passes. Do not modify {test_name}."),
        check=CHECK,
        fixture={name: source, test_name: test_source},
        expected_paths=[name],
        immutable=[test_name],
        one_shot_plausible=True,
        rationale="one file, one known defect, a pre-written failing test",
    )


def _s2(task_id: str, files: dict, test_name: str, prompt: str,
        rationale: str) -> PilotTask:
    """Three or four files that must agree, and a test that catches a half-fix.

    The write set is declared rather than inferred: a run that touches one file
    of three is then visible as scope, instead of showing up only as a failure
    whose cause has to be guessed at afterwards.
    """
    return PilotTask(
        task_id=task_id,
        prompt=prompt,
        check=CHECK,
        fixture=dict(files),
        expected_paths=[f for f in files if f != test_name],
        immutable=[test_name],
        one_shot_plausible=False,
        rationale=rationale,
    )


def s1_corpus() -> list:
    return [_s1(task_id) for task_id in S1_SOURCES]


def s2_corpus() -> list:
    return [
        _s2('s2_currency', S2_ORDER, 'test_order.py',
            'The tests in test_order.py fail. Add a currency to the order and make it work end to end: the model must carry it, the validator must reject an unknown one against USD/KRW/EUR, and the wire shape must include it. Do not modify test_order.py.',
            'one field through three files; a partial change passes some tests'),
        _s2('s2_rename', S2_RENAME, 'test_rename.py',
            'The tests in test_rename.py fail. Rename MAX_ITEMS to MAX_LINE_ITEMS at its definition and at every call site, leaving no alias behind. Do not modify test_rename.py.',
            'a rename that must reach two importers; an alias left behind is a visible partial fix'),
        _s2('s2_errors', S2_ERRORS, 'test_errors.py',
            'The tests in test_errors.py fail. Add a ParseError that subclasses DomainError, raise it from the parser for anything unparseable with the offending input in the message, and let it reach the caller. Do not modify test_errors.py.',
            'a new type in one file, raised in a second, surfacing through a third'),
        _s2('s2_paging', S2_PAGING, 'test_paging.py',
            'The tests in test_paging.py fail. Add limit and offset support to the query builder and pass them through the repository. Do not modify test_paging.py.',
            'two files, and the ordering of clauses is a detail a partial fix gets wrong'),
        _s2('s2_normalise', S2_NORMALISE, 'test_normalise.py',
            'The tests in test_normalise.py fail. Make name cleaning fold case and collapse internal spacing, in ONE place, so storing and looking up agree. Do not modify test_normalise.py.',
            'a change in one file that must hold for two callers; fixing only the store makes lookup wrong'),
        _s2('s2_audit', S2_AUDIT, 'test_audit.py',
            'The tests in test_audit.py fail. Give the audit trail a way to record movements, have the account accept an optional trail and use it for deposits and withdrawals, and refuse an overdraft without recording it. Do not modify test_audit.py.',
            'a new method in one file, a changed constructor in another, and a rule that must not record the refused case'),
    ]


def corpus() -> list:
    return s1_corpus() + s2_corpus()
