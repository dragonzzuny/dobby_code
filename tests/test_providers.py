import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers import (ABSENT, AVAILABLE, BLOCKED, AgentTask,
                             ProviderError, ProviderRegistry, ProviderSpec,
                             registry, report, resolve_panel, resolve_role,
                             run_provider, survey)
from dobby.providers.catalog import LOCAL_ONLY_ROLES, ROLE_ROUTING
from dobby.providers.detect import check
from dobby.providers.fanout import _needs_isolation, run_round


class TestSpecValidation(unittest.TestCase):
    def test_rejects_unknown_kind(self):
        with self.assertRaises(ProviderError):
            ProviderSpec(id="x", kind="magic", display="X", binary="x",
                         argv=lambda p, m, e: ["x"])

    def test_rejects_unknown_cost_tier(self):
        with self.assertRaises(ProviderError):
            ProviderSpec(id="x", kind="cli", display="X", binary="x",
                         argv=lambda p, m, e: ["x"], cost_tier="free")

    def test_rejects_unknown_capability(self):
        with self.assertRaises(ProviderError):
            ProviderSpec(id="x", kind="cli", display="X", binary="x",
                         argv=lambda p, m, e: ["x"], capabilities=("telepathy",))

    def test_cli_requires_binary_and_argv(self):
        with self.assertRaises(ProviderError):
            ProviderSpec(id="x", kind="cli", display="X", binary=None, argv=None)

    def test_duplicate_ids_rejected(self):
        spec = ProviderSpec(id="dup", kind="cli", display="D", binary="d",
                            argv=lambda p, m, e: ["d"])
        with self.assertRaises(ProviderError):
            ProviderRegistry([spec, spec])


class TestCatalogInvocations(unittest.TestCase):
    """The non-interactive flag is the critical fact: a wrong one HANGS."""

    def test_every_cli_has_a_non_interactive_flag(self):
        expected = {
            "claude": "-p",
            "codex": "exec",
            "gemini": "-p",
            "agy": "--print",
            "qwen": "-p",
        }
        reg = registry()
        for pid, flag in expected.items():
            argv = reg.get(pid).build_argv("hello", None, ())
            self.assertIn(flag, argv, f"{pid} lost its non-interactive flag")

    def test_prompt_is_an_argv_element_not_shell_text(self):
        # Prompt text can come from an untrusted source; it must never be able
        # to become shell syntax.
        nasty = 'x"; rm -rf / #'
        for pid in ("claude", "codex", "gemini", "agy"):
            argv = registry().get(pid).build_argv(nasty, None, ())
            self.assertIn(nasty, argv)

    def test_read_only_default_for_file_capable_clis(self):
        """A scout must not silently edit the tree."""
        plan_markers = {"claude": "plan", "gemini": "plan", "agy": "plan"}
        for pid, marker in plan_markers.items():
            argv = registry().get(pid).build_argv("t", None, ())
            self.assertIn(marker, argv, f"{pid} lost its read-only default")

    def test_extra_appended_last_so_caller_can_override(self):
        argv = registry().get("claude").build_argv(
            "t", None, ("--permission-mode", "acceptEdits"))
        self.assertEqual(argv[-2:], ["--permission-mode", "acceptEdits"])

    def test_ollama_supplies_a_model_because_grammar_requires_one(self):
        argv = registry().get("ollama").build_argv("t", None, ())
        self.assertEqual(argv[:2], ["ollama", "run"])
        self.assertTrue(argv[2], "ollama run needs a model positional")

    def test_model_flag_threaded(self):
        argv = registry().get("codex").build_argv("t", "gpt-5", ())
        self.assertIn("--model", argv)
        self.assertIn("gpt-5", argv)

    def test_api_providers_have_no_argv(self):
        for pid in ("kimi", "dashscope"):
            spec = registry().get(pid)
            self.assertEqual(spec.kind, "api")
            with self.assertRaises(ProviderError):
                spec.build_argv("t")


