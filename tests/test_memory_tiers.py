import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.memory import (MAX_LEAKAGE, TIERS, CompressionGuideline,
                          HierarchicalMemory, MemoryItem, forget_gate,
                          input_gate, leakage, load_bearing, output_gate,
                          promote)
from dobby.memory.tiers import (TIER_TTL_DAYS, branch_score,
                                build_forest_payload, build_mountain_payload,
                                build_nation_payload, build_tree_payload)

DAY = 86400.0


def item(iid, tier, title, body="", children=(), verified=False, age_days=0.0):
    return MemoryItem(id=iid, tier=tier, title=title, body=body or title,
                      children=tuple(children), verified=verified,
                      created=time.time() - age_days * DAY)


class TestShape(unittest.TestCase):
    def test_six_tiers_five_hops(self):
        self.assertEqual(len(TIERS), 6)
        self.assertEqual(TIERS[0], "nation")
        self.assertEqual(TIERS[-1], "leaf")

    def test_root_tiers_never_expire(self):
        """A moving root invalidates every pointer beneath it."""
        self.assertIsNone(TIER_TTL_DAYS["nation"])
        self.assertIsNone(TIER_TTL_DAYS["mountain"])

    def test_ttl_gradient_is_monotone_downward(self):
        finite = [(t, TIER_TTL_DAYS[t]) for t in TIERS
                  if TIER_TTL_DAYS[t] is not None]
        values = [v for _, v in finite]
        self.assertEqual(values, sorted(values, reverse=True),
                         "detail must expire sooner than abstraction")


class TestPerTierMechanisms(unittest.TestCase):
    def test_nation_keeps_only_shared_vocabulary(self):
        kids = [item("a", "mountain", "router policy severity"),
                item("b", "mountain", "router agency level")]
        p = build_nation_payload(kids)
        self.assertEqual(p["mechanism"], "fixed_vocabulary")
        # "router" is in both children -> domain vocabulary.
        self.assertIn("router", p["vocabulary"])
        # "severity" is in one child only -> belongs to that child, not the root.
        self.assertNotIn("severity", p["vocabulary"])

    def test_mountain_centroid_keeps_characteristic_tokens(self):
        kids = [item(f"k{i}", "forest", "memory tier routing hops")
                for i in range(3)]
        p = build_mountain_payload(kids)
        self.assertEqual(p["mechanism"], "prototype_centroid")
        self.assertIn("memory", p["centroid"])
        self.assertEqual(p["centroid"]["memory"], 1.0)

    def test_forest_keeps_relationships_a_summary_would_destroy(self):
        kids = [item("t1", "tree", "compression leakage audit tokens"),
                item("t2", "tree", "compression leakage budget tokens"),
                item("t3", "tree", "unrelated worktree isolation git")]
        p = build_forest_payload(kids)
        self.assertEqual(p["mechanism"], "cooccurrence_adjacency")
        pairs = {(e["a"], e["b"]) for e in p["edges"]}
        self.assertIn(("t1", "t2"), pairs)
        self.assertIsNotNone(p["hub"])

    def test_tree_reports_missing_slots_instead_of_inventing_them(self):
        p = build_tree_payload("purpose: route a task\nrisk: stale index")
        self.assertEqual(p["mechanism"], "slotted_card")
        self.assertEqual(p["slots"]["purpose"], "route a task")
        self.assertIn("contract", p["missing_slots"])
        self.assertFalse(p["complete"])

    def test_branch_verified_outranks_newer_unverified(self):
        """The kit's authority rule, enforced by arithmetic not by tuning."""
        from dobby.swarm.diversity import token_set
        q = token_set("router agency level policy")
        old_verified = item("v", "branch", "router agency level policy",
                            verified=True, age_days=80)
        new_unverified = item("u", "branch", "router agency level policy",
                              verified=False, age_days=0)
        self.assertGreater(branch_score(old_verified, q),
                           branch_score(new_unverified, q))


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = HierarchicalMemory(self.tmp.name)

    def _build_full_tree(self):
        leaves = [item("l1", "leaf", "leakage audit lost identifier path"),
                  item("l2", "leaf", "worktree isolation git detach")]
        branches = [item("b1", "branch", "compression episode", children=["l1"]),
                    item("b2", "branch", "isolation episode", children=["l2"])]
        trees = [item("t1", "tree", "compression leakage",
                      body="purpose: audit compression", children=["b1"]),
                 item("t2", "tree", "worktree isolation", children=["b2"])]
        forests = [item("f1", "forest", "memory subsystem",
                        children=["t1", "t2"])]
        mountains = [item("m1", "mountain", "harness internals",
                          children=["f1"])]
        nation = [item("n1", "nation", "dobby harness domain", children=["m1"])]
        mountains[0].payload = build_mountain_payload(forests)
        nation[0].payload = build_nation_payload(mountains + forests)
        for tier, items in (("leaf", leaves), ("branch", branches),
                            ("tree", trees), ("forest", forests),
                            ("mountain", mountains), ("nation", nation)):
            self.mem.write_tier(tier, items)

    def test_descends_at_most_five_hops(self):
        self._build_full_tree()
        out = self.mem.route("compression leakage audit")
        self.assertLessEqual(out["hops"], 5)
        self.assertEqual(out["entered_at"], "nation")
        self.assertEqual([p["tier"] for p in out["path"]], list(TIERS))

    def test_path_is_returned_so_retrieval_is_explainable(self):
        self._build_full_tree()
        out = self.mem.route("worktree isolation")
        self.assertTrue(out["path"])
        for step in out["path"]:
            self.assertIn("considered", step)
            self.assertIn("kept", step)

    def test_empty_memory_says_so(self):
        out = self.mem.route("anything")
        self.assertEqual(out["items"], [])
        self.assertIn("empty", out["note"])

    def test_young_project_answers_from_deepest_populated_tier(self):
        """Before any promotion the upper tiers are empty; that is normal."""
        self.mem.write_tier("leaf", [item("l1", "leaf", "router agency level")])
        out = self.mem.route("router agency")
        self.assertEqual(out["entered_at"], "leaf")
        self.assertIn("no promotion has happened yet", out["note"])
        self.assertTrue(out["items"])

    def test_dangling_index_is_reported_not_silently_ignored(self):
        self.mem.write_tier("nation", [item("n1", "nation", "domain",
                                            children=["gone"])])
        out = self.mem.route("domain")
        self.assertIn("dangling", out.get("note", ""))

    def test_expired_items_are_not_routed_to(self):
        self.mem.write_tier(
            "leaf", [item("old", "leaf", "stale fact",
                          age_days=TIER_TTL_DAYS["leaf"] + 5)])
        out = self.mem.route("stale fact")
        self.assertEqual(out["items"], [])


class TestIntegrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = HierarchicalMemory(self.tmp.name)

    def test_tier_skip_detected(self):
        """A parent indexing a grandchild breaks uniform routing."""
        self.mem.write_tier("nation", [item("n1", "nation", "d",
                                            children=["t1"])])
        self.mem.write_tier("tree", [item("t1", "tree", "t")])
        out = self.mem.integrity()
        self.assertFalse(out["ok"])
        self.assertTrue(any(p["kind"] == "tier_skip" for p in out["problems"]))

    def test_dangling_child_detected(self):
        self.mem.write_tier("forest", [item("f1", "forest", "f",
                                            children=["nope"])])
        out = self.mem.integrity()
        self.assertTrue(any(p["kind"] == "dangling_child"
                            for p in out["problems"]))

    def test_childless_summary_detected(self):
        self.mem.write_tier("mountain", [item("m1", "mountain", "summarizes nothing")])
        out = self.mem.integrity()
        self.assertTrue(any(p["kind"] == "childless_summary"
                            for p in out["problems"]))

    def test_expire_reports_dangling_parents_it_creates(self):
        self.mem.write_tier("tree", [item("t1", "tree", "parent",
                                          children=["l1"])])
        self.mem.write_tier("leaf", [item("l1", "leaf", "child",
                                          age_days=TIER_TTL_DAYS["leaf"] + 1)])
        out = self.mem.expire()
        self.assertEqual(out["removed"]["leaf"], 1)
        self.assertTrue(out["dangling_parents"])
        self.assertIn("REPORTED, not auto-fixed", out["note"])

    def test_corrupt_line_does_not_kill_the_tier(self):
        path = self.mem.path("leaf")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"id":"ok","tier":"leaf","title":"good","body":"good"}\n')
            f.write("{not json at all\n")
        self.assertEqual(len(self.mem.load("leaf")), 1)


