"""The Antigravity delegation lane, and the four defects it was built around.

Nothing here makes a network call. What is asserted is the argv, the prompt, the
two timeouts, and the refusals — every one of which corresponds to a failure that
was observed rather than imagined:

- a `--print-timeout` shorter than the process ceiling, which makes the harness
  kill a healthy call and blame interactive mode;
- a second `--mode` appended after the read-only default, staking read-only-ness
  on somebody else's flag-parser precedence;
- a delegation prompt with relative paths, answered confidently about a different
  tree;
- exit 0 with no output, which is agy's headless permission auto-deny and reads
  exactly like a harness bug.
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby import agy  # noqa: E402
from dobby.providers.base import ProviderResult  # noqa: E402
from dobby.providers.catalog import registry  # noqa: E402


class TestGoDuration(unittest.TestCase):
    """`--print-timeout 300` is not a Go duration and the flag rejects it."""

    def test_minutes_and_seconds(self):
        self.assertEqual(agy.go_duration(300), "5m0s")
        self.assertEqual(agy.go_duration(90), "1m30s")

    def test_under_a_minute_still_has_both_units(self):
        self.assertEqual(agy.go_duration(45), "0m45s")

    def test_non_positive_is_refused(self):
        for bad in (0, -1):
            with self.assertRaises(agy.AgyError):
                agy.go_duration(bad)


class TestTimeoutOrdering(unittest.TestCase):
    """The process ceiling must OUTLIVE the tool's own print timeout.

    When it does not, `run_provider` reaps a call agy would have finished and
    reports "the tool may have fallen back to interactive mode" — a diagnosis
    pointing at the wrong subsystem, which is worse than no diagnosis.
    """

    def test_process_ceiling_is_strictly_larger(self):
        env = agy.delegate("analyse the tree layout here", timeout_s=120,
                           dry_run=True)
        self.assertEqual(env["print_timeout"], "2m0s")
        self.assertGreater(env["process_timeout_s"], 120)

    def test_the_run_call_gets_the_larger_ceiling(self):
        seen = {}

        def spy(spec, prompt, **kwargs):
            seen.update(kwargs)
            return ProviderResult(provider="agy", ok=True, text="x", exit_code=0)

        with mock.patch.object(agy, "run_provider", spy):
            agy.delegate("analyse the tree layout here", timeout_s=100)
        self.assertEqual(seen["timeout_s"], 100 + agy.PROCESS_MARGIN_S)
        self.assertIn("--print-timeout", seen["extra"])
        self.assertIn("1m40s", seen["extra"])


class TestExactlyOneMode(unittest.TestCase):
    """Read-only-ness must not depend on repeated-flag precedence.

    Every other builder in the catalog appends extras last and relies on the CLI
    resolving duplicates last-wins. That is verified for claude. For agy it would
    have decided whether a scout can rewrite the working tree, on an unverified
    property of a third-party flag parser.
    """

    def _argv(self, extra):
        return registry().get("agy").build_argv("task text", None, extra)

    def test_default_is_plan(self):
        argv = self._argv(())
        self.assertEqual(argv.count("--mode"), 1)
        self.assertIn("plan", argv)

    def test_caller_mode_replaces_the_default_rather_than_stacking(self):
        argv = self._argv(("--mode", "accept-edits"))
        self.assertEqual(argv.count("--mode"), 1,
                         "two --mode flags reached the process")
        self.assertIn("accept-edits", argv)
        self.assertNotIn("plan", argv)

    def test_write_extra_produces_a_single_mode(self):
        """`write_extra` is appended verbatim by callers such as swebench."""
        spec = registry().get("agy")
        self.assertEqual(spec.write_extra, ("--mode", "accept-edits"))
        argv = spec.build_argv("t", None, spec.write_extra)
        self.assertEqual(argv.count("--mode"), 1)
        self.assertIn("accept-edits", argv)

    def test_the_write_mode_spelling_is_agys_not_claudes(self):
        """agy says `accept-edits`; claude says `acceptEdits`. A plausible guess
        here does not error, it hangs."""
        self.assertIn("accept-edits", agy.MODES)
        self.assertNotIn("acceptEdits", agy.MODES)

    def test_prompt_stays_an_argv_element(self):
        """The mode rework rebuilt the argv; the prompt must still be one element."""
        nasty = 'x"; rm -rf / #'
        self.assertIn(nasty, self._argv_for(nasty, ()))

    def _argv_for(self, prompt, extra):
        return registry().get("agy").build_argv(prompt, None, extra)


class TestExtraValidation(unittest.TestCase):
    """A rejected flag does not fail loudly — it can leave the tool waiting."""

    def test_effort_must_be_one_of_the_three(self):
        with self.assertRaises(agy.AgyError):
            agy.agy_extra(effort="extreme")
        self.assertIn("--effort", agy.agy_extra(effort="high"))

    def test_output_format_is_enumerated(self):
        with self.assertRaises(agy.AgyError):
            agy.agy_extra(output_format="yaml")

    def test_text_output_is_not_flagged_at_all(self):
        """The default needs no flag; emitting one is noise that can only rot."""
        self.assertNotIn("--output-format", agy.agy_extra())

    def test_json_schema_without_json_output_is_refused(self):
        with self.assertRaises(agy.AgyError):
            agy.agy_extra(json_schema="{}")
        self.assertIn("--json-schema",
                      agy.agy_extra(output_format="json", json_schema="{}"))

    def test_two_resumptions_at_once_are_refused(self):
        with self.assertRaises(agy.AgyError):
            agy.agy_extra(continue_conversation=True, conversation="abc")

    def test_a_nonexistent_add_dir_is_refused_before_launch(self):
        """The flag accepts it and then contributes nothing, so the answer is
        about a smaller tree and reads exactly like a complete one."""
        with self.assertRaises(agy.AgyError):
            agy.agy_extra(add_dirs=[os.path.join(REPO, "no-such-dir-xyz")])

    def test_add_dir_is_made_absolute(self):
        extra = agy.agy_extra(add_dirs=[REPO])
        self.assertIn(os.path.abspath(REPO), extra)

    def test_mode_appears_only_when_writes_are_requested(self):
        self.assertNotIn("--mode", agy.agy_extra())
        self.assertEqual(agy.agy_extra(allow_writes=True).count("--mode"), 1)

    def test_every_flag_emitted_is_one_the_help_text_carries(self):
        """A flag nobody measured is a five-minute hang waiting to happen."""
        extra = agy.agy_extra(
            allow_writes=True, effort="low", add_dirs=[REPO],
            continue_conversation=True, output_format="json",
            json_schema="{}", sandbox=True, skip_permissions=True,
            agent="reviewer")
        for token in extra:
            if token.startswith("--"):
                self.assertIn(token, agy.VERIFIED_FLAGS, token)


class TestAssess(unittest.TestCase):
    def test_a_capability_trigger_outranks_everything(self):
        v = agy.assess("search the web for the latest Flask CVEs")
        self.assertTrue(v.delegate)
        self.assertEqual(v.basis, "capability")
        self.assertEqual(v.template, "websearch")

    def test_korean_fires_the_same_trigger(self):
        """An English-only trigger table never fires in this harness."""
        v = agy.assess("Flask 3.x 최신 보안 권고를 웹 검색해서 정리해줘")
        self.assertTrue(v.delegate)
        self.assertEqual(v.basis, "capability")

    def test_trivial_work_stays_here(self):
        v = agy.assess("fix the typo in the readme heading")
        self.assertFalse(v.delegate)
        self.assertEqual(v.basis, "trivial")

    def test_volume_above_the_ceiling_delegates(self):
        v = agy.assess("convert every query in src to parameterised form",
                       estimated_tool_calls=40)
        self.assertTrue(v.delegate)
        self.assertEqual(v.basis, "volume")

    def test_volume_below_the_floor_does_not(self):
        v = agy.assess("convert every query in src to parameterised form",
                       estimated_tool_calls=2)
        self.assertFalse(v.delegate)

    def test_the_judgment_band_defaults_to_keeping_it_here(self):
        v = agy.assess("convert every query in src to parameterised form",
                       estimated_tool_calls=10)
        self.assertFalse(v.delegate)
        self.assertIn("judgment band", v.reason)

    def test_no_estimate_is_reported_as_no_decision(self):
        v = agy.assess("convert every query in src to parameterised form")
        self.assertFalse(v.delegate)
        self.assertEqual(v.basis, "unknown")

    def test_a_two_word_task_is_warned_about(self):
        self.assertTrue(agy.assess("fix it").warnings)


class TestPrompt(unittest.TestCase):
    def test_paths_are_absolute(self):
        """A relative path resolves against the DELEGATE's cwd, not this one."""
        prompt = agy.build_prompt("review this", template="review",
                                  files=["dobby/agy.py"])
        self.assertIn(os.path.abspath("dobby/agy.py"), prompt)

    def test_the_zero_context_sentence_is_always_present(self):
        self.assertIn("NO context from the caller",
                      agy.build_prompt("do the thing"))

    def test_read_only_is_stated_in_prose_as_well_as_in_flags(self):
        prompt = agy.build_prompt("review this", template="review")
        self.assertIn("Do NOT modify", prompt)

    def test_a_writing_template_refuses_to_be_silently_downgraded(self):
        with self.assertRaises(agy.AgyError):
            agy.build_prompt("refactor the queries", template="refactor")
        allowed = agy.build_prompt("refactor the queries", template="refactor",
                                   allow_writes=True)
        self.assertNotIn("Do NOT modify, create, or delete any file.", allowed)

    def test_requirements_are_numbered(self):
        prompt = agy.build_prompt("x", requirements=["alpha", "beta"])
        self.assertIn("1. alpha", prompt)
        self.assertIn("2. beta", prompt)

    def test_unknown_template_names_the_known_ones(self):
        with self.assertRaises(agy.AgyError) as caught:
            agy.build_prompt("x", template="telepathy")
        self.assertIn("research", str(caught.exception))


