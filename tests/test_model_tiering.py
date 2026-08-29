"""Which model a role gets, and whether the one that answered is the one asked.

Both halves come from measurements taken on this machine on 2026-08-29.

The first: model choice is worth making. Same prompt, same provider, only
`--model` differing.

    (unset)   claude-opus-5[1m]   249 out, 227 thinking   $0.2997   22.3s
    sonnet    claude-sonnet-5      47 out,   0 thinking   $0.1203   16.0s

Sixty percent of the cost for a lookup that reasoned about nothing. The
plumbing to ask for it already ran end to end -- `node.config["model"]` ->
`run_provider(model=)` -> `spec.build_argv` -> `--model` -- and nothing in the
runtime ever set it. A capability with no producer, which is the shape of the
quota ledger nothing imported and of `checks_at` before yesterday.

The second: asking is not getting.

    requested haiku                       answered claude-sonnet-5
    requested haiku                       answered claude-sonnet-5
    requested claude-haiku-4-5-20251001   answered claude-haiku-4-5-20251001
    requested sonnet                      answered claude-sonnet-5

That CLI does not resolve the `haiku` alias and falls back without a word. A
run pinning haiku to save money would be billed sonnet and never learn, so this
records whether the pin took. `evals/swebench/runner_arms.py` already kept both
names side by side and said in its own docstring that "them differing is a
finding" -- and never compared them. This does.

End to end, through a real `claude` call and an operator's declared table:

    role=scout      cheap=sonnet   ->  sonnet  answered claude-sonnet-5  ok
    role=architect  strong=opus    ->  opus    answered claude-opus-5    ok
    role=scout      cheap=haiku    ->  haiku   answered claude-sonnet-5  NOT ok
"""

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers.models import (ROLE_TIER, TIERS, honoured,  # noqa: E402
                                    load_table, model_for, tier_for)


class TheTiers(unittest.TestCase):
    def test_reading_roles_are_cheap_and_deciding_roles_are_strong(self):
        self.assertEqual(tier_for("scout"), "cheap")
        self.assertEqual(tier_for("mechanical"), "cheap")
        self.assertEqual(tier_for("architect"), "strong")
        self.assertEqual(tier_for("adjudicate"), "strong")

    def test_an_unclassified_role_is_standard_rather_than_cheap(self):
        """A role nobody thought about must not be quietly downgraded."""
        self.assertEqual(tier_for("something-new"), "standard")
        self.assertEqual(tier_for(""), "standard")

    def test_every_declared_tier_is_a_real_tier(self):
        self.assertTrue(set(ROLE_TIER.values()) <= set(TIERS))


class ChoosingAModel(unittest.TestCase):
    TABLE = {"claude": {"cheap": "sonnet", "strong": "opus"}}

    def test_a_declared_tier_is_used(self):
        self.assertEqual(model_for("claude", "scout", table=self.TABLE),
                         "sonnet")
        self.assertEqual(model_for("claude", "architect", table=self.TABLE),
                         "opus")

    def test_an_undeclared_provider_gets_nothing(self):
        """No `--model` flag, so the provider's own default stands -- which is
        what every run did before this existed."""
        self.assertEqual(model_for("codex", "scout", table=self.TABLE), "")

    def test_an_empty_table_gets_nothing(self):
        self.assertEqual(model_for("claude", "scout", table={}), "")

    def test_a_missing_tier_falls_upward_and_not_across_providers(self):
        """`standard` is undeclared above. An operator who configured only
        cheap and strong gets one of THEIR models, never somebody else's."""
        chosen = model_for("claude", "implement", table=self.TABLE)
        self.assertIn(chosen, {"sonnet", "opus"})
        self.assertEqual(chosen, "opus", "upward: never quietly cheaper")


class ReadingTheOperatorsTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def write(self, blob):
        path = os.path.join(self.tmp.name, "config.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(blob if isinstance(blob, str)
                     else json.dumps(blob, ensure_ascii=False))
        return self.tmp.name

    def test_it_reads_what_was_declared(self):
        data = self.write({"providers": {"models":
                                         {"claude": {"cheap": "sonnet"}}}})
        self.assertEqual(load_table(data), {"claude": {"cheap": "sonnet"}})

    def test_no_config_at_all_is_an_empty_table(self):
        self.assertEqual(load_table(self.tmp.name), {})

    def test_a_config_without_the_key_is_an_empty_table(self):
        self.assertEqual(load_table(self.write({"version": "2.1"})), {})

    def test_damaged_json_falls_back_rather_than_raising(self):
        """`doctor` reports a broken config. Choosing a model is not the place
        to raise about it, and pretending it said something is worse."""
        self.assertEqual(load_table(self.write("{not json")), {})

    def test_non_string_models_are_dropped_not_coerced(self):
        data = self.write({"providers": {"models": {"claude":
                                                    {"cheap": 7,
                                                     "strong": "opus"}}}})
        self.assertEqual(load_table(data), {"claude": {"strong": "opus"}})


