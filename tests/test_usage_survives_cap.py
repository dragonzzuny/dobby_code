"""The output cap bounds the ANSWER; it must not cost the token count.

Regression for a measured loss: codex streams JSONL and puts `turn.completed`,
which carries every token count, at the end of the stream. `run_provider` parsed
usage out of the ALREADY-CAPPED text, so any task whose output passed
DEFAULT_OUTPUT_CAP (24,000 chars) recorded `calls_measured: 0` — a real call, real
tokens spent, and a row that could only say it did not know. Seen on a django
SWE-bench instance 2026-08-24.
"""

import json
import unittest
from unittest import mock

from dobby.providers.catalog import registry
from dobby.providers.run import DEFAULT_OUTPUT_CAP, run_provider


def codex_stream(filler_chars: int) -> str:
    """A codex JSONL stream whose usage event is past `filler_chars` of noise."""
    lines = [json.dumps({"item": {"type": "agent_message",
                                  "text": "x" * filler_chars}})]
    lines.append(json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 14957, "cached_input_tokens": 11008,
                  "cache_write_input_tokens": 0, "output_tokens": 6,
                  "reasoning_output_tokens": 3}}))
    return "\n".join(lines)


class _Proc:
    def __init__(self, stdout):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


class TestUsageSurvivesCap(unittest.TestCase):
    def _run(self, stdout, **kw):
        spec = registry().get("codex")
        with mock.patch("subprocess.run", return_value=_Proc(stdout)), \
             mock.patch.object(type(spec), "which", lambda self: "codex"):
            return run_provider(spec, "prompt", collect_usage=True, **kw)

    def test_usage_is_parsed_when_the_stream_fits(self):
        result = self._run(codex_stream(50))
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.usage)
        self.assertEqual(result.usage["input_tokens"], 14957)

    def test_usage_is_parsed_when_the_stream_far_exceeds_the_cap(self):
        result = self._run(codex_stream(DEFAULT_OUTPUT_CAP * 3))
        self.assertIsNotNone(
            result.usage,
            "the usage event is at the END of the stream; capping before "
            "parsing threw away the only record of what the call cost")
        self.assertEqual(result.usage["input_tokens"], 14957)
        self.assertEqual(result.usage["output_tokens"], 6)
        self.assertEqual(result.usage["thinking_tokens"], 3)

    def test_the_answer_is_still_capped(self):
        """The cap must keep doing its job — this is not a licence to return it all."""
        result = self._run(codex_stream(DEFAULT_OUTPUT_CAP * 3))
        self.assertLessEqual(len(result.text), DEFAULT_OUTPUT_CAP + 200)
        self.assertTrue(result.truncated)

    def test_a_short_answer_is_not_marked_truncated(self):
        result = self._run(codex_stream(50))
        self.assertFalse(result.truncated)

    def test_an_unparseable_stream_still_returns_the_capped_text(self):
        result = self._run("not json at all\n" * 5000)
        self.assertTrue(result.ok)
        self.assertIsNone(result.usage)
        self.assertLessEqual(len(result.text), DEFAULT_OUTPUT_CAP + 200)


if __name__ == "__main__":
    unittest.main()
