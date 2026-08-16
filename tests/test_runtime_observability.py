"""Tests for the trace model and the metrics read off it.

The load-bearing ones here are about *absence*. A metrics table that reports 0%
for a system nobody has run, or a span tree that looks plausible and has the
wrong parent, is worse than no table and no tree: both are believed.
"""

import os
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.runtime import (RunStore, Runner, TaskGraph, TaskNode,
                           ArtifactContract, Tracer, default_graph,
                           metrics_report, percentile, render_timeline,
                           scorecard, to_otlp)
from dobby.runtime import graph as G
from dobby.runtime.trace import (AGENT_GENERATION, ERROR, NODE, OK, RUN,
                                 Span, TraceError, now_ms)


def static_node(node_id, payload, *, depends_on=(), **config):
    return TaskNode(node_id=node_id, kind=node_id, depends_on=list(depends_on),
                    worker="static", contract=ArtifactContract(),
                    config={"payload": payload, **config})


class TheClock(unittest.TestCase):
    """The defect that produced a child ordered before its own parent."""

    def test_two_timestamps_taken_back_to_back_are_distinguishable(self):
        samples = [now_ms() for _ in range(50)]
        self.assertEqual(samples, sorted(samples), "the clock went backwards")
        self.assertGreater(len(set(samples)), 1,
                           "every timestamp is identical — this is the coarse "
                           "time.time() resolution that mis-ordered spans")

    def test_it_is_still_wall_clock(self):
        import time
        self.assertLess(abs(now_ms() / 1000.0 - time.time()), 5.0,
                        "the anchored clock has drifted away from wall time")


class SpanContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(os.path.join(self.tmp.name, ".dobby"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(TraceError):
            Span(span_id="a", trace_id="t", kind="vibes", name="n")

    def test_a_span_missing_its_required_attributes_is_recorded_as_violating(self):
        tracer = Tracer(self.store, "run1")
        with tracer.span(AGENT_GENERATION, "gen"):    # no `provider`
            pass
        spans = self.store.spans("run1")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["status"], ERROR)
        self.assertIn("trace_violation", spans[0]["attributes"])

    def test_the_observation_is_kept_even_when_it_violates(self):
        """Losing the record to enforce a rule about records is its own defect."""
        tracer = Tracer(self.store, "run1")
        with tracer.span(AGENT_GENERATION, "gen"):
            pass
        self.assertEqual(len(self.store.spans("run1")), 1)

    def test_children_hang_under_their_parent(self):
        tracer = Tracer(self.store, "run1")
        with tracer.span(RUN, "root", task="t") as root:
            with tracer.span(NODE, "child", node_kind="k", worker="static"):
                pass
        spans = {s["name"]: s for s in self.store.spans("run1")}
        self.assertIsNone(spans["root"]["parent_span_id"])
        self.assertEqual(spans["child"]["parent_span_id"], root.span_id)

    def test_a_raising_block_is_recorded_as_an_error_and_re_raised(self):
        tracer = Tracer(self.store, "run1")
        with self.assertRaises(ValueError):
            with tracer.span(RUN, "root", task="t"):
                raise ValueError("boom")
        span = self.store.spans("run1")[0]
        self.assertEqual(span["status"], ERROR)
        self.assertIn("boom", span["attributes"]["error"])

    def test_two_threads_do_not_become_each_others_parents(self):
        tracer = Tracer(self.store, "run1")
        with tracer.span(RUN, "root", task="t") as root:
            child = tracer.child_of(root.span_id)

            def work(name):
                with child.span(NODE, name, node_kind="k", worker="static"):
                    # Long enough that the two overlap; a shared stack would
                    # make one of these the parent of the other.
                    for _ in range(2000):
                        pass

            threads = [threading.Thread(target=work, args=(f"n{i}",))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        spans = {s["name"]: s for s in self.store.spans("run1")}
        for i in range(4):
            self.assertEqual(spans[f"n{i}"]["parent_span_id"], root.span_id,
                             "a node span was parented to a sibling")

    def test_the_root_is_rendered_first(self):
        tracer = Tracer(self.store, "run1")
        with tracer.span(RUN, "root", task="t"):
            tracer.event(NODE, "point", node_kind="k", worker="static")
        lines = render_timeline(self.store.spans("run1"))
        self.assertTrue(lines[0].startswith("root"), lines)

    def test_otlp_carries_the_ids_that_make_it_joinable(self):
        tracer = Tracer(self.store, "run1")
        with tracer.span(RUN, "root", task="t"):
            pass
        payload = to_otlp(self.store.spans("run1"))
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(span["traceId"], "run1")
        keys = {a["key"] for a in span["attributes"]}
        self.assertLessEqual({"dobby.kind", "dobby.run_id", "policy_version"},
                             keys)

    def test_an_empty_timeline_says_so_rather_than_rendering_nothing(self):
        self.assertEqual(render_timeline([]), ["(no spans recorded)"])


class MetricsOnAnEmptyStore(unittest.TestCase):
    """Nothing recorded must read as nothing recorded, never as zero."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(os.path.join(self.tmp.name, ".dobby"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_metric_is_none_with_a_reason(self):
        report = metrics_report(self.store)
        for name in ("task_success_at_verifier", "retry_amplification",
                     "recovery_success_rate", "side_effect_duplicate_rate"):
            with self.subTest(metric=name):
                self.assertIsNone(report[name]["value"])
                self.assertTrue(report[name]["note"],
                                "a None with no reason is indistinguishable "
                                "from a bug")

    def test_the_unmeasured_list_names_them(self):
        report = metrics_report(self.store)
        self.assertIn("task_success_at_verifier", report["unmeasured"])

    def test_cost_is_reported_as_unmeasurable_rather_than_omitted(self):
        report = metrics_report(self.store)
        self.assertIsNone(report["cost_per_verified_task"]["value"])
        self.assertIn("cannot see money",
                      report["cost_per_verified_task"]["note"])

    def test_the_scorecard_is_empty_not_absent(self):
        self.assertEqual(scorecard(self.store), {})


class MetricsOnRealRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, ".dobby")
        self.runner = Runner(self.tmp.name, data_dir=self.data,
                             sleep=lambda _s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_clean_run_scores_one(self):
        run_id = self.runner.start("write the report", default_graph("write the report", static=True))
        self.runner.run(run_id)
        report = metrics_report(self.runner.store)
        self.assertEqual(report["task_success_at_verifier"]["value"], 1.0)
        self.assertEqual(report["task_success_at_verifier"]["n"], 1)

    def test_a_failed_run_moves_it(self):
        self.runner.run(self.runner.start(
            "write the summary", default_graph("write the summary",
                                               static=True)))
        bad = TaskGraph([TaskNode(node_id="a", kind="a", worker="static",
                                  contract=ArtifactContract(),
                                  config={"fail_with": "NON_RETRYABLE"})])
        self.runner.run(self.runner.start("run the failing graph", bad))
        report = metrics_report(self.runner.store)
        self.assertEqual(report["task_success_at_verifier"]["value"], 0.5)

    def test_retry_amplification_counts_the_retries(self):
        graph = TaskGraph([TaskNode(
            node_id="flaky", kind="flaky", worker="static",
            contract=ArtifactContract(),
            config={"payload": {}, "fail_with": "TRANSIENT_PROVIDER",
                    "fail_times": 1})])
        self.runner.run(self.runner.start("run the graph", graph))
        report = metrics_report(self.runner.store)
        self.assertEqual(report["retry_amplification"]["value"], 2.0)

    def test_recovery_rate_measures_only_interrupted_runs(self):
        graph = TaskGraph([static_node("a", {"ok": True})])
        run_id = self.runner.start("run the graph", graph)
        self.runner.store.set_node_state(run_id, "a", G.READY)
        self.runner.store.lease_node(run_id, "a", holder="ghost")
        self.runner.store.start_attempt(run_id, "a", 1, worker="static")
        self.runner.run(run_id)
        report = metrics_report(self.runner.store)
        self.assertEqual(report["recovery_success_rate"]["value"], 1.0)
        self.assertEqual(report["recovery_success_rate"]["n"], 1)

    def test_latency_has_a_p50_once_a_run_has_spans(self):
        self.runner.run(self.runner.start("write the report", default_graph("write the report", static=True)))
        report = metrics_report(self.runner.store)
        self.assertIsNotNone(report["completion_latency"]["p50"]["value"])

    def test_spans_are_written_for_every_node(self):
        run_id = self.runner.start("write the report", default_graph("write the report", static=True))
        self.runner.run(run_id)
        kinds = [s["kind"] for s in self.runner.store.spans(run_id)]
        self.assertIn("run", kinds)
        self.assertEqual(kinds.count("node"), 4)
        self.assertEqual(kinds.count("verifier"), 4)
        self.assertEqual(self.runner.store.span_write_failures, [])


class Percentiles(unittest.TestCase):
    def test_an_empty_sample_has_no_percentile(self):
        self.assertIsNone(percentile([], 95))

    def test_nearest_rank_picks_a_real_observation(self):
        sample = [1.0, 2.0, 3.0, 4.0]
        self.assertIn(percentile(sample, 95), sample)
        self.assertEqual(percentile(sample, 50), 2.0)


if __name__ == "__main__":
    unittest.main()