class TestHeadlessPermissionTrap(unittest.TestCase):
    """exit 0 + empty output is agy's auto-deny, not a harness fault.

    Measured 2026-08-04 (agy 1.1.8, win32): a read-only prompt naming one file
    returned rc=0 with 0 characters in 18.6s and this on stderr — "a tool
    required the "command" permission that headless mode cannot prompt for, so
    it was auto-denied". With permission granted the same prompt returned 334
    characters and the correct answer.
    """

    def _empty(self, error):
        return ProviderResult(provider="agy", ok=False, text="", exit_code=0,
                              error=error)

    def test_the_signature_is_recognised(self):
        self.assertTrue(agy.looks_permission_denied(
            self._empty("exit 0 but no stdout. stderr: ... was auto-denied.")))

    def test_an_empty_answer_without_the_marker_is_not_explained_away(self):
        self.assertFalse(agy.looks_permission_denied(
            self._empty("exit 0 but no stdout. stderr was empty too")))

    def test_a_successful_call_is_never_diagnosed(self):
        self.assertFalse(agy.looks_permission_denied(
            ProviderResult(provider="agy", ok=True, text="hello", exit_code=0)))

    def test_a_crash_is_never_diagnosed_as_permissions(self):
        self.assertFalse(agy.looks_permission_denied(
            ProviderResult(provider="agy", ok=False, text="", exit_code=3,
                           error="exit 3: permission denied opening /etc")))

    def test_the_warning_is_given_before_the_call_not_after(self):
        env = agy.delegate("map the module layout here", dry_run=True)
        self.assertIn("permission_note", env)
        self.assertIn("--skip-permissions", env["permission_note"])

    def test_granting_permission_replaces_the_note_with_the_risk(self):
        env = agy.delegate("map the module layout here", dry_run=True,
                           skip_permissions=True)
        self.assertNotIn("permission_note", env)
        self.assertIn("warning", env)
        self.assertIn("--dangerously-skip-permissions", env["argv_preview"])

    def test_a_failed_run_carries_the_remedy(self):
        def denied(spec, prompt, **kwargs):
            return self._empty("exit 0 but no stdout. stderr: auto-denied")

        with mock.patch.object(agy, "run_provider", denied):
            env = agy.delegate("map the module layout here")
        self.assertFalse(env["ok"])
        self.assertEqual(env["diagnosis"], "headless tool-permission auto-deny")
        self.assertIn("settings.json", env["remedy"])


