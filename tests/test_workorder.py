"""Compiling a plan, and the eleven things a plan is not allowed to compile into.

`execution_steps` was durable data nothing read. Making something read it is the
easy half; the half worth testing is that reading it did not turn the architect's
document into a request for capability that gets granted.

So most of this file is refusals. A step says "write these files" and "run as
this role", and a translator would hand over whatever was asked. Each test below
names a thing a plan asked for and asserts it was refused rather than repaired,
because a compiler that silently drops a second writing step is a compiler that
reports success for a graph the architect did not propose.

The compilation tests then check the one property that justifies letting a model
shape the middle at all: the tail is not proposable. `verify` carries the ITEM's
acceptance checks, runs the same way `runner.default_graph` builds it, and a
compiled run is graded by the identical mechanism as a generic one.
"""

import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.project import ProjectStore, initialise
from dobby.project import architecture as A
from dobby.project import loop as L
from dobby.project import workorder as W
from dobby.project.models import ProjectManifest, WorkItem
from dobby.runtime.contracts import LOCAL_WRITE, NONE, SCHEMAS, validate_schema

PASSING_SMOKE = '{python} -c "import sys; sys.exit(0)"'


def spec(**kw) -> A.PlanSpec:
    base = dict(plan_id="pl-1", work_item_id="W001", objective="paginate it",
                side_effect_class=LOCAL_WRITE)
    base.update(kw)
    return A.PlanSpec(**base)


def steps(*rows) -> tuple:
    return tuple(rows)


def scout(objective="find the handler", **kw):
    row = {"role": "scout", "objective": objective, "read_set": ["app.py"]}
    row.update(kw)
    return row


def implement(objective="add the page param", **kw):
    row = {"role": "implement", "objective": objective,
           "write_set": ["app.py"], "read_set": ["app.py"]}
    row.update(kw)
    return row


def critic(objective="is the parameter validated", **kw):
    row = {"role": "critic", "objective": objective}
    row.update(kw)
    return row


def an_item(**kw) -> WorkItem:
    base = dict(work_item_id="W001", project_id="p", title="paginate",
                outcome="make the endpoint paginate",
                acceptance_checks=["pytest -q"])
    base.update(kw)
    return WorkItem(**base)


class CompilerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = self.tmp.name
        self.manifest = ProjectManifest(project_id="p", root=self.root,
                                        repo_digest="d",
                                        smoke_checks=("pytest -q",))
        self.item = an_item()

    def tearDown(self):
        self.tmp.cleanup()

    def orders(self, plan):
        return W.compile_orders(plan, item=self.item, manifest=self.manifest)

    def graph(self, plan, **kw):
        kw.setdefault("static", True)
        return W.compile_graph(plan, item=self.item, manifest=self.manifest,
                               **kw)

    def refuses(self, plan):
        with self.assertRaises(W.PlanNotCompilable) as caught:
            self.orders(plan)
        return str(caught.exception)


class APlanMayNotCompileIntoWhateverItAsksFor(CompilerCase):
    def test_a_second_writing_step_is_refused_and_not_dropped(self):
        """Silently dropping it would report success for a graph nobody proposed."""
        reason = self.refuses(spec(execution_steps=steps(
            implement(), implement("and the sort param",
                                   write_set=["other.py"]))))
        self.assertIn("more than one writing step", reason)
        self.assertIn("implement-1", reason)

    def test_an_unknown_role_names_the_roles_that_exist(self):
        reason = self.refuses(spec(execution_steps=steps(
            {"role": "deployer", "objective": "ship it"})))
        self.assertIn("deployer", reason)
        self.assertIn("scout", reason)

    def test_a_step_may_not_propose_its_own_verify(self):
        """A plan that could propose a verify node could propose one that passes."""
        reason = self.refuses(spec(execution_steps=steps(
            implement(), {"role": "verify", "objective": "check it"})))
        self.assertIn("verify", reason)
        self.assertIn("this harness's", reason)

    def test_implementing_without_naming_what_it_writes_is_refused(self):
        reason = self.refuses(spec(execution_steps=steps(
            implement(write_set=[]))))
        self.assertIn("write_set", reason)

    def test_a_write_path_outside_the_project_is_refused(self):
        reason = self.refuses(spec(execution_steps=steps(
            implement(write_set=["../../etc/passwd"]))))
        self.assertIn("outside the project root", reason)

    def test_a_non_writing_role_may_not_declare_a_write_set(self):
        reason = self.refuses(spec(execution_steps=steps(
            scout(write_set=["app.py"]), implement())))
        self.assertIn("only an `implement` step may write", reason)

    def test_a_critic_with_nothing_to_criticise_is_refused(self):
        reason = self.refuses(spec(execution_steps=steps(critic())))
        self.assertIn("nothing to criticise", reason)

    def test_a_plan_with_no_steps_is_refused_rather_than_compiled_empty(self):
        reason = self.refuses(spec(execution_steps=()))
        self.assertIn("no execution steps", reason)

    def test_a_plan_that_never_implements_cannot_satisfy_the_item(self):
        reason = self.refuses(spec(execution_steps=steps(scout())))
        self.assertIn("none of", reason)

    def test_a_raised_side_effect_class_is_refused(self):
        """Above LOCAL_WRITE is the runtime's approval path, not this one's."""
        reason = self.refuses(spec(side_effect_class="EXTERNAL_IRREVERSIBLE",
                                   execution_steps=steps(implement())))
        self.assertIn("EXTERNAL_IRREVERSIBLE", reason)

    def test_a_step_that_is_not_an_object_is_refused(self):
        reason = self.refuses(spec(execution_steps=steps("do the thing")))
        self.assertIn("not an object", reason)

    def test_a_step_with_no_objective_is_refused(self):
        reason = self.refuses(spec(execution_steps=steps(
            implement(objective="   "))))
        self.assertIn("no objective", reason)


