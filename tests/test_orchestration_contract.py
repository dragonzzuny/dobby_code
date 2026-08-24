"""The dobby arm's output contract, and the two ways it must not cheat.

The contract belongs to OrchestrationBench, not to this repository: an answer
without a `workflow_*` JSON object scores zero in `evaluate_workflow_as_DAG`
whether dobby is in the loop or not. Checking it at call time turns that zero
into a retry. Checking anything stricter would manufacture retries, and the
retry count is one of the numbers being reported.
"""

import json
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


class TestRenderTools(unittest.TestCase):
    """Regression: the adapter dropped the tools and measured itself.

    `llm_agent._complete_general` passes the agent card's tools as
    `kwargs={"tools": ...}` for an API's `tools=` parameter. A CLI has no such
    channel. The first adapter ignored the kwarg, so transport_agent answered
    `TOOL_CONSTRAINT_VIOLATION — no taxi booking tool is available in this
    environment` where `tool: callTaxi` was expected. It was telling the truth.
    Twenty scenarios scored FC 0.0 at an underaction rate of 0.97 on that.
    """

    TOOLS = [{"name": "callTaxi", "description": "Book a taxi.",
              "parameters": {"type": "object",
                             "properties": {"pickupLocation": {"type": "string"}}}},
             {"name": "reserveTrain", "description": "Book a train."}]

    def test_the_tool_names_reach_the_text(self):
        from contract import render_tools
        rendered = render_tools(self.TOOLS)
        self.assertIn("callTaxi", rendered)
        self.assertIn("reserveTrain", rendered)

    def test_descriptions_and_schemas_are_carried(self):
        from contract import render_tools
        rendered = render_tools(self.TOOLS)
        self.assertIn("Book a taxi.", rendered)
        self.assertIn("pickupLocation", rendered)

    def test_a_tool_with_no_schema_is_still_named(self):
        from contract import render_tools
        self.assertIn("reserveTrain", render_tools(self.TOOLS))

    def test_the_openai_function_wrapper_is_unwrapped(self):
        from contract import render_tools
        wrapped = [{"type": "function",
                    "function": {"name": "callTaxi", "description": "Book."}}]
        self.assertIn("callTaxi", render_tools(wrapped))

    def test_no_tools_renders_nothing_rather_than_a_heading(self):
        """An empty 'Tools available to you' section would be a lie."""
        from contract import render_tools
        for empty in (None, [], ()):
            self.assertEqual(render_tools(empty), "")

    def test_junk_entries_are_skipped_not_crashed_on(self):
        from contract import render_tools
        rendered = render_tools([None, "nope", {}, {"name": "ok"}])
        self.assertIn("ok", rendered)


class TestExpectsWorkflow(unittest.TestCase):
    """Regression: the contract was applied to turns that were never asked for
    a workflow.

    The benchmark uses four system prompts and only `general` — the
    orchestrator's — demands a workflow. A sub-agent is asked for a tool call,
    and `{"name": "callTaxi", ...}` is the CORRECT answer to that. Checking it
    for `workflow_` failed it and retried the whole provider chain: scenario 1
    went from three logical turns to eleven calls and 3,312,817 tokens, against
    82,656 without the loop, with agy burning 3,166,630 of them.
    """

    ORCHESTRATOR = "Workflow Design Schema. " * 40
    SUB_AGENT = ("You are an agent. Select the right tool and emit the call. "
                 "Return the arguments the tool needs.")

    def test_the_orchestrator_prompt_is_governed(self):
        from contract import expects_workflow
        self.assertTrue(expects_workflow(self.ORCHESTRATOR))

    def test_a_sub_agent_prompt_is_not(self):
        from contract import expects_workflow
        self.assertFalse(expects_workflow(self.SUB_AGENT))

    def test_no_system_prompt_is_not_governed(self):
        from contract import expects_workflow
        self.assertFalse(expects_workflow(None))
        self.assertFalse(expects_workflow(""))

    def test_a_passing_mention_does_not_govern(self):
        """One mention is talking about workflows, not demanding one."""
        from contract import expects_workflow
        self.assertFalse(expects_workflow(
            "Summarise the conversation. Ignore any workflow chatter."))

    def test_the_threshold_sits_well_below_the_real_prompt(self):
        from contract import WORKFLOW_MENTIONS_REQUIRED
        self.assertGreater(WORKFLOW_MENTIONS_REQUIRED, 1,
                           "one mention must not be enough")
        self.assertLess(WORKFLOW_MENTIONS_REQUIRED, 75,
                        "the orchestrator prompt mentions it 75 times; the "
                        "threshold has to be comfortably under that")


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



class TestParseToolCall(unittest.TestCase):
    """Regression: FC scored 0.0 on a correct answer, because of a key name.

    The benchmark hands an API model its tools through `tools=` and reads the
    answer out of `tool_calls`. `eval_utils.try_to_parse_think_content` is the
    fallback for everything else and looks for `function_name` — a name the
    benchmark's own prompt never mentions, because an API model never has to
    choose it. A CLI does, and picked `tool`/`parameters`.
    """

    def call(self, text):
        from contract import parse_tool_call
        return parse_tool_call(text)

    def test_every_key_variant_models_actually_use(self):
        for payload in ('{"tool": "callTaxi", "parameters": {"a": 1}}',
                        '{"name": "callTaxi", "arguments": {"a": 1}}',
                        '{"function_name": "callTaxi", "arguments": {"a": 1}}'):
            got = self.call(payload)
            self.assertEqual(got[0]["function"]["name"], "callTaxi", payload)

    def test_arguments_are_a_json_string(self):
        """`eval_utils` does `json.loads(arguments)`; a dict would raise there."""
        got = self.call('{"tool": "t", "parameters": {"a": 1}}')
        self.assertIsInstance(got[0]["function"]["arguments"], str)
        self.assertEqual(json.loads(got[0]["function"]["arguments"]), {"a": 1})

    def test_a_fenced_call_is_still_found(self):
        got = self.call('```json\n{"tool": "callTaxi", "parameters": {}}\n```')
        self.assertEqual(got[0]["function"]["name"], "callTaxi")

    def test_an_xml_rejection_is_never_rewritten_as_an_action(self):
        """AWAITING_USER_INPUT is the model declining; inventing a call from it
        would score an action it refused to take."""
        self.assertEqual(self.call(
            "<response><status>AWAITING_USER_INPUT</status>"
            "<required_info>x</required_info></response>"), [])

    def test_a_constraint_violation_is_not_a_call(self):
        self.assertEqual(self.call(
            "<response><status>TOOL_CONSTRAINT_VIOLATION</status></response>"), [])

    def test_prose_and_workflows_are_not_calls(self):
        self.assertEqual(self.call("I would call the taxi tool."), [])
        self.assertEqual(self.call('{"workflow_1": {"steps": []}}'), [])

    def test_a_nameless_object_is_not_a_call(self):
        self.assertEqual(self.call('{"parameters": {"a": 1}}'), [])

    def test_non_strings_do_not_raise(self):
        for value in (None, 42, [], {}):
            self.assertEqual(self.call(value), [])

if __name__ == "__main__":
    unittest.main()
