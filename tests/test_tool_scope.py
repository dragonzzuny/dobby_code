"""Narrowing a CLI's built-in tool surface, and where that must not reach.

The lever is a token one. `write_extra` decides whether a provider may edit at
all; this decides how many tool schemas it carries while doing it. Measured on
this machine 2026-08-24: the full built-in set costs 7,603 tokens per call above
tools-disabled, and `Read,Edit,Bash` costs 2,050 — so a node handed everything
it does not need pays about 5,550 tokens for nothing, on every call it makes.
"""

import unittest

from dobby.providers.catalog import registry


class TestSpecToolScope(unittest.TestCase):
    def setUp(self):
        self.claude = registry().get("claude")

    def test_claude_declares_how_to_narrow_its_tools(self):
        self.assertEqual(self.claude.tool_scope_extra, ("--tools", "{tools}"))

    def test_a_comma_string_is_passed_through(self):
        self.assertEqual(self.claude.tool_scope("Read,Edit,Bash"),
                         ("--tools", "Read,Edit,Bash"))

    def test_a_sequence_is_joined(self):
        self.assertEqual(self.claude.tool_scope(["Read", "Edit"]),
                         ("--tools", "Read,Edit"))

    def test_none_means_no_preference_and_yields_nothing(self):
        self.assertEqual(self.claude.tool_scope(None), ())

    def test_empty_string_is_a_real_request_for_no_tools(self):
        """`--tools ""` is the documented way to disable all of them.

        Collapsing it into `None` would make the cheapest setting unreachable,
        and it is the one that establishes the 22,565-token floor.
        """
        self.assertEqual(self.claude.tool_scope(""), ("--tools", ""))

    def test_a_provider_without_the_flag_declines_rather_than_inventing_one(self):
        for pid in ("codex", "agy", "gemini"):
            spec = registry().get(pid)
            if spec.tool_scope_extra:
                continue
            self.assertEqual(
                spec.tool_scope("Read"), (),
                f"{pid} has no verified tool-scope flag and must not be given "
                f"an invented one")


class TestWorkerApplication(unittest.TestCase):
    """The worker appends the scope AFTER the write grant, never instead."""

    def test_the_scope_is_appended_not_substituted(self):
        spec = registry().get("claude")
        grant = tuple(spec.write_extra) + spec.tool_scope("Read,Edit,Bash")
        self.assertEqual(grant[:2], ("--permission-mode", "acceptEdits"))
        self.assertEqual(grant[-2:], ("--tools", "Read,Edit,Bash"))

    def test_a_node_that_asks_for_nothing_keeps_the_default_surface(self):
        """No opt-in means no behaviour change for anything already running."""
        spec = registry().get("claude")
        self.assertEqual(tuple(spec.write_extra) + spec.tool_scope(None),
                         tuple(spec.write_extra))


class _Contract:
    def __init__(self, side_effect_class):
        self.side_effect_class = side_effect_class


class _Node:
    def __init__(self, side_effect_class, **config):
        self.contract = _Contract(side_effect_class)
        self.config = config


class TestToolsForNode(unittest.TestCase):
    """`tools_for` derives the set from the DECLARED side effect, not the prompt."""

    def test_absent_means_no_preference_and_nothing_changes(self):
        from dobby.runtime.workers import tools_for
        self.assertIsNone(tools_for(_Node("LOCAL_WRITE")))

    def test_auto_gives_a_writing_node_an_editing_tool(self):
        from dobby.runtime.contracts import LOCAL_WRITE
        from dobby.runtime.workers import WRITING_TOOLS, tools_for
        got = tools_for(_Node(LOCAL_WRITE, tools="auto"))
        self.assertEqual(got, WRITING_TOOLS)
        self.assertIn("Edit", got)

    def test_auto_denies_a_read_only_node_an_editing_tool(self):
        from dobby.runtime.workers import READ_ONLY_TOOLS, tools_for
        got = tools_for(_Node("NONE", tools="auto"))
        self.assertEqual(got, READ_ONLY_TOOLS)
        self.assertNotIn("Edit", got)
        self.assertNotIn("Write", got)
        self.assertNotIn("Bash", got)

    def test_an_explicit_list_overrides_the_contract(self):
        from dobby.runtime.workers import tools_for
        self.assertEqual(tools_for(_Node("NONE", tools="Read")), "Read")

    def test_the_read_only_set_is_a_subset_of_the_writing_set(self):
        """A node that gains write rights must not LOSE a tool it had."""
        from dobby.runtime.workers import READ_ONLY_TOOLS, WRITING_TOOLS
        self.assertTrue(set(READ_ONLY_TOOLS.split(","))
                        <= set(WRITING_TOOLS.split(",")))


if __name__ == "__main__":
    unittest.main()
