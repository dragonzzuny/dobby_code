"""The policy reaching the scheduler, which is the only part that ever mattered.

`providers/policy.py` declared codex the default implementer and agy an isolated
delegate, and `runtime/placement.py` did not read any of it: `candidates()`
returned every installed provider and let the scorecard sort them out. So the
role tables were a sentence in a file, and traffic went wherever the utility
formula pointed — which, with no measurements, was the catalog's own order.

These tests are about the refusals, because that is where a policy either exists
or does not:

- agy must be unreachable outside isolation. Not deprioritised. Unreachable.
  `providers/catalog.py` records it creating files in all four mode/permission
  combinations, so the directory it was launched in is the only containment
  anyone has demonstrated.
- a quota must not be a tie-break. A capped provider still reachable by falling
  back is not capped, and the difference arrives as a bill.
- an override must reproduce a run, not leave the policy. One that could bypass
  isolation would make isolation advisory.

No provider process is launched anywhere in this file.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers import policy as P
from dobby.runtime import graph as G
from dobby.runtime.contracts import LOCAL_WRITE, ArtifactContract
from dobby.runtime.placement import ProviderPlacement


class FakeStore:
    """A store with no runs, so nothing is measured and preference decides."""

    span_write_failures: list = []

    def list_runs(self, **_kw):
        return []

    def attempts(self, *_a, **_kw):
        return []

    def spans(self, *_a, **_kw):
        return []


def node(kind="implement", *, role=None, provider=None, exclude=()):
    config = {}
    if role:
        config["provider_role"] = role
    if provider:
        config["provider"] = provider
    if exclude:
        config["exclude"] = list(exclude)
    return G.TaskNode(node_id="n", kind=kind, worker="provider",
                      instruction="do it",
                      contract=ArtifactContract(side_effect_class=LOCAL_WRITE),
                      config=config)


class PlacementCase(unittest.TestCase):
    def setUp(self):
        self.placement = ProviderPlacement(FakeStore(), scorecard={})

    def place(self, n, *, installed=("codex", "agy", "claude"), isolated=False,
              calls=None, override=None, avoid=None, prefs=None):
        import dobby.providers.detect as detect

        original = detect.available_ids
        detect.available_ids = lambda **_kw: list(installed)
        try:
            ctx = P.PlacementContext(
                isolated=isolated, original_root="/proj",
                preferences=prefs or P.ProviderPreferences(),
                provider_calls=dict(calls or {}))
            return self.placement.choose(n, context=ctx, override=override,
                                         avoid=set(avoid or ()))
        finally:
            detect.available_ids = original


class AFocusedPatchGoesToCodex(PlacementCase):
    def test_codex_is_selected_and_claude_is_not_called(self):
        got = self.place(node("implement"))
        self.assertEqual(got.provider, "codex")
        self.assertEqual(got.provider_role, "implement")
        self.assertNotEqual(got.provider, "claude")

    def test_agy_is_rejected_with_the_rule_that_stopped_it(self):
        """An operator asking why agy never runs needs the rule, not an absence."""
        got = self.place(node("implement"))
        self.assertIn("agy", got.rejected)
        self.assertIn("measured writing files", got.rejected["agy"])

    def test_the_basis_says_it_was_preference_not_a_measurement(self):
        got = self.place(node("implement"))
        self.assertEqual(got.selection_basis,
                         "subscription_first_static_preference")
        self.assertTrue(got.provisional)

    def test_claude_stays_eligible_but_behind(self):
        """Capped, not banned: it is the escalation, and it is still reachable."""
        got = self.place(node("implement"))
        self.assertIn("claude", got.eligible)
        self.assertEqual(got.claude_cap_remaining, 2)


class ABroadTaskGoesToAgyInsideIsolation(PlacementCase):
    def test_agy_is_selected_for_the_delegate_role_when_isolated(self):
        got = self.place(node("scout", role=P.ISOLATED_DELEGATE), isolated=True)
        self.assertEqual(got.provider, "agy")
        self.assertTrue(got.isolated)

    def test_the_same_role_without_isolation_selects_nobody(self):
        got = self.place(node("scout", role=P.ISOLATED_DELEGATE), isolated=False)
        self.assertIsNone(got.provider)
        self.assertEqual(got.selection_basis, "no_eligible_provider")
        self.assertIn("isolated workspace", got.rejected["agy"])

    def test_agy_becomes_an_implement_candidate_only_once_isolated(self):
        """The fallback the operator wants: use the subscription, off the tree."""
        without = self.place(node("implement"), isolated=False)
        self.assertIn("agy", without.rejected)
        with_iso = self.place(node("implement"), isolated=True,
                              avoid=["codex"])
        self.assertEqual(with_iso.provider, "agy")


class AgyMisuseLaunchesNothing(PlacementCase):
    def test_a_critic_without_isolation_does_not_reach_agy(self):
        got = self.place(node("critic", role=P.CRITIC), isolated=False,
                         installed=("agy",))
        self.assertIsNone(got.provider)
        self.assertNotIn("agy", got.eligible)

    def test_the_refusal_is_recorded_rather_than_the_name_dropped(self):
        got = self.place(node("critic", role=P.CRITIC), isolated=False,
                         installed=("agy",))
        self.assertIn("agy", got.rejected)


class AFailedProviderFallsBackWithinThePolicy(PlacementCase):
    def test_codex_failing_moves_to_claude_on_the_original_tree(self):
        got = self.place(node("implement"), avoid=["codex"], isolated=False)
        self.assertEqual(got.provider, "claude",
                         "agy must not inherit an original-root task")

    def test_codex_failing_prefers_agy_once_a_worktree_exists(self):
        got = self.place(node("implement"), avoid=["codex"], isolated=True)
        self.assertEqual(got.provider, "agy")

    def test_the_avoid_reason_is_recorded(self):
        got = self.place(node("implement"), avoid=["codex"])
        self.assertIn("avoid list", got.rejected["codex"])


class TheClaudeQuotaIsNotATieBreak(PlacementCase):
    def test_an_exhausted_cap_removes_claude_entirely(self):
        got = self.place(node("implement"), installed=("claude",),
                         calls={"claude": 2})
        self.assertIsNone(got.provider)
        self.assertIn("cap is the operator's budget", got.rejected["claude"])

    def test_there_is_no_silent_fallback_past_the_cap(self):
        """A capped provider still reachable by falling back is not capped."""
        got = self.place(node("implement"), installed=("claude",),
                         calls={"claude": 5})
        self.assertEqual(got.selection_basis, "no_eligible_provider")

    def test_the_remaining_allowance_is_reported_on_every_placement(self):
        got = self.place(node("implement"), calls={"claude": 1})
        self.assertEqual(got.claude_cap_remaining, 1)

    def test_a_cap_of_zero_means_zero(self):
        prefs = P.ProviderPreferences(
            caps={"claude": P.ProviderCap(max_calls=0)})
        got = self.place(node("implement"), installed=("claude",), prefs=prefs)
        self.assertIsNone(got.provider)


class AnOverrideReproducesARunAndDoesNotLeaveThePolicy(PlacementCase):
    def test_an_allowed_override_is_taken_and_says_so(self):
        got = self.place(node("implement"), override="claude")
        self.assertEqual(got.provider, "claude")
        self.assertEqual(got.selection_basis, "explicit_override")

    def test_an_override_that_would_bypass_isolation_is_refused(self):
        got = self.place(node("implement"), override="agy", isolated=False)
        self.assertIsNone(got.provider)
        self.assertEqual(got.selection_basis, "override_refused")
        self.assertIn("measured writing files", got.reason)

    def test_an_override_past_an_exhausted_cap_is_refused(self):
        got = self.place(node("implement"), override="claude",
                         calls={"claude": 2})
        self.assertIsNone(got.provider)
        self.assertIn("cap", got.reason)

    def test_the_same_override_inside_isolation_is_allowed(self):
        got = self.place(node("implement"), override="agy", isolated=True)
        self.assertEqual(got.provider, "agy")


class UnmeasuredEconomicsNeverWinOnPrice(PlacementCase):
    def test_a_thin_record_selects_by_preference_and_says_so(self):
        got = self.place(node("implement"))
        self.assertEqual(got.selection_basis,
                         "subscription_first_static_preference")

    def test_economics_report_unmeasured_for_codex_and_agy(self):
        for pid in ("codex", "agy"):
            row = P.economics({}, pid, "implement")
            self.assertEqual(row["economics_status"], "unmeasured", pid)
            self.assertIsNone(row["usd_per_verified"])


class TheRoleComesFromTheNodeThenTheKind(unittest.TestCase):
    def test_a_declared_role_wins(self):
        n = node("execute", role=P.CRITIC)
        self.assertEqual(n.config["provider_role"], P.CRITIC)

    def test_an_undeclared_kind_falls_back_to_the_table(self):
        self.assertEqual(P.node_role_for("plan"), P.ARCHITECT)
        self.assertEqual(P.node_role_for("scout"), P.SCOUT)

    def test_a_scout_is_placeable_without_a_worktree(self):
        """This assertion used to say ISOLATED_DELEGATE, and it was wrong.

        Measured 2026-08-23: every compiled S2 plan began with a scout step,
        `isolated_delegate` requires isolation, the benchmark arm ran without a
        worktree, and so `scout-1` FAILED with no provider placed and the whole
        run ended `item_blocked` having done nothing. Reading the tree is not
        the isolated delegate; it is its own read-only role.
        """
        from dobby.providers.detect import Availability

        available = {i: Availability(id=i, state="available", detail="",
                                     path="x", cost_tier="standard", kind="cli",
                                     verified_here=True)
                     for i in ("codex", "claude")}
        self.assertTrue(P.candidates_for(P.SCOUT, availability=available))
        self.assertFalse(P.candidates_for(P.ISOLATED_DELEGATE,
                                          availability=available))

    def test_a_scout_still_may_not_write(self):
        self.assertFalse(P.ROLE_POLICY[P.SCOUT].writes)

    def test_an_unknown_kind_lands_on_implement_rather_than_anywhere(self):
        self.assertEqual(P.node_role_for("something-new"), P.IMPLEMENT)

    def test_the_fast_path_declares_implement(self):
        from dobby.project.execution_policy import TaskProfile
        from dobby.project.fastpath import direct_gated_graph
        from dobby.project.models import WorkItem

        item = WorkItem(work_item_id="W001", project_id="p", title="t",
                        outcome="fix it", acceptance_checks=["pytest -q"])
        graph = direct_gated_graph(
            item, TaskProfile(acceptance_declared=True,
                              side_effect_class="LOCAL_WRITE"),
            provider="codex")
        self.assertEqual(graph.nodes["execute"].config["provider_role"],
                         "implement")


if __name__ == "__main__":
    unittest.main()