class TheCompiledOrdersCarryTheirLimits(CompilerCase):
    def setUp(self):
        super().setUp()
        self.plan = spec(execution_steps=steps(scout(), implement(), critic()))

    def test_roles_profiles_and_dependencies(self):
        orders = {o.role: o for o in self.orders(self.plan)}
        self.assertEqual(orders["scout"].execution_profile, W.READ_ONLY)
        self.assertEqual(orders["implement"].execution_profile, W.SERIAL_WRITE)
        self.assertEqual(orders["critic"].execution_profile, W.READ_ONLY)
        self.assertEqual(orders["implement"].depends_on,
                         (orders["scout"].work_id,))
        self.assertEqual(orders["critic"].depends_on,
                         (orders["implement"].work_id,))

    def test_only_the_writer_carries_a_side_effect(self):
        for order in self.orders(self.plan):
            expected = LOCAL_WRITE if order.role == W.IMPLEMENT else NONE
            self.assertEqual(order.side_effect_class, expected, order.work_id)

    def test_an_order_cannot_be_edited_after_the_checks_that_allowed_it(self):
        order = self.orders(self.plan)[0]
        with self.assertRaises(Exception):
            order.side_effect_class = "EXTERNAL_IRREVERSIBLE"

    def test_the_writer_is_told_what_it_may_not_touch(self):
        graph = self.graph(self.plan)
        node = graph.nodes["implement-2"]
        self.assertIn("You may write ONLY: app.py", node.instruction)
        self.assertIn("out of scope", node.instruction)

    def test_a_reader_is_told_it_writes_nothing(self):
        graph = self.graph(self.plan)
        self.assertIn("writes NOTHING", graph.nodes["scout-1"].instruction)


class TheTailIsNotProposable(CompilerCase):
    def setUp(self):
        super().setUp()
        self.plan = spec(
            proposed_acceptance_checks=("echo definitely-passes",),
            execution_steps=steps(scout(), implement(), critic()))

    def test_the_graph_ends_in_verify_then_report(self):
        graph = self.graph(self.plan)
        self.assertEqual(graph.topological_order(),
                         ["scout-1", "implement-2", "critic-3", "verify",
                          "report"])

    def test_verify_carries_the_items_checks_and_not_the_plans(self):
        """`evaluate` already decided what the plan could contribute; this is not
        a second, unvalidated route to the definition of done."""
        graph = self.graph(self.plan)
        checks = graph.nodes["verify"].contract.acceptance_checks
        self.assertEqual(checks, ["pytest -q"])
        self.assertNotIn("echo definitely-passes", checks)

    def test_the_critic_runs_on_the_advisory_adapter(self):
        graph = self.graph(self.plan, static=False, provider="claude")
        self.assertEqual(graph.nodes["critic-3"].worker, "judge")

    def test_the_critic_does_not_judge_its_own_author(self):
        graph = self.graph(self.plan, static=False, provider="claude")
        self.assertEqual(graph.nodes["critic-3"].config["exclude"], ["claude"])

    def test_every_static_payload_satisfies_the_schema_it_claims(self):
        """Otherwise a dry run fails for a reason with nothing to do with the plan."""
        graph = self.graph(self.plan)
        for node in graph.nodes.values():
            schema = node.contract.output_schema
            payload = node.config.get("payload")
            if not schema or payload is None:
                continue
            self.assertEqual(validate_schema(payload, schema), [],
                             f"{node.node_id} emits a payload its own contract "
                             f"rejects")

    def test_a_graph_with_nobody_to_do_the_work_is_refused(self):
        with self.assertRaises(W.PlanNotCompilable):
            W.compile_graph(self.plan, item=self.item, manifest=self.manifest)