class TestDelegateEnvelope(unittest.TestCase):
    def test_dry_run_spends_nothing(self):
        def explode(*a, **k):
            raise AssertionError("a dry run must not launch anything")

        with mock.patch.object(agy, "run_provider", explode):
            env = agy.delegate("map the module layout here", dry_run=True)
        self.assertTrue(env["dry_run"])

    def test_the_prompt_is_recoverable_from_the_envelope(self):
        """A provider answer whose prompt cannot be reproduced is a rumour."""
        env = agy.delegate("map the module layout here", dry_run=True)
        self.assertIn("map the module layout here", env["prompt"])
        self.assertEqual(env["prompt_chars"], len(env["prompt"]))

    def test_the_argv_preview_does_not_repeat_the_whole_prompt(self):
        env = agy.delegate("map the module layout here", dry_run=True)
        self.assertIn("<PROMPT>", env["argv_preview"])

    def test_truncation_is_flagged_rather_than_summarised_as_complete(self):
        def big(spec, prompt, **kwargs):
            return ProviderResult(provider="agy", ok=True, text="x" * 10,
                                  exit_code=0, truncated=True)

        with mock.patch.object(agy, "run_provider", big):
            env = agy.delegate("map the module layout here")
        self.assertIn("truncation_warning", env)


class TestCapabilityHonesty(unittest.TestCase):
    """Upstream's matrix is a map, not a measurement, and must stay labelled."""

    def test_declared_and_measured_are_separate_keys(self):
        caps = agy.capabilities()
        self.assertIn("measured_here", caps)
        self.assertIn("declared_upstream_not_verified_here", caps)

    def test_every_trigger_declares_its_evidence_level(self):
        for t in agy.TRIGGERS:
            self.assertIn(t.evidence, ("declared", "measured"), t.capability)

    def test_the_evidence_file_named_by_capabilities_exists(self):
        path = os.path.join(REPO, agy.capabilities()["measured_here"]
                            ["flag_surface_evidence"])
        self.assertTrue(os.path.exists(path), path)

    def test_every_trigger_routes_to_a_real_template(self):
        for t in agy.TRIGGERS:
            self.assertIn(t.template, agy.TEMPLATES, t.capability)


