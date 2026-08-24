"""The dobby arm's output contract, and the two ways it must not cheat.

The contract belongs to OrchestrationBench, not to this repository: an answer
without a `workflow_*` JSON object scores zero in `evaluate_workflow_as_DAG`
whether dobby is in the loop or not. Checking it at call time turns that zero
into a retry. Checking anything stricter would manufacture retries, and the
retry count is one of the numbers being reported.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "evals", "orchestration"))

from contract import (DEFAULT_RETRY_CHAIN, carries_workflow,  # noqa: E402
                      retry_chain, summarise)

GOOD = '{"workflow_1": {"steps": [{"agent": "a", "status": "done"}]}}'
FENCED = "Here you go:\n```json\n" + GOOD + "\n```\nhope that helps"
#: The benchmark's own gold answers are YAML, not JSON. See
#: `data/EN/scenario_data/1.yaml`.
YAML_GOLD = ("workflow_1:\n  status: pending\n  type: independent\n"
             "  steps:\n    - status: pending\n      name: transport_agent\n")


def fake_extract(text):
    """Stands in for the benchmark's extractor, which is not importable here.

    The real one is `evaluate_workflow_as_DAG.extract_workflow_from_content` and
    `contract.load_extractor()` returns exactly that. This stub covers the two
    paths these tests exercise — bare JSON and whole-content YAML — and exists
    so the contract's DECISIONS can be tested without a benchmark checkout.
    """
    import json as _json

    import yaml as _yaml
    for loader in (_json.loads, _yaml.safe_load):
        try:
            parsed = loader(text)
        except Exception:                          # noqa: BLE001
            continue
        if isinstance(parsed, dict) and any(
                str(k).startswith("workflow_") for k in parsed):
            return parsed
    return {}


class TestContract(unittest.TestCase):
    def test_a_bare_workflow_object_passes(self):
        self.assertTrue(carries_workflow(GOOD, fake_extract))

    def test_a_yaml_workflow_passes(self):
        """The benchmark's OWN gold answers are YAML. A JSON-only contract
        would have rejected correct replies and manufactured retries."""
        self.assertTrue(carries_workflow(YAML_GOLD, fake_extract))

    def test_prose_with_no_json_fails(self):
        self.assertFalse(carries_workflow("I would first call the search tool.", fake_extract))

    def test_valid_json_without_a_workflow_key_fails(self):
        self.assertFalse(carries_workflow('{"plan": {"steps": []}}', fake_extract))

    def test_the_codex_envelope_that_started_this_fails(self):
        """Measured 2026-08-24: codex answered a schema request with this."""
        self.assertFalse(carries_workflow(
            '{"type": "thread.started", "thread_id": "01a0312f"}',
            fake_extract))

    def test_a_workflow_key_in_a_string_but_no_object_fails(self):
        """The regex alone is not the contract; the JSON has to parse."""
        self.assertFalse(carries_workflow('the key is "workflow_1" but broken {', fake_extract))

    def test_non_strings_fail_rather_than_raise(self):
        for value in (None, 42, [], {}):
            self.assertFalse(carries_workflow(value, fake_extract))


class TestRetryChain(unittest.TestCase):
    def test_the_primary_goes_first(self):
        self.assertEqual(retry_chain("codex")[0], "codex")

    def test_the_primary_is_never_asked_twice(self):
        """A model's second pass is correlated with its first."""
        chain = retry_chain("claude")
        self.assertEqual(chain.count("claude"), 1)

    def test_every_other_provider_appears_once(self):
        chain = retry_chain("codex", ("claude", "codex", "agy"))
        self.assertEqual(sorted(chain), ["agy", "claude", "codex"])

    def test_a_primary_outside_the_chain_is_still_first(self):
        chain = retry_chain("gemini", DEFAULT_RETRY_CHAIN)
        self.assertEqual(chain[0], "gemini")
        self.assertEqual(len(chain), len(DEFAULT_RETRY_CHAIN) + 1)


class TestSummarise(unittest.TestCase):
    def attempt(self, index, provider, ok=True, passed=True):
        return {"index": index, "provider": provider, "ok": ok,
                "carries_workflow": passed}

    def test_a_clean_solo_call_has_no_violations(self):
        got = summarise([self.attempt(0, "claude")], arm="solo",
                        provider="claude")
        self.assertEqual(got["contract_violations"], 0)
        self.assertEqual(got["contract_violation_rate"], 0.0)
        self.assertEqual(got["retries_on_another_provider"], 0)

    def test_a_recovered_retry_is_recorded_as_such(self):
        got = summarise([self.attempt(0, "codex", passed=False),
                         self.attempt(1, "claude")],
                        arm="dobby", provider="codex")
        self.assertEqual(got["contract_violations"], 1)
        self.assertEqual(got["retries_on_another_provider"], 1)
        self.assertTrue(got["recovered_by_retry"])
        self.assertEqual(got["providers_used"], ["claude", "codex"])

    def test_a_provider_that_never_answered_is_not_a_violation(self):
        """An outage and a formatting miss are different failures."""
        got = summarise([self.attempt(0, "agy", ok=False, passed=False)],
                        arm="dobby", provider="agy")
        self.assertEqual(got["answered"], 0)
        self.assertEqual(got["contract_violations"], 0)
        self.assertIsNone(got["contract_violation_rate"],
                          "no answered call means no rate, and 0.0 would read "
                          "as a clean run")

    def test_a_retry_that_also_missed_is_not_recovered(self):
        got = summarise([self.attempt(0, "codex", passed=False),
                         self.attempt(1, "agy", passed=False)],
                        arm="dobby", provider="codex")
        self.assertFalse(got["recovered_by_retry"])
        self.assertEqual(got["contract_violations"], 2)


if __name__ == "__main__":
    unittest.main()
