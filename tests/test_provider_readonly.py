"""A read-only role, enforced twice, because one enforcement is a claim.

The defect this covers was not a missing check. It was a check that read as one:
`propose_via_provider` said "READ-ONLY by construction" because it never passed
`write_extra`, while the catalog three files away recorded a probe showing `agy`
creating files under exactly that argv, four configurations out of four. Two
parts of the repository disagreed and the prose won.

So the tests come in two groups matching the two enforcements, and the second
group is the one that matters. Routing can only exclude what somebody has already
measured; every remaining architect provider is RO_CLAIMED, meaning documented
and unprobed — which is precisely what agy was.
"""

import json
import os
import sys
import tempfile
import unittest
import unittest.mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers.base import (RO_CLAIMED, RO_DENIED, RO_UNKNOWN,
                                  RO_VERIFIED, ProviderError, ProviderSpec)
from dobby.providers.catalog import (LOCAL_ONLY_ROLES, READ_ONLY_ROLES,
                                     ROLE_ROUTING, registry, role_preference)
from dobby.providers.detect import Availability, resolve_role
from dobby.project import architecture as A
from dobby.project.readonly import ReadOnlyViolation, fingerprint, run_read_only


def available(*ids):
    return {i: Availability(id=i, state="available", detail="", path="x",
                            cost_tier="standard", kind="cli",
                            verified_here=True) for i in ids}


class Result:
    """The shape `run_provider` returns, reduced to what these paths read."""

    def __init__(self, ok=True, text="{}", error=""):
        self.ok, self.text, self.error = ok, text, error


class TheCatalogRecordsWhatWasMeasured(unittest.TestCase):
    def test_agy_is_recorded_as_measured_to_write(self):
        """The probe is in providers/catalog.py; this is the field that acts on it."""
        self.assertEqual(registry().get("agy").read_only_default, RO_DENIED)

    def test_a_measured_writer_may_not_fill_a_read_only_role(self):
        self.assertFalse(registry().get("agy").may_fill_a_read_only_role)

    def test_unlooked_at_is_refused_rather_than_assumed_safe(self):
        """"nobody checked" and "it is safe" are the two things that must not merge."""
        qwen = registry().get("qwen")
        self.assertEqual(qwen.read_only_default, RO_UNKNOWN)
        self.assertFalse(qwen.may_fill_a_read_only_role)

    def test_a_provider_with_no_file_capability_is_read_only_structurally(self):
        spec = ProviderSpec(id="t", kind="api", display="t", binary=None,
                            argv=None, capabilities=("long_context",),
                            read_only_default=RO_UNKNOWN)
        self.assertTrue(spec.may_fill_a_read_only_role,
                        "a provider with no file mechanism was excluded on the "
                        "strength of a flag it does not have")

    def test_the_documented_but_unprobed_providers_are_claimed_not_verified(self):
        """Marking them VERIFIED would be the same error pointing the other way."""
        for pid in ("claude", "codex", "gemini"):
            self.assertEqual(registry().get(pid).read_only_default, RO_CLAIMED,
                             pid)

    def test_an_unknown_state_is_refused_at_construction(self):
        with self.assertRaises(ProviderError):
            ProviderSpec(id="t", kind="api", display="t", binary=None,
                         argv=None, read_only_default="probably")


