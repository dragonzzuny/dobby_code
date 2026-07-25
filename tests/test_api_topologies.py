import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers.api import (BASE_URLS, MAX_REQUEST_BYTES, ApiCallRecord,
                                 NetworkNotAllowed, audit_line, call_api)
from dobby.swarm.topologies import (FAN_OUT_IN, HIERARCHICAL, INDEPENDENT,
                                    MESH, PIPELINE, SUPERVISOR, build,
                                    recommend)


class TestNetworkGate(unittest.TestCase):
    """The gate must be impossible to trip accidentally."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("MOONSHOT_API_KEY", "DASHSCOPE_API_KEY")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_allow_network_is_required_and_has_no_default(self):
        with self.assertRaises(TypeError):
            call_api("kimi", "hello")          # noqa: missing kwarg on purpose

    def test_allow_network_false_raises_rather_than_degrading(self):
        with self.assertRaises(NetworkNotAllowed) as ctx:
            call_api("kimi", "hello", allow_network=False)
        self.assertIn("THREAT_MODEL", str(ctx.exception))

    def test_missing_key_raises_before_any_connection(self):
        with self.assertRaises(NetworkNotAllowed) as ctx:
            call_api("kimi", "hello", allow_network=True)
        self.assertIn("MOONSHOT_API_KEY", str(ctx.exception))
        # The message must not tempt anyone into persisting the key.
        self.assertIn("never written to disk", str(ctx.exception))

    def test_cli_provider_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            call_api("claude", "hello", allow_network=True)
        self.assertIn("not an api provider", str(ctx.exception))

    def test_oversized_request_is_not_sent(self):
        os.environ["MOONSHOT_API_KEY"] = "test-key-not-real"
        result, record = call_api("kimi", "x" * (MAX_REQUEST_BYTES + 10),
                                  allow_network=True)
        self.assertFalse(result.ok)
        self.assertIn("Nothing was sent", result.error)
        self.assertEqual(record.response_bytes, 0)
        self.assertIn("not sent", record.error)

    def test_every_api_provider_has_a_base_url(self):
        from dobby.providers import registry
        for spec in registry().all():
            if spec.kind == "api":
                self.assertIn(spec.id, BASE_URLS,
                              f"{spec.id} is declared but unreachable")


class TestAuditRecord(unittest.TestCase):
    def test_audit_line_records_what_left(self):
        rec = ApiCallRecord(provider="kimi", base_url="https://x/v1",
                            model="m", request_bytes=1234, response_bytes=99,
                            duration_s=0.5, status=200)
        line = audit_line(rec)
        self.assertIn("egress", line)
        self.assertIn("sent=1234B", line)
        self.assertIn("received=99B", line)


# ======================================================================
class TestTopologyShapes(unittest.TestCase):
    def test_independent_has_zero_connectivity(self):
        plan = build(INDEPENDENT, 4)
        self.assertEqual(plan.connectivity, 0.0)
        self.assertTrue(all(not s.sees_dependencies for s in plan.stages))
        self.assertEqual(plan.single_points_of_failure, [])

    def test_mesh_is_fully_connected_and_says_so(self):
        plan = build(MESH, 4)
        self.assertEqual(plan.connectivity, 1.0)
        self.assertIn("COLLAPSE RISK", plan.diversity_note)

    def test_connectivity_alone_does_not_separate_pipeline_from_fan_out(self):
        """The reason framing_depth exists: equal edges, opposite diversity."""
        n = 6
        self.assertEqual(build(PIPELINE, n).connectivity,
                         build(FAN_OUT_IN, n).connectivity)

    def test_framing_depth_separates_them(self):
        n = 6
        pipe = build(PIPELINE, n)
        fan = build(FAN_OUT_IN, n)
        self.assertEqual(pipe.framing_depth(), n - 1)
        self.assertEqual(fan.framing_depth(), 1)
        self.assertEqual(pipe.independent_agents(), 1)
        self.assertEqual(fan.independent_agents(), n - 1)

    def test_independent_is_depth_zero_all_the_way(self):
        plan = build(INDEPENDENT, 5)
        self.assertEqual(plan.framing_depth(), 0)
        self.assertEqual(plan.independent_agents(), 5)

    def test_hierarchical_is_deeper_than_supervisor(self):
        self.assertGreater(build(HIERARCHICAL, 8).framing_depth(),
                           build(SUPERVISOR, 8).framing_depth())

    def test_mesh_depth_grows_with_the_team(self):
        self.assertEqual(build(MESH, 6).framing_depth(), 5)
        self.assertEqual(build(MESH, 6).independent_agents(), 1)

    def test_pipeline_is_a_chain(self):
        plan = build(PIPELINE, 4)
        self.assertEqual([s.depends_on for s in plan.stages],
                         [(), (0,), (1,), (2,)])
        self.assertEqual(len(plan.waves()), 4, "a chain has no parallelism")

    def test_fan_out_in_isolates_workers_then_merges_once(self):
        plan = build(FAN_OUT_IN, 5)
        workers = [s for s in plan.stages if s.label.startswith("worker")]
        self.assertEqual(len(workers), 4)
        self.assertTrue(all(not w.sees_dependencies for w in workers))
        self.assertEqual(len(plan.waves()), 2, "isolated workers run in one wave")

    def test_supervisor_warns_that_its_framing_reaches_everyone(self):
        plan = build(SUPERVISOR, 4)
        self.assertIn("LOW diversity", plan.diversity_note)
        self.assertEqual(len(plan.waves()), 3)

    def test_hierarchical_warns_about_two_framing_hops(self):
        plan = build(HIERARCHICAL, 8)
        self.assertIn("two framing hops", plan.diversity_note)
        self.assertGreaterEqual(len(plan.single_points_of_failure), 2)

    def test_hierarchical_refuses_when_supervisor_is_simpler(self):
        with self.assertRaises(ValueError) as ctx:
            build(HIERARCHICAL, 3)
        self.assertIn("supervisor", str(ctx.exception))

    def test_fan_out_in_needs_a_merger(self):
        with self.assertRaises(ValueError):
            build(FAN_OUT_IN, 1)

    def test_unknown_topology_lists_the_known_ones(self):
        with self.assertRaises(ValueError) as ctx:
            build("telepathy", 3)
        self.assertIn("pipeline", str(ctx.exception))

    def test_zero_agents_rejected(self):
        with self.assertRaises(ValueError):
            build(INDEPENDENT, 0)


class TestWaveScheduling(unittest.TestCase):
    def test_waves_respect_dependencies(self):
        plan = build(SUPERVISOR, 5)
        waves = plan.waves()
        done = set()
        for wave in waves:
            for idx in wave:
                stage = plan.stages[idx]
                self.assertTrue(set(stage.depends_on) <= done,
                                f"stage {idx} ran before its dependencies")
            done |= set(wave)

    def test_cost_separates_wall_clock_from_token_cost(self):
        cost = build(FAN_OUT_IN, 7).cost()
        self.assertEqual(cost["agents"], 7)
        self.assertEqual(cost["waves"], 2)
        self.assertEqual(cost["max_parallel"], 6)

    def test_cycle_raises_rather_than_looping(self):
        plan = build(PIPELINE, 3)
        # Force a cycle: stage 0 depends on stage 2.
        plan.stages[0] = type(plan.stages[0])(
            index=0, role="x", label="x", depends_on=(2,))
        with self.assertRaises(ValueError) as ctx:
            plan.waves()
        self.assertIn("cyclic", str(ctx.exception))

    def test_every_topology_produces_a_schedulable_plan(self):
        for topo, n in ((INDEPENDENT, 4), (PIPELINE, 4), (FAN_OUT_IN, 4),
                        (SUPERVISOR, 4), (HIERARCHICAL, 8), (MESH, 4)):
            plan = build(topo, n)
            waves = plan.waves()
            self.assertEqual(sum(len(w) for w in waves), len(plan.stages),
                             f"{topo} lost stages during scheduling")


class TestRecommendation(unittest.TestCase):
    def test_exploration_gets_isolation(self):
        out = recommend(agents=5, needs_exploration=True,
                        needs_reallocation=False, stages_are_distinct=False)
        self.assertEqual(out["topology"], FAN_OUT_IN)

    def test_distinct_stages_get_a_pipeline(self):
        out = recommend(agents=4, needs_exploration=False,
                        needs_reallocation=False, stages_are_distinct=True)
        self.assertEqual(out["topology"], PIPELINE)

    def test_reallocation_gets_a_supervisor_with_the_framing_warning(self):
        out = recommend(agents=4, needs_exploration=False,
                        needs_reallocation=True, stages_are_distinct=False)
        self.assertEqual(out["topology"], SUPERVISOR)
        self.assertIn("one framing", out["warning"])

    def test_large_reallocation_goes_hierarchical_with_a_warning(self):
        out = recommend(agents=9, needs_exploration=False,
                        needs_reallocation=True, stages_are_distinct=False)
        self.assertEqual(out["topology"], HIERARCHICAL)
        self.assertIn("not use this shape for exploration", out["warning"])

    def test_single_agent_has_no_topology(self):
        out = recommend(agents=1, needs_exploration=True,
                        needs_reallocation=False, stages_are_distinct=False)
        self.assertIn("one call", out["reason"])

    def test_mesh_is_never_recommended(self):
        """It is selectable, never suggested."""
        for explore in (True, False):
            for realloc in (True, False):
                for distinct in (True, False):
                    out = recommend(agents=6, needs_exploration=explore,
                                    needs_reallocation=realloc,
                                    stages_are_distinct=distinct)
                    self.assertNotEqual(out["topology"], MESH)


if __name__ == "__main__":
    unittest.main()
