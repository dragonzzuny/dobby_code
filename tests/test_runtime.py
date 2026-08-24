"""Tests for the durable execution kernel.

The interesting ones do not assert that a happy run succeeds. They kill a real
process in the middle of a run and assert what the next process does about it,
because "resume" is a claim about a crash and a claim about a crash that has
never been tested against one is a claim about nothing.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import (ArtifactContract, EXTERNAL_IRREVERSIBLE,
                           EXTERNAL_REVERSIBLE, Failure, GraphError, RunBudget,
                           RunStore, Runner, SCHEMAS, StaticWorker, TaskGraph,
                           TaskNode, Verifier, WorkerRegistry,
                           classify_provider_error, default_graph,
                           idempotency_key, promotable, validate_schema)
from dobby.runtime import graph as G
from dobby.runtime.failures import (CAPACITY, CONTRACT_VIOLATION,
                                    DEFAULT_POLICY, NON_RETRYABLE,
                                    POLICY_BLOCKED, QUALITY_FAILURE, REPAIR,
                                    RETRY_ELSEWHERE, RETRY_SAME,
                                    TRANSIENT_PROVIDER, WAIT, backoff_delay,
                                    classify_command_failure)
from dobby.runtime.store import AttemptAlreadyRecorded


#: A fixture declares SOMETHING, because a contract that declares nothing is
#: now refused at the gate — `all([])` is True, so a node with no schema, no
#: check, no effect and nothing to ground used to promote whatever it was
#: handed. `{"type": "object"}` is the weakest real claim: the output is an
#: object. Tests that care about a stronger shape still pass their own.
FIXTURE_SCHEMA = {"type": "object"}


def static_node(node_id, payload, *, depends_on=(), schema=None, checks=(),
                side_effect="NONE", **config):
    return TaskNode(
        node_id=node_id, kind=node_id, depends_on=list(depends_on),
        worker="static",
        contract=ArtifactContract(output_schema=schema or FIXTURE_SCHEMA,
                                  acceptance_checks=list(checks),
                                  side_effect_class=side_effect),
        config={"payload": payload, **config})


class GraphValidation(unittest.TestCase):
    def test_a_cycle_is_refused_at_construction(self):
        with self.assertRaises(GraphError) as caught:
            TaskGraph([static_node("a", {}, depends_on=["b"]),
                       static_node("b", {}, depends_on=["a"])])
        self.assertIn("cycle", str(caught.exception))

    def test_a_dependency_that_is_not_in_the_graph_is_refused(self):
        with self.assertRaises(GraphError) as caught:
            TaskGraph([static_node("a", {}, depends_on=["ghost"])])
        self.assertIn("ghost", str(caught.exception))

    def test_duplicate_node_ids_are_refused(self):
        with self.assertRaises(GraphError):
            TaskGraph([static_node("a", {}), static_node("a", {})])

    def test_ready_nodes_respect_dependencies(self):
        graph = TaskGraph([static_node("a", {}),
                           static_node("b", {}, depends_on=["a"])])
        self.assertEqual([n.node_id for n in graph.ready_nodes()], ["a"])
        graph.nodes["a"].state = G.NODE_SUCCEEDED
        self.assertEqual([n.node_id for n in graph.ready_nodes()], ["b"])

    def test_a_failed_dependency_makes_dependents_unreachable_not_pending(self):
        graph = TaskGraph([static_node("a", {}),
                           static_node("b", {}, depends_on=["a"])])
        graph.nodes["a"].state = G.NODE_FAILED
        self.assertEqual(graph.ready_nodes(), [])
        self.assertEqual(graph.unreachable(), ["b"])
        self.assertIn("a (FAILED)", graph.blocking_reason("b"))

    def test_terminal_states_are_terminal(self):
        with self.assertRaises(GraphError):
            G.check_node_transition(G.NODE_SUCCEEDED, G.READY)
        with self.assertRaises(GraphError):
            G.check_run_transition(G.SUCCEEDED, G.RUNNING)


class SchemaValidation(unittest.TestCase):
    def test_every_violation_is_reported_not_just_the_first(self):
        problems = validate_schema(
            {"steps": "no"}, {"type": "object", "required": ["steps", "owner"],
                              "properties": {"steps": {"type": "array"}}})
        self.assertEqual(len(problems), 2, problems)

    def test_a_boolean_is_not_a_number(self):
        self.assertTrue(validate_schema(True, {"type": "integer"}))

    def test_plan_schema_rejects_an_empty_step_list(self):
        self.assertTrue(validate_schema({"steps": []}, SCHEMAS["plan"]))
        self.assertFalse(validate_schema({"steps": [{"what": "do the thing"}]},
                                         SCHEMAS["plan"]))


class FailureClassification(unittest.TestCase):
    def test_rate_limits_move_the_work_and_timeouts_do_not(self):
        self.assertEqual(classify_provider_error("HTTP 429 rate limit").failure_class,
                         CAPACITY)
        self.assertEqual(
            classify_provider_error("timeout after 120s").failure_class,
            TRANSIENT_PROVIDER)

    def test_authentication_is_never_retried(self):
        failure = classify_provider_error("invalid api key")
        self.assertEqual(failure.failure_class, NON_RETRYABLE)
        self.assertEqual(failure.rule().action, "FAIL")

    def test_a_permission_refusal_waits_for_a_human(self):
        failure = classify_provider_error(
            "a tool required the command permission; auto-denied")
        self.assertEqual(failure.failure_class, POLICY_BLOCKED)
        self.assertEqual(failure.rule().action, WAIT)

    def test_exit_zero_with_no_output_is_a_broken_contract_not_a_success(self):
        failure = classify_provider_error("", exit_code=0, empty_output=True)
        self.assertEqual(failure.failure_class, CONTRACT_VIOLATION)

    def test_an_unrecognised_failure_is_permanent_so_the_budget_survives(self):
        self.assertEqual(
            classify_provider_error("something nobody has seen").failure_class,
            NON_RETRYABLE)

    def test_a_command_the_shell_cannot_run_is_a_definition_error(self):
        self.assertEqual(
            classify_command_failure(127, "not found").failure_class,
            NON_RETRYABLE)
        self.assertEqual(
            classify_command_failure(1, "2 tests failed").failure_class,
            QUALITY_FAILURE)

    def test_the_class_picks_the_action_not_the_count(self):
        self.assertEqual(DEFAULT_POLICY[TRANSIENT_PROVIDER].action, RETRY_SAME)
        self.assertEqual(DEFAULT_POLICY[CAPACITY].action, RETRY_ELSEWHERE)
        self.assertEqual(DEFAULT_POLICY[CONTRACT_VIOLATION].action, REPAIR)
        self.assertTrue(DEFAULT_POLICY[CONTRACT_VIOLATION].avoid_last_provider)

    def test_backoff_is_deterministic_when_jitter_is_not_supplied(self):
        rule = DEFAULT_POLICY[TRANSIENT_PROVIDER]
        self.assertEqual(backoff_delay(rule, 1), 2.0)
        self.assertEqual(backoff_delay(rule, 3), 8.0)
        self.assertEqual(backoff_delay(DEFAULT_POLICY[CONTRACT_VIOLATION], 2),
                         0.0)

    def test_an_unknown_class_is_refused_rather_than_stored(self):
        with self.assertRaises(ValueError):
            Failure("VIBES", "no")


class StoreInvariants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(os.path.join(self.tmp.name, ".dobby"))
        self.graph = TaskGraph([static_node("a", {}),
                                static_node("b", {}, depends_on=["a"])])
        self.run_id = self.store.create_run("t", self.graph)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_attempt_number_can_be_recorded_exactly_once(self):
        self.store.start_attempt(self.run_id, "a", 1)
        with self.assertRaises(AttemptAlreadyRecorded):
            self.store.start_attempt(self.run_id, "a", 1)

    def test_finishing_an_attempt_twice_is_refused(self):
        self.store.start_attempt(self.run_id, "a", 1)
        self.store.finish_attempt(self.run_id, "a", 1, outcome=G.FINISHED)
        with self.assertRaises(Exception):
            self.store.finish_attempt(self.run_id, "a", 1, outcome=G.FINISHED)

    def test_only_one_holder_can_lease_a_node(self):
        self.store.set_node_state(self.run_id, "a", G.READY)
        self.assertTrue(self.store.lease_node(self.run_id, "a", holder="one"))
        self.assertFalse(self.store.lease_node(self.run_id, "a", holder="two"))

    def test_an_external_effect_can_be_claimed_once(self):
        key = idempotency_key(self.run_id, "a")
        self.assertTrue(self.store.claim_effect(key, self.run_id, "a", "1"))
        self.assertFalse(self.store.claim_effect(key, self.run_id, "a", "1"))

    def test_a_reworded_retry_collides_and_a_new_version_does_not(self):
        first = idempotency_key(self.run_id, "send", "1")
        self.assertEqual(first, idempotency_key(self.run_id, "send", "1"))
        self.assertNotEqual(first, idempotency_key(self.run_id, "send", "2"))

    def test_an_interrupted_attempt_is_visible_as_an_open_one(self):
        self.store.start_attempt(self.run_id, "a", 1)
        self.assertEqual([r["node_id"] for r in
                          self.store.open_attempts(self.run_id)], ["a"])

    def test_the_projection_agrees_with_the_event_log(self):
        self.store.set_node_state(self.run_id, "a", G.READY)
        report = self.store.rebuild(self.run_id)
        self.assertTrue(report["consistent"], report["mismatches"])

    def test_a_run_that_does_not_exist_is_named_in_the_error(self):
        with self.assertRaises(Exception) as caught:
            self.store.load_run("nope")
        self.assertIn("nope", str(caught.exception))


class VerifierGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_shape_is_checked_before_any_command_runs(self):
        contract = ArtifactContract(
            output_schema={"type": "object", "required": ["x"]},
            acceptance_checks=["exit 1"])
        verdict = Verifier(self.tmp.name).verify(contract, {})
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.records, [])       # the command never ran
        self.assertEqual(verdict.failure.failure_class, CONTRACT_VIOLATION)

    def test_a_failing_check_carries_the_repair_hint(self):
        contract = ArtifactContract(
            acceptance_checks=[f'{sys.executable} -c "print(\'boom\'); exit(1)"'])
        verdict = Verifier(self.tmp.name).verify(contract, {})
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.failure.failure_class, QUALITY_FAILURE)
        self.assertIn("boom", verdict.repair_hint)

    def test_a_check_that_cannot_run_here_blocks_promotion(self):
        contract = ArtifactContract(
            acceptance_checks=["definitely-not-a-real-binary-xyz --version"])
        verdict = Verifier(self.tmp.name).verify(contract, {})
        self.assertFalse(promotable(contract, verdict))

    def test_passing_checks_promote(self):
        contract = ArtifactContract(
            acceptance_checks=[f'{sys.executable} -c "pass"'])
        verdict = Verifier(self.tmp.name).verify(contract, {})
        self.assertTrue(verdict.passed, verdict.to_dict())
        self.assertTrue(promotable(contract, verdict))


class RunnerBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, **kwargs):
        return Runner(self.tmp.name, data_dir=self.data, sleep=lambda _s: None,
                      **kwargs)

    def test_a_four_node_run_completes_and_promotes_every_artifact(self):
        runner = self.runner()
        run_id = runner.start("demo", default_graph("demo", static=True))
        result = runner.run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertEqual([s.node_id for s in result.steps],
                         ["plan", "execute", "verify", "report"])
        self.assertTrue(all(s.artifact_id for s in result.steps))

    def test_an_unverified_artifact_never_reaches_the_next_node(self):
        """A rejected artifact is not an input, and the dependent is skipped."""
        graph = TaskGraph([
            static_node("produce", {"wrong": "shape"},
                        schema={"type": "object", "required": ["right"]}),
            static_node("consume", {"ok": True}, depends_on=["produce"]),
        ])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id)
        states = {s.node_id: s.state for s in result.steps}
        self.assertEqual(states["produce"], G.NODE_FAILED)
        self.assertEqual(states["consume"], G.SKIPPED)
        self.assertEqual(result.state, G.FAILED)
        promoted = runner.store.artifacts(run_id, state="PROMOTED")
        self.assertEqual(promoted, [])

    def test_a_promoted_artifact_is_handed_to_the_dependent(self):
        graph = TaskGraph([
            static_node("produce", {"value": 41}),
            static_node("consume", {"ok": True}, depends_on=["produce"]),
        ])
        runner = self.runner()
        run_id = runner.start("t", graph)
        runner.run(run_id)
        with open(os.path.join(self.data, "state", "runtime", run_id,
                               "artifacts", "produce-1.json"),
                  encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["payload"], {"value": 41})
        self.assertEqual(payload["state"], "PROMOTED")

        with open(os.path.join(self.data, "state", "runtime", run_id,
                               "artifacts", "consume-1.json"),
                  encoding="utf-8") as handle:
            consumer = json.load(handle)
        # The consumer records WHAT it was given, on disk, so the attempt can be
        # reproduced rather than re-imagined.
        self.assertEqual(consumer["evidence"]["input_refs"], ["produce-1"])

    def test_a_retryable_failure_is_retried_and_a_permanent_one_is_not(self):
        graph = TaskGraph([
            static_node("flaky", {"ok": True}, fail_with=TRANSIENT_PROVIDER,
                        fail_times=1),
            static_node("doomed", {"ok": True}, fail_with=NON_RETRYABLE),
        ])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id)
        by_node = {s.node_id: s for s in result.steps}
        self.assertEqual(by_node["flaky"].state, G.NODE_SUCCEEDED)
        self.assertEqual(by_node["flaky"].attempts, 2)
        self.assertEqual(by_node["doomed"].state, G.NODE_FAILED)
        self.assertEqual(by_node["doomed"].attempts, 1)

    def test_a_contract_violation_repairs_with_the_failure_in_hand(self):
        graph = TaskGraph([
            static_node("shape", {"ok": True}, fail_with=CONTRACT_VIOLATION,
                        fail_times=1),
        ])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id)
        self.assertEqual(result.steps[0].state, G.NODE_SUCCEEDED)
        rows = runner.store.attempts(run_id, "shape")
        self.assertEqual(rows[0]["failure_class"], CONTRACT_VIOLATION)
        self.assertIn("REPAIR", rows[0]["detail"])

    def test_an_irreversible_node_does_not_run_without_an_approval(self):
        graph = TaskGraph([static_node("deploy", {"ok": True},
                                       side_effect=EXTERNAL_IRREVERSIBLE)])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id)
        self.assertEqual(result.state, G.WAITING)
        self.assertEqual(result.steps[0].attempts, 0)
        self.assertTrue(any("not\napproved" in d["reason"].replace(" ", "\n")
                            or "approved" in d["reason"]
                            for d in result.deferred), result.deferred)

    def test_an_approved_irreversible_node_still_needs_budget_for_it(self):
        graph = TaskGraph([static_node("deploy", {"ok": True},
                                       side_effect=EXTERNAL_IRREVERSIBLE)])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id, approvals={"deploy"})
        self.assertEqual(result.state, G.WAITING)     # max_irreversible == 0

        run_id2 = runner.start("t", TaskGraph([
            static_node("deploy", {"ok": True},
                        side_effect=EXTERNAL_IRREVERSIBLE)]))
        budget = RunBudget(max_irreversible=1)
        result2 = runner.run(run_id2, budget=budget, approvals={"deploy"})
        self.assertEqual(result2.state, G.SUCCEEDED, result2.to_dict())

    def test_an_external_effect_is_performed_once_across_two_runs(self):
        graph = TaskGraph([static_node("send", {"ok": True},
                                       side_effect=EXTERNAL_REVERSIBLE)])
        runner = self.runner()
        run_id = runner.start("t", graph)
        runner.run(run_id)
        effects = runner.store.effects(run_id)
        self.assertEqual(len(effects), 1)
        self.assertTrue(effects[0]["result_digest"])

    def test_a_spent_budget_defers_instead_of_killing_a_node(self):
        graph = TaskGraph([static_node("a", {}), static_node("b", {})])
        runner = self.runner()
        run_id = runner.start("t", graph)
        result = runner.run(run_id, budget=RunBudget(max_attempts=1))
        self.assertEqual(result.state, G.WAITING)
        self.assertEqual(sum(s.attempts for s in result.steps), 1)
        self.assertIn("budget", result.deferred[0]["reason"])

    def test_a_step_limit_leaves_a_run_that_says_it_is_unfinished(self):
        """RUNNING inside a process that already returned is a lie."""
        runner = self.runner()
        run_id = runner.start("demo", default_graph("demo", static=True))
        result = runner.run(run_id, max_steps=1)
        self.assertEqual(result.state, G.WAITING)
        self.assertEqual(sum(s.attempts for s in result.steps), 1)
        finished = self.runner().run(run_id)
        self.assertEqual(finished.state, G.SUCCEEDED)
        self.assertEqual(sum(s.attempts for s in finished.steps), 4)

    def test_a_finished_run_is_not_restarted_by_a_second_call(self):
        runner = self.runner()
        run_id = runner.start("demo", default_graph("demo", static=True))
        runner.run(run_id)
        again = runner.run(run_id)
        self.assertEqual(again.state, G.SUCCEEDED)
        self.assertEqual(sum(s.attempts for s in again.steps), 4)

    def test_an_interrupted_attempt_is_recovered_rather_than_left_open(self):
        graph = TaskGraph([static_node("a", {"ok": True})])
        runner = self.runner()
        run_id = runner.start("t", graph)
        # Exactly what a killed process leaves behind: a started attempt with
        # no finish, and a node that never left LEASED.
        runner.store.set_node_state(run_id, "a", G.READY)
        runner.store.lease_node(run_id, "a", holder="ghost")
        runner.store.start_attempt(run_id, "a", 1, worker="static")

        result = self.runner().run(run_id)
        self.assertEqual(result.state, G.SUCCEEDED, result.to_dict())
        self.assertTrue(any("interrupted" in n for n in result.notes),
                        result.notes)
        rows = runner.store.attempts(run_id, "a")
        self.assertEqual(rows[0]["outcome"], G.RETRYABLE_FAILURE)
        self.assertEqual(rows[1]["outcome"], G.FINISHED)

    def test_a_claimed_but_unconfirmed_effect_is_reported_not_repeated(self):
        """Not repeated, and — the half this test used to get wrong — not
        reported as a success either.

        The claim is written before the effect, so an unconfirmed claim means
        the effect MAY have happened. The node blocks. An earlier version of
        this test asserted NODE_SUCCEEDED, which made the runtime's answer to
        "did the mail go out?" a confident yes it had no basis for.
        """
        graph = TaskGraph([static_node("send", {"ok": True},
                                       side_effect=EXTERNAL_REVERSIBLE)])
        runner = self.runner()
        run_id = runner.start("t", graph)
        key = idempotency_key(run_id, "send")
        runner.store.claim_effect(key, run_id, "send", "1")   # crashed here

        result = self.runner().run(run_id)
        self.assertTrue(any("never confirmed" in n for n in result.notes),
                        result.notes)
        self.assertEqual(result.steps[0].state, G.BLOCKED_ON_APPROVAL,
                         result.to_dict())
        self.assertEqual(result.state, G.WAITING)
        self.assertEqual(len(runner.store.effects(run_id)), 1)


# --------------------------------------------------------------------------
# The real thing: a process that is actually killed.
# --------------------------------------------------------------------------

_CRASH_SCRIPT = r'''
import os, sys
sys.path.insert(0, {repo!r})
from dobby.runtime import Runner, TaskGraph, TaskNode, ArtifactContract

def node(node_id, depends_on=()):
    return TaskNode(node_id=node_id, kind=node_id, depends_on=list(depends_on),
                    worker="command",
                    contract=ArtifactContract(output_schema=dict(type="object")),
                    config={{"command": {command!r}}})

graph = TaskGraph([node("first"), node("second", ["first"]),
                   node("third", ["second"])])
runner = Runner({repo_dir!r}, data_dir={data!r})
run_id = {start}
with open({idfile!r}, "w") as handle:
    handle.write(run_id)
runner.run(run_id, max_steps={steps})
{tail}
'''


def _kill_tree(proc) -> None:
    """Kill `proc` AND the child it is waiting on.

    `Popen.kill` kills one process. The runner's worker is a subprocess of that
    process, so killing only the parent leaves a grandchild running with the
    temp directory as its working directory — which on Windows makes the
    directory undeletable and turned the teardown of this test into its only
    failure. Killing the tree is also the more faithful simulation: a machine
    losing the harness loses the tool it launched too.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    proc.kill()


