"""Opening a shift must not cost more for every item already finished.

`open_session` asks the runtime store whether any run left an external effect
unconfirmed, and it asks once per run the portfolio has ever produced. Each ask
opened its own SQLite connection. Measured on this machine:

    completed items   connections in open_session   open_session
                  1                             7         0.220s
                100                           106         1.011s

`open_session` is itself called once per item worked, so that is O(items) per
shift and O(items^2) to work a portfolio -- the project layer's version of the
defect `metrics.report` had, where reading about work already finished made new
work slower.

    after: 7 connections at 1 item and at 100, 0.386s

The assertions count CONNECTIONS, not seconds, for the same reason as
`test_metrics_scale`: a wall-clock threshold measured here is a flake on a
slower machine, and what a later edit would remove by accident is the session.
The count staying FLAT is the property; its exact value is not, since a
different number of unrelated store reads in `open_session` would change it
without changing what is being tested.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise  # noqa: E402
from dobby.project import loop as L  # noqa: E402
from dobby.project.session import _unconfirmed_by_run  # noqa: E402
from dobby.project.session import open_session  # noqa: E402
from dobby.runtime import store as runtime_store  # noqa: E402
from dobby.runtime.store import RunStore  # noqa: E402

CHECK = 'python -c "import sys; sys.exit(0)"'


def connections_during(call):
    """How many times `runtime.store.connect` was called during `call`."""
    original = runtime_store.connect
    count = {"n": 0}

    def counted(path):
        count["n"] += 1
        return original(path)

    runtime_store.connect = counted
    try:
        call()
    finally:
        runtime_store.connect = original
    return count["n"]


class PortfolioCase(unittest.TestCase):
    """A real project, advanced far enough to have a history to walk."""

    ITEMS = 24
    #: Enough finished items that a per-run connection would be visible, and
    #: few enough that building the fixture is not the slowest test here.
    COMPLETED = 8

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        with open(os.path.join(cls.tmp, "app.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("x = 1\n")
        cls.data = os.path.join(cls.tmp, ".dobby")
        initialise(cls.data, cls.tmp, smoke=(CHECK,),
                   item_specs=[{"work_item_id": f"W{i:03d}",
                                "title": f"item {i}",
                                "outcome": f"do thing {i}",
                                "acceptance_checks": [CHECK]}
                               for i in range(cls.ITEMS)],
                   run_baseline=False)
        store = ProjectStore(cls.data)
        while cls.done_count(store) < cls.COMPLETED:
            L.advance(cls.data, max_items=1,
                      execute_command='python -c "pass"')
        cls.finished = cls.done_count(store)

    @staticmethod
    def done_count(store):
        return sum(1 for i in store.load_project(None)["portfolio"].items
                   if i.state == "DONE")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class OpeningAShiftIsFlat(PortfolioCase):
    def test_the_fixture_really_did_finish_items(self):
        """The premise. With nothing finished there is no history to walk and
        a per-run connection would be invisible."""
        self.assertGreaterEqual(self.finished, self.COMPLETED)

    def test_open_session_does_not_open_one_connection_per_finished_run(self):
        used = connections_during(
            lambda: open_session(self.data, rebaseline=False))
        self.assertLess(
            used, self.finished,
            f"{used} connections for {self.finished} finished items: the walk "
            f"is opening one per run again")

    def test_the_walk_itself_opens_exactly_one(self):
        """`_unconfirmed_by_run` is the part that scales with history."""
        store = RunStore(self.data)
        runs = [i.latest_run_id
                for i in ProjectStore(self.data).load_project(
                    None)["portfolio"].items]
        used = connections_during(lambda: _unconfirmed_by_run(store, runs))
        self.assertEqual(used, 1)

    def test_it_still_finds_what_it_is_looking_for(self):
        """A cheaper walk that returns something else is not the same walk.

        These runs used no external effects, so the honest expectation is an
        empty map -- and an empty map from a walk that never ran would look the
        same, which is why the connection count above is asserted separately.
        """
        store = RunStore(self.data)
        runs = [i.latest_run_id
                for i in ProjectStore(self.data).load_project(
                    None)["portfolio"].items]
        self.assertEqual(_unconfirmed_by_run(store, runs), {})

    def test_no_run_ids_is_still_one_connection_and_no_walk(self):
        store = RunStore(self.data)
        self.assertEqual(_unconfirmed_by_run(store, []), {})

    def test_none_entries_are_skipped_rather_than_queried(self):
        """An item that has never run has no `latest_run_id`."""
        store = RunStore(self.data)
        self.assertEqual(_unconfirmed_by_run(store, [None, None]), {})

    def test_the_session_is_released(self):
        store = RunStore(self.data)
        _unconfirmed_by_run(store, [None])
        self.assertIsNone(getattr(store._local, "conn", None))


class TheControl(PortfolioCase):
    """Without the session the same walk costs one connection per run.

    Here so the flat numbers above are evidence rather than a formality: if the
    counter stops counting, this fails and says so.
    """

    def test_a_per_run_walk_opens_a_connection_per_run(self):
        store = RunStore(self.data)
        runs = sorted({i.latest_run_id
                       for i in ProjectStore(self.data).load_project(
                           None)["portfolio"].items if i.latest_run_id})
        if len(runs) < 2:
            self.skipTest("needs at least two finished runs to tell apart")

        def without_session():
            for run_id in runs:
                store.unconfirmed_effects(run_id)

        used = connections_during(without_session)
        self.assertEqual(used, len(runs),
                         "the connection counter is not counting")


if __name__ == "__main__":
    unittest.main()