class TestExitZeroCarriesStderr(unittest.TestCase):
    """The branch that threw away the tool's own explanation."""

    def test_stderr_reaches_the_error_string(self):
        from dobby.providers.base import ProviderSpec
        from dobby.providers.run import run_provider

        spec = ProviderSpec(
            id="quiet", kind="cli", display="Q", binary=sys.executable,
            argv=lambda p, m, e: [
                sys.executable, "-c",
                "import sys; sys.stderr.write('auto-denied: no permission')"])
        result = run_provider(spec, "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("no stdout", result.error)
        self.assertIn("auto-denied", result.error)

    def test_an_empty_stderr_still_says_something_useful(self):
        from dobby.providers.base import ProviderSpec
        from dobby.providers.run import run_provider

        spec = ProviderSpec(
            id="silent", kind="cli", display="S", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c", "pass"])
        result = run_provider(spec, "x")
        self.assertFalse(result.ok)
        self.assertIn("output-format", result.error)


class TestCliWiring(unittest.TestCase):
    """An accepted flag that changes nothing reads as control nobody has."""

    def test_run_without_yes_spends_nothing(self):
        from dobby.cli import cmd_agy

        namespace = types.SimpleNamespace(
            repo=REPO, action="run", task="map the module layout here",
            template=None, tool_calls=None, file=None, add_dir=None, stack=None,
            require=None, model=None, effort=None, timeout=None, write=False,
            skip_permissions=False, sandbox=False, continue_conversation=False,
            conversation=None, output_format="text", json_schema=None,
            agent=None, yes=False)

        def explode(*a, **k):
            raise AssertionError("no --yes must mean no call")

        printed = []
        with mock.patch.object(agy, "run_provider", explode), \
                mock.patch("dobby.cli._out", printed.append):
            cmd_agy(namespace)
        self.assertTrue(printed[0]["dry_run"])
        self.assertIn("why_nothing_ran", printed[0])

    def test_the_verdict_travels_with_the_envelope(self):
        from dobby.cli import cmd_agy

        namespace = types.SimpleNamespace(
            repo=REPO, action="prompt", task="search the web for Flask CVEs",
            template=None, tool_calls=None, file=None, add_dir=None, stack=None,
            require=None, model=None, effort=None, timeout=None, write=False,
            skip_permissions=False, sandbox=False, continue_conversation=False,
            conversation=None, output_format="text", json_schema=None,
            agent=None, yes=False)
        printed = []
        with mock.patch("dobby.cli._out", printed.append):
            cmd_agy(namespace)
        self.assertTrue(printed[0]["verdict"]["delegate"])
        # The trigger chose the template; the caller named none.
        self.assertEqual(printed[0]["template"], "websearch")


if __name__ == "__main__":
    unittest.main()