class TheRoutingRefusesTheProviderThatWrites(unittest.TestCase):
    def test_the_architect_role_is_read_only(self):
        self.assertIn("architect", READ_ONLY_ROLES)

    def test_agy_never_fills_the_architect_role_even_when_it_is_all_there_is(self):
        self.assertIsNone(resolve_role("architect", availability=available("agy")))

    def test_the_exclusion_does_not_leak_into_roles_that_may_write(self):
        """agy is a capable scout; cwd and worktree isolation contain it there."""
        self.assertEqual(resolve_role("scout", availability=available("agy")),
                         "agy")

    def test_a_permitted_provider_is_still_reached_past_an_excluded_one(self):
        self.assertEqual(
            resolve_role("architect", availability=available("agy", "gemini")),
            "gemini")

    def test_agy_stays_in_the_preference_table(self):
        """The table states what would be best; READ_ONLY_ROLES states what is allowed."""
        self.assertIn("agy", ROLE_ROUTING["architect"])

    def test_no_read_only_role_is_left_with_nobody_who_can_fill_it(self):
        reg = registry()
        for role in READ_ONLY_ROLES:
            fillable = [p for p in role_preference(role)
                        if reg.get(p).may_fill_a_read_only_role]
            self.assertTrue(fillable,
                            f"{role} has no permitted provider at all; the "
                            f"safety rule removed the feature")


class TheTreeIsTheFactAndItIsChecked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = self.tmp.name
        self.spec = registry().get("claude")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_provider_that_touches_nothing_returns_normally(self):
        def runner(spec, prompt, *, cwd=None, timeout_s=None):
            return Result(text='{"objective": "x"}')

        result = run_read_only(self.spec, "plan it", root=self.root,
                               runner=runner)
        self.assertTrue(result.ok)

    def test_a_provider_that_writes_a_file_has_its_result_discarded(self):
        """The whole point: this is what a CLAIMED read-only mode failing looks like."""
        def runner(spec, prompt, *, cwd=None, timeout_s=None):
            with open(os.path.join(cwd, "hello.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("DOBBY_WRITE_OK")
            return Result(text='{"objective": "a perfectly good plan"}')

        with self.assertRaises(ReadOnlyViolation) as caught:
            run_read_only(self.spec, "plan it", root=self.root, runner=runner,
                          role="architect")
        self.assertIn("architect", str(caught.exception))
        self.assertIn("claude", str(caught.exception))

    def test_a_provider_that_edits_an_existing_file_is_caught_too(self):
        target = os.path.join(self.root, "app.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("print('a')\n")

        def runner(spec, prompt, *, cwd=None, timeout_s=None):
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("print('a')\nprint('b')\n")
            return Result()

        with self.assertRaises(ReadOnlyViolation):
            run_read_only(self.spec, "plan it", root=self.root, runner=runner)

    def test_the_fingerprint_moves_when_the_tree_does(self):
        before = fingerprint(self.root)
        with open(os.path.join(self.root, "new.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        self.assertNotEqual(before, fingerprint(self.root))


class TheArchitectSurfacesAViolationAsARejectedPlan(unittest.TestCase):
    def test_a_mutation_becomes_PlanRejected_rather_than_a_crash(self):
        """The request is already recorded; a crash would lose the loop's stop path."""
        request = A.ArchitectureRequest(
            project_id="p", work_item_id="W001",
            trigger=A.MISSING_ACCEPTANCE, manifest_digest="d",
            baseline_git_sha="s")
        item = unittest.mock.Mock(acceptance_checks=[], title="t", outcome="o",
                                  uncertainty=1, evidence_refs=[])
        manifest = unittest.mock.Mock(root=REPO, smoke_checks=())

        def boom(*args, **kwargs):
            raise ReadOnlyViolation("the tree changed while it ran")

        with unittest.mock.patch("dobby.project.readonly.run_read_only", boom):
            with self.assertRaises(A.PlanRejected) as caught:
                A.propose_via_provider(request, item=item, manifest=manifest,
                                       provider="claude")
        self.assertIn("the tree changed", str(caught.exception))

    def test_the_function_still_never_passes_write_extra(self):
        """The original guarantee is weaker than it read, but it is not abandoned."""
        import inspect
        body = inspect.getsource(A.propose_via_provider)
        code = body.split('"""')[2] if body.count('"""') >= 2 else body
        self.assertNotIn("write_extra", code)
        self.assertNotIn("extra=", code)


if __name__ == "__main__":
    unittest.main()