class TestDetection(unittest.TestCase):
    def test_api_blocked_without_network_flag(self):
        a = check(registry().get("kimi"), allow_network=False)
        self.assertEqual(a.state, BLOCKED)
        self.assertFalse(a.usable)
        # The distinction that makes doctor useful: blocked, not absent.
        self.assertIn("allow_network", a.detail)

    def test_api_blocked_when_key_missing_even_with_network(self):
        os.environ.pop("MOONSHOT_API_KEY", None)
        a = check(registry().get("kimi"), allow_network=True)
        self.assertEqual(a.state, BLOCKED)
        self.assertIn("MOONSHOT_API_KEY", a.detail)

    def test_absent_binary_reported_as_absent(self):
        spec = ProviderSpec(id="ghost", kind="cli", display="G",
                            binary="definitely-not-installed-xyz",
                            argv=lambda p, m, e: ["x"])
        self.assertEqual(check(spec).state, ABSENT)

    def test_survey_covers_whole_catalog(self):
        self.assertEqual(set(survey()), set(registry().ids()))

    def test_report_states_multi_agent_readiness_honestly(self):
        r = report()
        self.assertEqual(r["multi_agent_ready"], r["usable_count"] >= 2)
        self.assertEqual(r["max_panel_size"], r["usable_count"])


class TestRoleResolution(unittest.TestCase):
    def test_panel_never_repeats_a_provider(self):
        """Repeating a provider would fake independent opinions."""
        panel = resolve_panel("draft", 12)
        self.assertEqual(len(panel), len(set(panel)))

    def test_panel_capped_by_availability_not_request(self):
        usable = report()["usable_count"]
        self.assertLessEqual(len(resolve_panel("draft", 99)), usable)

    def test_zero_size_returns_empty(self):
        self.assertEqual(resolve_panel("draft", 0), [])

    def test_exclude_prevents_self_review(self):
        author = resolve_role("draft")
        if author is None:
            self.skipTest("no provider available on this machine")
        critic = resolve_role("critic", exclude={author})
        self.assertNotEqual(critic, author)

    def test_returns_none_rather_than_falling_back_to_excluded(self):
        every = set(registry().ids())
        self.assertIsNone(resolve_role("critic", exclude=every))

    def test_local_only_roles_never_use_api_providers(self):
        """The aggregated context is the crown jewel; it must not leave."""
        os.environ["MOONSHOT_API_KEY"] = "test-key-not-real"
        try:
            for role in LOCAL_ONLY_ROLES:
                picked = resolve_panel(role, 8, allow_network=True)
                for pid in picked:
                    self.assertEqual(registry().get(pid).kind, "cli",
                                     f"{role} must not route to an api provider")
        finally:
            os.environ.pop("MOONSHOT_API_KEY", None)

    def test_every_role_has_a_preference_list(self):
        for role in ROLE_ROUTING:
            self.assertTrue(ROLE_ROUTING[role])
            for pid in ROLE_ROUTING[role]:
                self.assertIn(pid, registry(), f"{role} references unknown {pid}")