class WasThePinHonoured(unittest.TestCase):
    def test_an_alias_expanding_is_agreement(self):
        self.assertIs(honoured("sonnet", "claude-sonnet-5"), True)
        self.assertIs(honoured("opus", "claude-opus-5"), True)

    def test_the_measured_substitution_is_caught(self):
        """`haiku` answered by `claude-sonnet-5`, twice, with no error."""
        self.assertIs(honoured("haiku", "claude-sonnet-5"), False)

    def test_a_full_id_that_is_honoured_agrees(self):
        self.assertIs(honoured("claude-haiku-4-5-20251001",
                               "claude-haiku-4-5-20251001"), True)

    def test_unknowable_is_not_the_same_as_agreement(self):
        """A CLI reporting nothing has confirmed nothing. Flattening this into
        True turns "we cannot tell" into "it was fine"."""
        self.assertIsNone(honoured("sonnet", ""))
        self.assertIsNone(honoured("", "claude-sonnet-5"))
        self.assertIsNone(honoured("", ""))

    def test_case_and_separators_do_not_decide_it(self):
        self.assertIs(honoured("Claude Sonnet 5", "claude-sonnet-5"), True)


class ThroughTheWorker(unittest.TestCase):
    """The worker's own wiring, with `run_provider` stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, ".dobby")
        os.makedirs(self.data)

    def declare(self, table):
        with open(os.path.join(self.data, "config.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            json.dump({"providers": {"models": table}}, fh)

    def call(self, *, role, node_model=None, answered="claude-sonnet-5"):
        from dobby.providers import run as run_module
        from dobby.providers.run import ProviderResult
        from dobby.runtime import graph as G
        from dobby.runtime.contracts import ArtifactContract
        from dobby.runtime.workers import ProviderWorker

        seen = {}

        def stub(spec, prompt, **kw):
            seen.update(kw)
            return ProviderResult(provider=spec.id, ok=True,
                                  text='{"done": true}', meta={},
                                  duration_s=0.1,
                                  usage={"model": answered} if answered
                                  else None)

        config = {"provider": "claude", "provider_role": role, "schema": None}
        if node_model:
            config["model"] = node_model
        node = G.TaskNode(node_id="n", kind=role, worker="provider",
                          instruction="i",
                          contract=ArtifactContract(
                              output_schema={"type": "object"}),
                          config=config)
        original = run_module.run_provider
        run_module.run_provider = stub
        try:
            result = ProviderWorker().run(node, {
                "repo": self.tmp.name, "attempt": 0, "isolated": False,
                "inputs": {}, "run_id": "r", "data_dir": self.data,
                "collect_usage": True})
        finally:
            run_module.run_provider = original
        return seen, (result.meta or {})

    def test_the_declared_tier_reaches_run_provider(self):
        self.declare({"claude": {"cheap": "sonnet", "strong": "opus"}})
        seen, _ = self.call(role="scout")
        self.assertEqual(seen["model"], "sonnet")

    def test_a_deciding_role_gets_the_strong_model(self):
        self.declare({"claude": {"cheap": "sonnet", "strong": "opus"}})
        seen, _ = self.call(role="architect", answered="claude-opus-5")
        self.assertEqual(seen["model"], "opus")

    def test_a_model_named_on_the_node_wins_over_the_table(self):
        self.declare({"claude": {"cheap": "sonnet"}})
        seen, _ = self.call(role="scout", node_model="opus",
                            answered="claude-opus-5")
        self.assertEqual(seen["model"], "opus")

    def test_with_no_table_nothing_is_pinned(self):
        """The default. `--model` is not passed and the provider's own default
        stands, exactly as before this existed."""
        seen, meta = self.call(role="scout")
        self.assertIsNone(seen["model"])
        self.assertIsNone(meta["model"])

    def test_the_substitution_is_recorded_on_the_result(self):
        self.declare({"claude": {"cheap": "haiku"}})
        _, meta = self.call(role="scout", answered="claude-sonnet-5")
        self.assertEqual(meta["model"], "haiku")
        self.assertEqual(meta["model_reported"], "claude-sonnet-5")
        self.assertIs(meta["model_honoured"], False)

    def test_agreement_is_recorded_too(self):
        self.declare({"claude": {"cheap": "sonnet"}})
        _, meta = self.call(role="scout", answered="claude-sonnet-5")
        self.assertIs(meta["model_honoured"], True)

    def test_a_provider_naming_no_model_is_unknowable(self):
        self.declare({"claude": {"cheap": "sonnet"}})
        _, meta = self.call(role="scout", answered=None)
        self.assertIsNone(meta["model_honoured"])


class StrictMode(unittest.TestCase):
    """`DOBBY_REQUIRE_PINNED_MODEL`: a substitution stops the node.

    Off by default, because turning it on everywhere would change what an
    existing run costs and how it fails, and a substitution is a billing
    surprise rather than a wrong answer -- the model that replied still
    replied.

    NON_RETRYABLE and not CAPACITY. `DEFAULT_POLICY[CAPACITY]` is
    RETRY_ELSEWHERE, which would send a `claude` model id somewhere it cannot
    be honoured either; and retrying the SAME provider reproduces the
    substitution, measured twice on the same alias. The declaration is what
    cannot be satisfied, and the scheduler has no move that fixes a
    declaration.

    What it cannot catch, stated because the gap is real: a provider that names
    no model at all. `codex` names none, so `None` is never a failure here.
    Treating "we cannot tell" as a violation would make this flag mean "codex
    is banned", which is a different feature and would say so in the wrong
    words.
    """

    def setUp(self):
        from dobby.providers.models import STRICT_ENV

        self.env = STRICT_ENV
        self.original = os.environ.get(self.env)
        self.addCleanup(self.restore)

    def restore(self):
        if self.original is None:
            os.environ.pop(self.env, None)
        else:
            os.environ[self.env] = self.original

    def test_it_is_off_unless_asked_for(self):
        from dobby.providers.models import strict

        os.environ.pop(self.env, None)
        self.assertFalse(strict())

    def test_the_usual_spellings_turn_it_on(self):
        from dobby.providers.models import strict

        for raw in ("1", "true", "TRUE", "yes", "on"):
            os.environ[self.env] = raw
            self.assertTrue(strict(), raw)

    def test_anything_else_leaves_it_off(self):
        from dobby.providers.models import strict

        for raw in ("0", "false", "no", "", "maybe"):
            os.environ[self.env] = raw
            self.assertFalse(strict(), raw)

    def test_the_refusal_names_both_models_and_a_way_out(self):
        from dobby.providers.models import refusal

        text = refusal("haiku", "claude-sonnet-5")
        self.assertIn("haiku", text)
        self.assertIn("claude-sonnet-5", text)
        self.assertIn("claude-haiku-4-5-20251001", text)


class StrictModeThroughTheWorker(ThroughTheWorker):
    def setUp(self):
        super().setUp()
        from dobby.providers.models import STRICT_ENV

        self.env = STRICT_ENV
        self.original = os.environ.get(self.env)
        os.environ[self.env] = "1"
        self.addCleanup(self.restore)

    def restore(self):
        if self.original is None:
            os.environ.pop(self.env, None)
        else:
            os.environ[self.env] = self.original

    def test_a_substitution_fails_the_node(self):
        self.declare({"claude": {"cheap": "haiku"}})
        from dobby.providers import run as run_module
        from dobby.providers.run import ProviderResult
        from dobby.runtime import graph as G
        from dobby.runtime.contracts import ArtifactContract
        from dobby.runtime.workers import ProviderWorker

        def stub(spec, prompt, **kw):
            return ProviderResult(provider=spec.id, ok=True, text="{}",
                                  meta={}, duration_s=0.1,
                                  usage={"model": "claude-sonnet-5"})

        node = G.TaskNode(node_id="n", kind="scout", worker="provider",
                          instruction="i",
                          contract=ArtifactContract(
                              output_schema={"type": "object"}),
                          config={"provider": "claude",
                                  "provider_role": "scout", "schema": None})
        original = run_module.run_provider
        run_module.run_provider = stub
        try:
            result = ProviderWorker().run(node, {
                "repo": self.tmp.name, "attempt": 0, "isolated": False,
                "inputs": {}, "run_id": "r", "data_dir": self.data,
                "collect_usage": True})
        finally:
            run_module.run_provider = original

        self.assertFalse(result.ok)
        self.assertEqual(result.failure.failure_class, "NON_RETRYABLE")
        self.assertIs(result.meta["model_honoured"], False)

    def test_an_honoured_pin_still_passes(self):
        self.declare({"claude": {"cheap": "sonnet"}})
        _, meta = self.call(role="scout", answered="claude-sonnet-5")
        self.assertIs(meta["model_honoured"], True)

    def test_a_provider_naming_nothing_is_not_failed(self):
        """Otherwise the flag means "codex is banned"."""
        self.declare({"claude": {"cheap": "sonnet"}})
        _, meta = self.call(role="scout", answered=None)
        self.assertIsNone(meta["model_honoured"])


if __name__ == "__main__":
    unittest.main()
