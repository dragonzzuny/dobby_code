"""A failed attempt that says why, from an adapter that did not classify it.

`WorkerResult.failure` is optional and every adapter in this package fills it,
so this path never fires here. It fires for anybody who writes their own --
which the `WorkerAdapter` contract explicitly invites -- and the runner used to
answer with:

    Failure("NON_RETRYABLE", "the worker failed without a class")

Measured with an adapter returning `WorkerResult(False, raw="rate limit
exceeded")`:

    before   n1 FAILED, NON_RETRYABLE, 1 attempt, the run dead
    after    n1 SUCCEEDED, CAPACITY, 2 attempts, the run finished

A rate limit is the textbook retryable condition and `DEFAULT_POLICY` already
knows what to do with it. The reason was sitting in `raw` and nothing read it.

`classify_provider_error` is the same classifier `ProviderWorker` uses, on the
same kind of text, so a custom adapter gets the same answer for the same words
rather than a worse one for not having done the classification itself. The
answer is still NON_RETRYABLE when there is no text -- a failure that says
nothing is not a failure anybody can retry into.

Not a defect anything in this repository could hit today. It is a defect in the
CONTRACT: the class is documented as optional and the consequence of omitting
it was a run that ends on something the policy table would have retried.
"""

import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import RunBudget  # noqa: E402
from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import ArtifactContract, SCHEMAS  # noqa: E402
from dobby.runtime.failures import Failure  # noqa: E402
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.workers import (WorkerAdapter, WorkerRegistry,  # noqa: E402
                                   WorkerResult)

FLEET = {"claude", "codex", "gemini"}


class Unclassified(WorkerAdapter):
    """Fails once on `n1` with text and no `Failure`, then succeeds."""

    name = "provider"

    def __init__(self, raw, *, fail_on="n1", failure=None):
        self.raw, self.fail_on, self.failure = raw, fail_on, failure
        self.calls = {}

    def run(self, node, context):
        self.calls[node.node_id] = self.calls.get(node.node_id, 0) + 1
        if node.node_id == self.fail_on and self.calls[node.node_id] == 1:
            return WorkerResult(False, raw=self.raw, failure=self.failure)
        return WorkerResult(True,
                            payload={"steps": [{"what": "do the thing"}]})


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def chain(self, n=3):
        nodes, previous = [], None
        for i in range(n):
            nodes.append(G.TaskNode(
                node_id=f"n{i}", kind="plan", worker="provider",
                instruction="i", depends_on=([previous] if previous else []),
                contract=ArtifactContract(output_schema=SCHEMAS["plan"]),
                config={"provider": "claude"}))
            previous = f"n{i}"
        return G.TaskGraph(nodes)

    def drive(self, worker):
        runner = Runner(repo=self.tmp,
                        data_dir=os.path.join(self.tmp, "d"),
                        workers=WorkerRegistry({"provider": worker}),
                        available_providers=FLEET, sleep=lambda _s: None)
        result = runner.run(runner.start("t", self.chain()),
                            budget=RunBudget(max_attempts=20))
        steps = {s["node_id"]: s for s in result.to_dict()["steps"]}
        return result, steps


class TheReasonInRawIsRead(RunnerCase):
    def test_a_rate_limit_is_retried_and_the_run_finishes(self):
        result, steps = self.drive(Unclassified("rate limit exceeded"))
        self.assertEqual(result.state, G.SUCCEEDED, steps)
        self.assertEqual(steps["n1"]["attempts"], 2)

    def test_it_is_classified_as_capacity_and_not_as_unclassed(self):
        _result, steps = self.drive(Unclassified("rate limit exceeded"))
        self.assertEqual(steps["n1"]["failure"]["class"], "CAPACITY")

    def test_a_transient_fault_is_retried_too(self):
        result, steps = self.drive(Unclassified("connection reset"))
        self.assertEqual(result.state, G.SUCCEEDED, steps)
        self.assertEqual(steps["n1"]["failure"]["class"], "TRANSIENT_PROVIDER")

    def test_an_auth_failure_is_still_permanent(self):
        """Reading the text must not turn every failure into a retry."""
        result, steps = self.drive(
            Unclassified("not logged in", fail_on="n1"))
        self.assertEqual(result.state, G.FAILED)
        self.assertEqual(steps["n1"]["failure"]["class"], "NON_RETRYABLE")
        self.assertEqual(steps["n1"]["attempts"], 1)

    def test_silence_is_still_unclassed_and_permanent(self):
        """A failure that says nothing is not one anybody can retry into."""
        result, steps = self.drive(Unclassified(""))
        self.assertEqual(result.state, G.FAILED)
        self.assertEqual(steps["n1"]["failure"]["class"], "NON_RETRYABLE")


class AnAdapterThatDoesClassifyIsUntouched(RunnerCase):
    """The class an adapter gives always wins; the text is only a fallback."""

    def test_a_declared_class_is_used_as_given(self):
        _result, steps = self.drive(Unclassified(
            "rate limit exceeded",
            failure=Failure("PERMISSION_DENIED", "the operator said no")))
        self.assertEqual(steps["n1"]["failure"]["class"], "PERMISSION_DENIED")

    def test_a_declared_class_is_not_second_guessed_by_the_text(self):
        """`rate limit` in the raw would classify as CAPACITY. It must not
        override an adapter that said something else on purpose."""
        _result, steps = self.drive(Unclassified(
            "rate limit exceeded",
            failure=Failure("NON_RETRYABLE", "this one is final")))
        self.assertEqual(steps["n1"]["failure"]["class"], "NON_RETRYABLE")
        self.assertIn("final", steps["n1"]["failure"]["detail"])


class ThePackagesOwnAdaptersAllClassify(unittest.TestCase):
    """Which is why this was never reachable from inside the repository."""

    def test_no_worker_returns_a_failure_without_a_class(self):
        import ast

        offenders = []
        for base, _dirs, names in os.walk(os.path.join(REPO, "dobby")):
            if "__pycache__" in base:
                continue
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for call in ast.walk(tree):
                    if not isinstance(call, ast.Call):
                        continue
                    func = call.func
                    if (getattr(func, "id", None) != "WorkerResult"
                            and getattr(func, "attr", None) != "WorkerResult"):
                        continue
                    args = call.args
                    failed = (args and isinstance(args[0], ast.Constant)
                              and args[0].value is False)
                    classed = any(k.arg == "failure" for k in call.keywords)
                    if failed and not classed:
                        offenders.append(
                            f"{os.path.relpath(path, REPO)}:{call.lineno}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
