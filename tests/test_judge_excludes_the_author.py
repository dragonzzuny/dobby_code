"""The critic must not be the provider that did the work.

`AGENTS.md` states it as a rule for people -- "your own second pass is
correlated with your first, and reporting it as independent corroboration is
the failure `dobby/swarm/diversity.py` exists to catch" -- and the compiler
enforced it against the wrong name.

`workorder.compile_graph` writes the critic's `exclude` from the provider it
was HANDED. `Runner._place` re-decides per node and rewrites
`node.config["provider"]`, and the judge node is not a provider-worker so it
resolves its own. Measured on a compiled work order:

    compile-time argument                        claude
    critic exclude                               ["claude"]
    implement-1 actually ran on                  codex
    resolve_role("critic", exclude={"claude"})    codex

The author grading its own output, past a rule that was being enforced. Nothing
was bypassed; the exclusion named a provider that never touched the work.

The fix is to take the name from the artifact rather than from the compiler's
argument: `Runner._input_authors` reads which provider produced each promoted
dependency and puts it in the worker context, and `AdvisoryJudgeWorker` unions that
into `exclude`. Union rather than replace -- the compile-time entry may name a
provider that must not judge for a reason no artifact records.
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import architecture as A  # noqa: E402
from dobby.project import workorder as W  # noqa: E402
from dobby.project.models import ProjectManifest, WorkItem  # noqa: E402
from dobby.runtime import graph as G  # noqa: E402
from dobby.runtime.contracts import ArtifactContract, LOCAL_WRITE  # noqa: E402
from dobby.runtime.runner import Runner  # noqa: E402
from dobby.runtime.workers import (AdvisoryJudgeWorker,  # noqa: E402
                                   WorkerAdapter,
                                   WorkerRegistry, WorkerResult)

PATCH = {"base_commit": "0" * 40,
         "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n",
         "changed_files": ["app.py"],
         "summary": "고쳤다. 로그에 다 있었다. 지금은 통과한다."}
PROSE = {"summary": "고쳤다. 로그에 다 있었다. 지금은 통과한다.",
         "not_established": []}


class Author(WorkerAdapter):
    """Answers like a provider worker, and reports who it was in `meta`."""

    name = "provider"

    def __init__(self):
        self.used: dict = {}

    def run(self, node, context):
        provider = node.config.get("provider")
        self.used[node.node_id] = provider
        payload = PROSE if node.node_id == "report" else dict(PATCH)
        return WorkerResult(True, payload=payload,
                            meta={"provider": provider})


class Spy(WorkerAdapter):
    """A judge that records what it was told to keep out, and judges nothing."""

    name = "judge"

    def __init__(self):
        self.context: dict = {}
        self.config: dict = {}

    def run(self, node, context):
        self.context = dict(context)
        self.config = dict(node.config)
        return WorkerResult(True,
                            payload={"verdict_token": "PASS", "evidence": "x"},
                            meta={"advisory": True})


class ARealWorkOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "app.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("a\n")
        self.manifest = ProjectManifest(project_id="p", root=self.root,
                                        repo_digest="d",
                                        smoke_checks=("pytest -q",))
        self.plan = A.PlanSpec(
            plan_id="pl", work_item_id="W1", objective="paginate",
            side_effect_class=LOCAL_WRITE,
            execution_steps=({"role": "implement",
                              "objective": "add the param",
                              "write_set": ["app.py"],
                              "read_set": ["app.py"]},
                             {"role": "critic",
                              "objective": "is it validated"}))
        self.item = WorkItem(
            work_item_id="W1", project_id="p", title="t",
            outcome="make it paginate",
            acceptance_checks=['python -c "import sys; sys.exit(0)"'])
        self.author, self.spy = Author(), Spy()

    def run_it(self, provider="claude"):
        graph = W.compile_graph(self.plan, item=self.item,
                                manifest=self.manifest, provider=provider)
        runner = Runner(repo=self.root,
                        data_dir=os.path.join(self.root, "d"),
                        workers=WorkerRegistry({"provider": self.author,
                                                "judge": self.spy}),
                        sleep=lambda _s: None)
        return runner.run(runner.start("t", graph))

    def effective_exclude(self):
        """What the judge worker would keep out, from the same two sources."""
        exclude = set(self.spy.config.get("exclude") or ())
        author = (self.spy.context.get("input_authors") or {}).get(
            self.spy.config.get("judge_of"))
        if author:
            exclude.add(author)
        return exclude

    def test_the_run_completes(self):
        result = self.run_it()
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())

    def test_the_runner_reports_who_actually_authored_the_input(self):
        self.run_it()
        authors = self.spy.context.get("input_authors")
        self.assertEqual(authors, {"implement-1":
                                   self.author.used["implement-1"]})

    def test_the_real_author_is_excluded_however_placement_decided(self):
        """The defect. The compile-time name may be nobody who touched it."""
        self.run_it()
        self.assertIn(self.author.used["implement-1"], self.effective_exclude())

    def test_the_compile_time_name_is_kept_as_well(self):
        """Union, not replace: it may name a provider that must not judge for a
        reason no artifact records."""
        self.run_it()
        self.assertIn("claude", self.effective_exclude())

    def test_the_chosen_critic_is_not_the_author(self):
        from dobby.providers.detect import resolve_role

        self.run_it()
        author = self.author.used["implement-1"]
        chosen = resolve_role("critic", exclude=self.effective_exclude())
        if chosen is None:
            self.skipTest("this machine has no third provider to judge with")
        self.assertNotEqual(chosen, author)


class TheWorkerItself(unittest.TestCase):
    """`AdvisoryJudgeWorker` in isolation, so the union is tested without a full run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.seen: dict = {}

        def fake_judge(criterion, artifact, *, provider_id=None,
                       exclude=None, cwd=None):
            self.seen = {"exclude": set(exclude or ()),
                         "provider_id": provider_id}
            return {"verdict_token": "PASS", "evidence": "ok",
                    "judge_provider": "gemini"}

        import dobby.judge as judge_module
        self.original = judge_module.judge_criterion
        judge_module.judge_criterion = fake_judge
        self.addCleanup(self.restore)

    def restore(self):
        import dobby.judge as judge_module
        judge_module.judge_criterion = self.original

    def node(self, **config):
        base = {"criterion": {"id": "c", "description": "d"},
                "judge_of": "implement-1"}
        base.update(config)
        return G.TaskNode(node_id="critic", kind="critic", worker="judge",
                          instruction="i",
                          contract=ArtifactContract(
                              output_schema={"type": "object"}),
                          config=base)

    def call(self, node, **context):
        base = {"repo": self.tmp.name, "attempt": 0, "isolated": False,
                "inputs": {"implement-1": {"done": True}}, "run_id": "r"}
        base.update(context)
        return AdvisoryJudgeWorker().run(node, base)

    def test_the_author_is_added_to_the_declared_exclusion(self):
        self.call(self.node(exclude=["claude"]),
                  input_authors={"implement-1": "codex"})
        self.assertEqual(self.seen["exclude"], {"claude", "codex"})

    def test_it_works_with_no_declared_exclusion_at_all(self):
        self.call(self.node(), input_authors={"implement-1": "codex"})
        self.assertEqual(self.seen["exclude"], {"codex"})

    def test_an_author_that_is_not_a_provider_contributes_nothing(self):
        """A command worker has no provider, so there is nobody to exclude."""
        self.call(self.node(exclude=["claude"]), input_authors={})
        self.assertEqual(self.seen["exclude"], {"claude"})

    def test_with_no_judge_of_every_author_present_is_excluded(self):
        """A judge grading the whole input set is judging all of their work."""
        node = self.node()
        node.config.pop("judge_of")
        self.call(node, inputs={"a": {}, "b": {}},
                  input_authors={"a": "codex", "b": "gemini"})
        self.assertEqual(self.seen["exclude"], {"codex", "gemini"})

    def test_what_was_excluded_travels_in_the_meta(self):
        """So a report can show the independence rather than assert it."""
        result = self.call(self.node(exclude=["claude"]),
                           input_authors={"implement-1": "codex"})
        self.assertEqual(result.meta.get("judge_excluded"),
                         ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