class AKilledProcess(unittest.TestCase):
    """Resume is a claim about a crash. These make a real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.marker = os.path.join(self.tmp.name, "marker.txt")
        self.idfile = os.path.join(self.tmp.name, "run_id.txt")
        # Each node appends one line. The line COUNT is the measurement: work
        # that ran twice is visible as two lines, and no amount of state
        # bookkeeping can hide it.
        self.command = (
            f'{sys.executable} -c "open(r\'{self.marker}\', \'a\','
            f' encoding=\'utf-8\').write(\'ran\\n\')"')

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, *, steps, start, tail):
        return _CRASH_SCRIPT.format(
            repo=REPO, repo_dir=self.tmp.name, data=self.data,
            command=self.command, idfile=self.idfile, steps=steps,
            start=start, tail=tail)

    def _run(self, script):
        return subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)

    def _marker_lines(self):
        if not os.path.exists(self.marker):
            return 0
        with open(self.marker, encoding="utf-8") as handle:
            return len([l for l in handle if l.strip()])

    def test_work_that_finished_before_the_kill_is_not_repeated(self):
        first = self._run(self._script(
            steps=2, start='runner.start("crash demo", graph)',
            tail="os._exit(1)"))          # killed before the third node
        self.assertEqual(first.returncode, 1, first.stderr[-800:])
        self.assertEqual(self._marker_lines(), 2, "two nodes should have run")

        with open(self.idfile, encoding="utf-8") as handle:
            run_id = handle.read().strip()

        second = self._run(self._script(
            steps=10, start=repr(run_id), tail=""))
        self.assertEqual(second.returncode, 0, second.stderr[-800:])
        # Three nodes in the graph, three lines total: the resumed process ran
        # the third and neither of the first two.
        self.assertEqual(self._marker_lines(), 3)

        store = RunStore(self.data)
        self.assertEqual(store.load_run(run_id)["state"], G.SUCCEEDED)
        for node_id in ("first", "second", "third"):
            self.assertEqual(len(store.attempts(run_id, node_id)), 1,
                             f"{node_id} ran more than once")

    def test_a_process_killed_mid_node_leaves_a_recoverable_run(self):
        # The node's command blocks until the test releases it, rather than
        # sleeping. That makes the kill deterministic: killing the whole tree
        # let taskkill reach the CHILD first often enough to be flaky — the
        # runner then saw its worker exit, recorded the attempt as failed, and
        # died with nothing open, so the test's premise was gone. Killing only
        # the parent, while the child is still blocked, guarantees the attempt
        # is open at the moment the process dies.
        release = os.path.join(self.tmp.name, "release.txt")
        slow = (f'{sys.executable} -c "import os, time;'
                f' open(r\'{self.marker}\', \'a\', encoding=\'utf-8\')'
                f'.write(\'ran\\n\');'
                f' [time.sleep(0.05) for _ in range(1200)'
                f'  if not os.path.exists(r\'{release}\')]"')
        script = _CRASH_SCRIPT.format(
            repo=REPO, repo_dir=self.tmp.name, data=self.data, command=slow,
            idfile=self.idfile, steps=10,
            start='runner.start("kill demo", graph)', tail="")
        popen_extra = ({} if os.name == "nt" else {"start_new_session": True})
        with subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **popen_extra) as proc:
            try:
                deadline = time.monotonic() + 120
                while self._marker_lines() < 1 and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertGreaterEqual(self._marker_lines(), 1,
                                        "the first node never started")
            finally:
                # The PARENT only, and while its worker is still blocked.
                proc.kill()
                proc.wait(timeout=30)
                # Now let the orphaned worker finish, so it stops holding the
                # temp directory as its working directory on Windows.
                with open(release, "w", encoding="utf-8") as handle:
                    handle.write("go")
                _kill_tree(proc)

        with open(self.idfile, encoding="utf-8") as handle:
            run_id = handle.read().strip()
        store = RunStore(self.data)
        self.assertTrue(store.open_attempts(run_id),
                       "a killed process should leave an attempt open")

        runner = Runner(self.tmp.name, data_dir=self.data)
        result = runner.run(run_id, max_steps=0)
        self.assertTrue(any("interrupted" in n for n in result.notes),
                        result.notes)
        self.assertEqual(store.open_attempts(run_id), [])
        self.assertEqual(
            store.load_run(run_id)["graph"].nodes["first"].state, G.READY,
            "the node whose lease was lost must be runnable again")


class WorkerContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_is_found_inside_a_fenced_answer(self):
        from dobby.runtime.workers import extract_json
        self.assertEqual(
            extract_json('Sure!\n```json\n{"a": 1}\n```\nHope that helps.'),
            {"a": 1})

    def test_json_is_found_after_a_preamble_without_a_fence(self):
        from dobby.runtime.workers import extract_json
        self.assertEqual(extract_json('Here you go: {"a": {"b": 2}} done'),
                         {"a": {"b": 2}})

    def test_prose_yields_nothing_rather_than_a_guess(self):
        from dobby.runtime.workers import extract_json
        self.assertIsNone(extract_json("I could not complete this task."))

    def test_a_command_worker_reports_a_non_zero_exit_as_a_quality_failure(self):
        node = TaskNode(node_id="t", kind="t", worker="command",
                        config={"command": f'{sys.executable} -c "exit(3)"'})
        result = WorkerRegistry().get("command").run(
            node, {"repo": self.tmp.name})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, QUALITY_FAILURE)

    def test_a_command_that_promised_json_and_printed_prose_violates_contract(self):
        node = TaskNode(node_id="t", kind="t", worker="command",
                        config={"command": f'{sys.executable} -c "print(\'hi\')"',
                                "parse": "json"})
        result = WorkerRegistry().get("command").run(
            node, {"repo": self.tmp.name})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, CONTRACT_VIOLATION)

    def test_retry_elsewhere_actually_moves_the_call(self):
        """The avoid list has to change the provider, not just be recorded."""
        from dobby.runtime.workers import ProviderWorker
        node = TaskNode(node_id="t", kind="t", worker="provider",
                        config={"provider": "claude",
                                "avoid_providers": ["claude"],
                                "fallback_providers": ["gemini", "codex"]})
        chosen = ProviderWorker._alternative(
            "claude", {"claude"}, node)
        if chosen is None:
            self.skipTest("this machine has no second usable provider")
        self.assertNotEqual(chosen, "claude")

    def test_with_nothing_left_to_move_to_the_failure_says_so(self):
        from dobby.runtime.workers import ProviderWorker
        from dobby.providers.detect import available_ids
        node = TaskNode(node_id="t", kind="t", worker="provider",
                        config={"provider": "claude"})
        every = set(available_ids(allow_network=False)) | {"claude"}
        self.assertIsNone(ProviderWorker._alternative("claude", every, node))

    def test_an_unknown_worker_is_named_in_the_error(self):
        with self.assertRaises(KeyError) as caught:
            WorkerRegistry().get("telepathy")
        self.assertIn("telepathy", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
