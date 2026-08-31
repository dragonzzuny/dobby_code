"""A rejected plan gets one more attempt, with the rejection handed back.

`architecture.evaluate` refuses a plan for reasons it states as sentences,
deterministically -- "the plan drops acceptance check(s) already on this item
[...]. An architect may add to the definition of done and may never narrow it".
The loop mapped that outcome straight onto a STOP.

Measured, `reports/RESULTS_three_arm_regression.md`, `django__django-13121`:

    dobby   plan_rejected   1 provider call   0/1 fixed
    claude  ok              1 provider call   1/1 fixed

The refusal was correct: the plan really did narrow the definition of done.
Ending the run there was the waste. The budget allowed five calls and one was
spent, on an instance a single call solved.

Nothing here infers anything. The rejection is a machine's finding about a
specific plan, `ArchitectureRequest.failure_context` already carried such text
into the architect's prompt, and the reason travels verbatim -- paraphrasing it
would put the loop's reading of the gate in front of what the gate said.

Bounded at ONE repair. Two is a budget decision nobody made, and a plan
rejected twice for the same stated reason is a provider that cannot act on that
reason rather than one that missed it.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import loop as L  # noqa: E402

CHECK = 'python -c "import sys; sys.exit(0)"'


class Proposer:
    """Answers with a narrow plan first, then whatever the flags say.

    A NARROW plan omits the acceptance checks the item already carries, which
    is the shape `evaluate` refuses and the shape that stopped the run on
    `django__django-13121`.
    """

    def __init__(self, *, narrow_first=True, always_narrow=False):
        self.narrow_first = narrow_first
        self.always_narrow = always_narrow
        self.contexts = []

    def __call__(self, request):
        self.contexts.append(tuple(request.failure_context))
        narrow = self.always_narrow or (self.narrow_first
                                        and len(self.contexts) == 1)
        # `proposed_acceptance_checks` is the key `evaluate` requires. A first
        # version of this fixture wrote `acceptance_checks` and BOTH plans were
        # rejected for the missing key -- so the repair looked like it worked
        # while the fixture had never produced a valid plan at all. The test was
        # reading `stop_reason`, which the result does not have, and a missing
        # key reads as None, which read as success.
        return {"objective": "add the page param",
                "side_effect_class": "LOCAL_WRITE",
                # NARROW drops the check the item already carries, which is what
                # `evaluate` refuses and what stopped django__django-13121.
                "proposed_acceptance_checks": ([] if narrow else [CHECK]),
                "execution_steps": [{"role": "implement",
                                     "objective": "add it",
                                     "write_set": ["app.py"],
                                     "read_set": ["app.py"]}]}


class LoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "app.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("x = 1\n")
        self.data = os.path.join(self.root, ".dobby")
        from dobby.project import initialise

        initialise(self.data, self.root, smoke=(CHECK,),
                   item_specs=[{"work_item_id": "W1", "title": "paginate",
                                "outcome": "make it paginate",
                                "acceptance_checks": [CHECK],
                                "uncertainty": 3}],
                   run_baseline=False)

    def advance(self, propose):
        return L.advance(self.data, architect=True, propose=propose,
                         max_items=1,
                         execute_command='python -c "pass"')


class ARejectedPlanIsRepairedOnce(LoopCase):
    def test_the_architect_is_asked_again(self):
        propose = Proposer()
        self.advance(propose)
        self.assertEqual(len(propose.contexts), 2,
                         "the loop stopped on the first rejection")

    def test_the_first_call_carries_no_context(self):
        propose = Proposer()
        self.advance(propose)
        self.assertEqual(propose.contexts[0], ())

    def test_the_second_call_carries_the_rejection_verbatim(self):
        propose = Proposer()
        self.advance(propose)
        second = propose.contexts[1]
        self.assertTrue(second, "the repair was asked with nothing to act on")
        self.assertIn("REJECTED", second[0])
        self.assertTrue(any("acceptance" in line or "omits" in line
                            for line in second),
                        f"the gate's reason did not travel: {second}")

    def test_a_repaired_plan_lets_the_loop_continue(self):
        propose = Proposer()
        result = self.advance(propose)
        self.assertNotEqual(result.get("stopped"), L.PLAN_REJECTED, result)


class ItIsBoundedAtOne(LoopCase):
    def test_a_plan_rejected_twice_stops_the_loop(self):
        """A provider that cannot act on the reason is not asked a third time."""
        propose = Proposer(always_narrow=True)
        result = self.advance(propose)
        self.assertEqual(len(propose.contexts), 2)
        self.assertEqual(result.get("stopped"), L.PLAN_REJECTED, result)

    def test_the_ceiling_is_stated_rather_than_hidden(self):
        self.assertEqual(L.PLAN_REPAIR_ATTEMPTS, 1)

    def test_a_plan_accepted_first_time_costs_one_call(self):
        """The repair must not become a tax on the ordinary path."""
        propose = Proposer(narrow_first=False)
        self.advance(propose)
        self.assertEqual(len(propose.contexts), 1)


if __name__ == "__main__":
    unittest.main()