class TestLoadBearing(unittest.TestCase):
    def test_identifiers_paths_numbers_negations_all_detected(self):
        lb = load_bearing("Do not edit dobby/core/router.py; the cap is 4000 tokens "
                          "and MAX_LEAKAGE applies to build_tree_payload")
        self.assertIn("not", lb)
        self.assertIn("dobby/core/router.py", lb)
        self.assertIn("MAX_LEAKAGE", lb)
        self.assertIn("build_tree_payload", lb)
        self.assertTrue(any("4000" in x for x in lb))

    def test_prose_is_not_load_bearing(self):
        self.assertEqual(load_bearing("this is a nice and pleasant summary"), set())


class TestLeakage(unittest.TestCase):
    def test_dropping_a_path_is_corruption_not_compression(self):
        original = "Edit dobby/core/router.py to raise the cap to 4000 tokens"
        compressed = "Edit the file to raise the cap"
        out = leakage(original, compressed)
        self.assertGreater(out["leakage_rate"], MAX_LEAKAGE)
        self.assertIn("REJECT", out["verdict"])
        self.assertIn("dobby/core/router.py", out["lost_items"])

    def test_dropping_a_negation_is_caught(self):
        out = leakage("the walker does not expand expired nodes",
                      "the walker expands expired nodes")
        self.assertIn("not", out["lost_items"])
        self.assertGreater(out["leakage_rate"], 0)

    def test_dropping_prose_only_is_lossless(self):
        original = ("It is worth noting that, generally speaking, the module "
                    "dobby/core/router.py caps output at 4000 tokens")
        compressed = "dobby/core/router.py caps output at 4000 tokens"
        out = leakage(original, compressed)
        self.assertEqual(out["leakage_rate"], 0.0)
        self.assertIn("lossless", out["verdict"])
        self.assertGreater(out["compression_ratio"], 0.0)

    def test_no_load_bearing_content_is_stated_not_scored(self):
        out = leakage("some pleasant prose", "prose")
        self.assertIn("no load-bearing content", out["verdict"])


class TestGates(unittest.TestCase):
    def test_verified_items_exempt_from_age_expiry(self):
        old = item("v", "branch", "verified fact", verified=True, age_days=500)
        d = forget_gate(old)
        self.assertFalse(d.passed)
        self.assertIn("exempt", d.reason)

    def test_unverified_expired_is_forgotten(self):
        old = item("u", "branch", "stale", verified=False, age_days=500)
        self.assertTrue(forget_gate(old).passed)

    def test_contradiction_forces_forget(self):
        d = forget_gate(item("x", "tree", "t", verified=True), contradicted=True)
        self.assertTrue(d.passed)
        self.assertIn("contradicted", d.reason)

    def test_input_gate_rejects_near_duplicates(self):
        existing = [item("e", "tree", "compression leakage audit of tokens")]
        cand = item("c", "tree", "compression leakage audit of tokens")
        d = input_gate(cand, existing)
        self.assertFalse(d.passed)
        self.assertIn("overlap", d.reason)

    def test_verified_duplicate_supersedes_unverified_incumbent(self):
        existing = [item("e", "tree", "compression leakage audit of tokens",
                         verified=False)]
        cand = item("c", "tree", "compression leakage audit of tokens",
                    verified=True)
        d = input_gate(cand, existing)
        self.assertTrue(d.passed)
        self.assertIn("supersede", d.reason)

    def test_input_gate_admits_novel(self):
        existing = [item("e", "tree", "compression leakage audit")]
        cand = item("c", "tree", "worktree isolation for parallel writers")
        self.assertTrue(input_gate(cand, existing).passed)

    def test_input_gate_rejects_empty(self):
        self.assertFalse(input_gate(item("c", "tree", "", body=""), []).passed)

    def test_output_gate_filters_irrelevant(self):
        it = item("x", "tree", "worktree isolation git detach")
        self.assertFalse(output_gate(it, "colour palette typography").passed)
        self.assertTrue(output_gate(it, "worktree isolation").passed)