class TestRunGuards(unittest.TestCase):
    def test_missing_binary_is_data_not_exception(self):
        spec = ProviderSpec(id="ghost", kind="cli", display="G",
                            binary="definitely-not-installed-xyz",
                            argv=lambda p, m, e: ["definitely-not-installed-xyz"])
        res = run_provider(spec, "hello")
        self.assertFalse(res.ok)
        self.assertIn("not on PATH", res.error)

    def test_api_provider_refused_by_run_provider(self):
        res = run_provider(registry().get("kimi"), "hello")
        self.assertFalse(res.ok)
        self.assertIn("api provider", res.error)

    def test_timeout_reported_with_the_interactive_hint(self):
        """A hung tool is the common failure; the error must name the cause."""
        spec = ProviderSpec(
            id="sleeper", kind="cli", display="S", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c",
                                  "import time; time.sleep(30)"],
            timeout_s=1)
        res = run_provider(spec, "hello", timeout_s=1)
        self.assertFalse(res.ok)
        self.assertIn("timeout", res.error.lower())
        self.assertIn("interactive", res.error)

    def test_exit_zero_with_no_output_is_a_failure(self):
        spec = ProviderSpec(
            id="silent", kind="cli", display="S", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c", "pass"])
        res = run_provider(spec, "hello")
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("no stdout", res.error)

    def test_successful_call_captures_text(self):
        spec = ProviderSpec(
            id="echo", kind="cli", display="E", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c", f"print({p!r})"])
        res = run_provider(spec, "DOBBY_OK")
        self.assertTrue(res.ok, res.error)
        self.assertIn("DOBBY_OK", res.text)

    def test_non_ascii_output_survives(self):
        spec = ProviderSpec(
            id="korean", kind="cli", display="K", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c",
                                  "print('\\ud55c\\uad6d\\uc5b4 em\\u2014dash')"])
        res = run_provider(spec, "x")
        self.assertTrue(res.ok, res.error)
        self.assertIn("em—dash", res.text)

    def test_output_cap_marks_truncation(self):
        spec = ProviderSpec(
            id="loud", kind="cli", display="L", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c", "print('x'*5000)"])
        res = run_provider(spec, "x", output_cap=500)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(res.truncated)

    def test_nonzero_exit_carries_stderr(self):
        spec = ProviderSpec(
            id="crash", kind="cli", display="C", binary=sys.executable,
            argv=lambda p, m, e: [sys.executable, "-c",
                                  "import sys; sys.stderr.write('boom'); "
                                  "sys.exit(3)"])
        res = run_provider(spec, "x")
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 3)
        self.assertIn("boom", res.error)


def _fake(pid: str, script: str, mutates: bool = False) -> ProviderSpec:
    return ProviderSpec(
        id=pid, kind="cli", display=pid, binary=sys.executable,
        argv=lambda p, m, e, s=script: [sys.executable, "-c", s],
        mutates_worktree=mutates)


class TestFanout(unittest.TestCase):
    def test_empty_round_is_safe(self):
        r = run_round([])
        self.assertEqual(r.results, [])

    def test_one_failure_does_not_lose_the_others(self):
        # Register fakes through a patched registry so run_round can resolve them.
        import dobby.providers.fanout as fanout_mod
        fakes = ProviderRegistry([
            _fake("good1", "print('alpha beta gamma')"),
            _fake("bad", "import sys; sys.exit(9)"),
            _fake("good2", "print('delta epsilon zeta')"),
        ])
        original = fanout_mod.registry
        fanout_mod.registry = lambda: fakes
        try:
            round_ = run_round([AgentTask(provider_id=p, prompt="x")
                                for p in ("good1", "bad", "good2")],
                               isolate=False)
        finally:
            fanout_mod.registry = original
        self.assertEqual(len(round_.results), 3)
        self.assertEqual(len(round_.ok_results), 2)
        self.assertEqual(len(round_.summary()["failed"]), 1)

    def test_results_are_input_ordered(self):
        import dobby.providers.fanout as fanout_mod
        fakes = ProviderRegistry([
            _fake("slow", "import time; time.sleep(0.4); print('slow one')"),
            _fake("fast", "print('fast one')"),
        ])
        original = fanout_mod.registry
        fanout_mod.registry = lambda: fakes
        try:
            round_ = run_round([AgentTask(provider_id="slow", prompt="x"),
                                AgentTask(provider_id="fast", prompt="x")],
                               isolate=False)
        finally:
            fanout_mod.registry = original
        self.assertEqual([r.provider for r in round_.results], ["slow", "fast"])

    def test_isolation_only_when_two_or_more_mutate(self):
        import dobby.providers.fanout as fanout_mod
        fakes = ProviderRegistry([
            _fake("ro", "print('x')", mutates=False),
            _fake("rw1", "print('x')", mutates=True),
            _fake("rw2", "print('x')", mutates=True),
        ])
        original = fanout_mod.registry
        fanout_mod.registry = lambda: fakes
        try:
            one_writer = [AgentTask(provider_id="rw1", prompt="x"),
                          AgentTask(provider_id="ro", prompt="x")]
            two_writers = [AgentTask(provider_id="rw1", prompt="x"),
                           AgentTask(provider_id="rw2", prompt="x")]
            self.assertEqual(_needs_isolation(one_writer), 0)
            self.assertEqual(_needs_isolation(two_writers), 2)
        finally:
            fanout_mod.registry = original

    def test_speedup_recorded(self):
        import dobby.providers.fanout as fanout_mod
        fakes = ProviderRegistry([
            _fake("a", "import time; time.sleep(0.3); print('a')"),
            _fake("b", "import time; time.sleep(0.3); print('b')"),
        ])
        original = fanout_mod.registry
        fanout_mod.registry = lambda: fakes
        try:
            round_ = run_round([AgentTask(provider_id=p, prompt="x")
                                for p in ("a", "b")], isolate=False)
        finally:
            fanout_mod.registry = original
        # Two 0.3s calls in parallel must beat 0.6s of serial time.
        self.assertGreater(round_.speedup(), 1.2, round_.summary())


if __name__ == "__main__":
    unittest.main()