class TheFallbackIsNeverSilent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.data = os.path.join(self.tmp.name, ".dobby")
        report = initialise(self.data, self.root, smoke=(PASSING_SMOKE,),
                            item_specs=[{"outcome": "make it paginate",
                                         "acceptance_checks": []}])
        self.project_id = report["project_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return ProjectStore(self.data)

    def item(self):
        return self.store().load_project(self.project_id)["portfolio"].get(
            "W001")

    def apply_plan(self, **plan_kw):
        payload = {"objective": "paginate it",
                   "proposed_acceptance_checks": [PASSING_SMOKE],
                   "side_effect_class": LOCAL_WRITE}
        payload.update(plan_kw)
        decision = A.request_architecture(self.data, "W001",
                                          project_id=self.project_id,
                                          propose=lambda _r: payload)
        self.assertEqual(decision.outcome, A.APPLIED, decision.reason)
        return decision

    def test_an_applied_plan_with_steps_reshapes_the_run(self):
        self.apply_plan(execution_steps=[scout(), implement(), critic()])
        result = L.advance(self.data, compile_plans=True)
        step = result["iterations"][0]
        self.assertTrue(step["graph"].startswith("plan:"), step["graph"])
        self.assertEqual(step["item_state"], "DONE", result)

    def test_without_the_flag_the_plan_is_still_not_executed(self):
        """Opt-in, like --architect: a model shaping what runs is somebody's call."""
        self.apply_plan(execution_steps=[scout(), implement(), critic()])
        result = L.advance(self.data)
        self.assertEqual(result["iterations"][0]["graph"], W.GENERIC)

    def test_a_plan_that_proposed_no_steps_is_ordinary_and_not_a_refusal(self):
        """Most applied plans widen acceptance and propose nothing.

        Attaching a reason to those would put one on almost every planned item,
        leaving the plan that genuinely could not compile indistinguishable from
        the rest — the same silent-difference problem `shape` exists to prevent,
        reached from the other side.
        """
        result = L.advance(self.data, compile_plans=True, architect=True,
                           propose=lambda _r: {
                               "objective": "x",
                               "proposed_acceptance_checks": [PASSING_SMOKE]})
        self.assertEqual(result["iterations"][0]["graph"], W.GENERIC)

    def test_an_item_that_was_never_planned_runs_generic(self):
        result = L.advance(self.data, compile_plans=True, architect=True,
                           propose=lambda _r: {
                               "objective": "x",
                               "proposed_acceptance_checks": [PASSING_SMOKE]})
        self.assertEqual(result["iterations"][0]["graph"], W.GENERIC)
        self.assertIsNone(
            W.plan_for(self.store(), self.project_id,
                       WorkItem(work_item_id="W001", project_id="p",
                                title="t", outcome="o")),
            "an item carrying no planned_by must find no plan")

    def test_a_plan_that_will_not_compile_falls_back_carrying_the_refusal(self):
        """A silent fallback would look exactly like nobody having planned anything."""
        self.apply_plan(execution_steps=[scout()])       # never implements
        result = L.advance(self.data, compile_plans=True)
        shape = result["iterations"][0]["graph"]
        self.assertTrue(shape.startswith(W.GENERIC), shape)
        self.assertIn("none of", shape)

    def test_plan_for_matches_the_applied_plan_and_not_merely_the_newest(self):
        applied = self.apply_plan(
            execution_steps=[scout(), implement(), critic()])
        found = W.plan_for(self.store(), self.project_id, self.item())
        self.assertIsNotNone(found)
        self.assertEqual(found["plan_id"], applied.plan_id)

    def test_an_unplanned_item_has_no_plan_to_find(self):
        self.assertIsNone(W.plan_for(self.store(), self.project_id,
                                     self.item()))


if __name__ == "__main__":
    unittest.main()