class TestPromotion(unittest.TestCase):
    def test_parent_tier_is_derived_not_supplied(self):
        kids = [item("l1", "leaf", "alpha"), item("l2", "leaf", "beta")]
        parent, audit = promote(kids, parent_id="b1", title="episode",
                                summary="alpha and beta")
        self.assertEqual(parent.tier, "branch")
        self.assertEqual(audit["parent_tier"], "branch")

    def test_mixed_tier_children_refused(self):
        with self.assertRaises(ValueError):
            promote([item("a", "leaf", "x"), item("b", "tree", "y")],
                    parent_id="p", title="t", summary="s")

    def test_empty_children_refused(self):
        with self.assertRaises(ValueError):
            promote([], parent_id="p", title="t", summary="s")

    def test_nation_cannot_be_promoted_above(self):
        with self.assertRaises(ValueError):
            promote([item("n", "nation", "x")], parent_id="p", title="t",
                    summary="s")

    def test_summary_of_unverified_detail_is_unverified(self):
        """Promotion must never manufacture confidence."""
        kids = [item("l1", "leaf", "alpha", verified=True),
                item("l2", "leaf", "beta", verified=False)]
        parent, _ = promote(kids, parent_id="b", title="t", summary="alpha beta")
        self.assertFalse(parent.verified)

    def test_lossy_promotion_is_refused_with_the_lost_tokens_named(self):
        kids = [item("l1", "leaf", "edit dobby/core/router.py cap 4000 tokens"),
                item("l2", "leaf", "do not touch MAX_LEAKAGE in gates.py")]
        _, audit = promote(kids, parent_id="b", title="some changes",
                           summary="we changed a few things")
        self.assertFalse(audit["accepted"])
        self.assertIn("REFUSED", audit["action"])
        self.assertTrue(audit["lost_items"])

    def test_faithful_promotion_accepted(self):
        kids = [item("l1", "leaf", "edit dobby/core/router.py cap 4000 tokens")]
        _, audit = promote(
            kids, parent_id="b", title="router cap",
            summary="dobby/core/router.py cap 4000 tokens")
        self.assertTrue(audit["accepted"], audit)

    def test_children_index_is_recorded(self):
        kids = [item("l1", "leaf", "a"), item("l2", "leaf", "b")]
        parent, _ = promote(kids, parent_id="b", title="t", summary="a b")
        self.assertEqual(set(parent.children), {"l1", "l2"})


class TestCompressionGuideline(unittest.TestCase):
    def test_ships_with_preservation_clauses(self):
        g = CompressionGuideline()
        rendered = g.render()
        self.assertIn("file path", rendered)
        self.assertIn("negation", rendered.lower())

    def test_learns_a_clause_from_a_real_loss(self):
        g = CompressionGuideline()
        before = len(g.clauses)
        out = g.learn_from_failure(
            full_context="run dobby/cli.py with timeout 900 s",
            compressed_context="run the cli",
            failure_note="agent used the default timeout and the call was killed")
        self.assertTrue(out["changed"])
        self.assertEqual(len(g.clauses), before + 1)
        self.assertEqual(g.version, 2)
        self.assertTrue(g.revisions)

    def test_refuses_to_learn_when_nothing_was_lost(self):
        """An unattributable failure must not add a rule."""
        g = CompressionGuideline()
        out = g.learn_from_failure(
            full_context="pleasant prose about the system",
            compressed_context="prose about the system",
            failure_note="the agent still failed")
        self.assertFalse(out["changed"])
        self.assertIn("not attributable to compression", out["reason"])

    def test_no_duplicate_clauses(self):
        g = CompressionGuideline()
        args = dict(full_context="edit dobby/cli.py now",
                    compressed_context="edit it",
                    failure_note="wrong file touched")
        g.learn_from_failure(**args)
        second = g.learn_from_failure(**args)
        self.assertFalse(second["changed"])

    def test_roundtrip_persists_history(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "guideline.json")
        g = CompressionGuideline()
        g.learn_from_failure(full_context="cap is 4000 tokens",
                             compressed_context="there is a cap",
                             failure_note="agent used the wrong cap")
        g.save(path)
        loaded = CompressionGuideline.load(path)
        self.assertEqual(loaded.version, g.version)
        self.assertEqual(loaded.clauses, g.clauses)
        self.assertTrue(loaded.revisions)

    def test_missing_file_yields_defaults(self):
        g = CompressionGuideline.load(os.path.join(tempfile.gettempdir(),
                                                   "definitely-absent-xyz.json"))
        self.assertEqual(g.version, 1)


if __name__ == "__main__":
    unittest.main()
