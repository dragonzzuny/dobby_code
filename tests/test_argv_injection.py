"""One argument has to stay one argument.

The gateway's header says arguments "are validated and shell-quoted -- the
model never composes raw shell". It does not, and every shell metacharacter
probe bounced off `safe_arg`. But it composed argv, which for a fixed-template
design is the same class of failure: the caller controlled more of the command
than the template author wrote.

`_exec` split argument values on NUL to support multi-path arguments, and the
split ran BEFORE validation, so `safe_arg` never saw the separator. Measured
against `bootstrap_scan`, whose template is
`{python} -m dobby.cli init --scan {scan_root} --overwrite`:

    scan_root  ".\\x00--overwrite\\x00--scan\\x00/"
    assembled  init --scan . --overwrite --scan / --overwrite
    argv       7 tokens from a template that has 4

The caller redirected the scan to the filesystem root without a single shell
metacharacter, and `guard_command` answered `(True, 'no destructive token')` --
correctly, since nothing in that command line is a destructive token. Quoting
keeps one argument one argument. A separator inside the argument is exactly how
that guarantee is lost, and the split threw the guarantee away before the check
that was supposed to protect it ever ran.

The split was also redundant. A list argument is the explicit way to pass
several values and it sits four lines below, so nothing was gained by making a
scalar do it too.

NOT fixed, and reported rather than changed: an argument beginning with `-`
still becomes a flag rather than a value -- `--scan -rf` reaches the command as
the flag `-rf`. `"--overwrite"` is listed in this suite's own LEGITIMATE_ARGS,
so accepting leading dashes is a standing decision of the project and not an
oversight to be quietly reversed under cover of an unrelated fix. It is a real
argument-injection surface for any template whose command takes flags, and it
belongs in a decision, not in this commit.
"""

import os
import shlex
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

import dobby_mcp_server as M  # noqa: E402

from dobby.core.security import safe_arg  # noqa: E402

#: Values that must never reach a command line, each a way of turning one
#: argument into several or of writing to the reader's terminal.
SEPARATORS = [
    ".\x00--overwrite\x00--scan\x00/",
    "a\x00b",
    "\x00",
    "path\x00",
    "x\x1b[31mred",
    "x\x07",
    "col\x08\x08\x08umn",
    "a\x1fb",
    "a\x7fb",
]

#: Values that must keep working. Paths are the whole point of this gateway.
INNOCENT = [".", "..", "tests", "src/main.py", r"C:\Users\x\project",
            r"C:\Program Files (x86)\Tool", "my project", "v1.2.3",
            "feature/branch-name", "2026-07-26", ""]


class TheCheckSeesWhatWasActuallySent(unittest.TestCase):
    """`safe_arg` used to be handed the pieces, not the value."""

    def test_every_separator_is_refused(self):
        leaked = [v for v in SEPARATORS if safe_arg(v)[0]]
        self.assertEqual(leaked, [], f"accepted: {leaked!r}")

    def test_nul_is_named_in_the_reason(self):
        ok, why = safe_arg("a\x00b")
        self.assertFalse(ok)
        self.assertIn("control character", why)

    def test_the_reason_says_why_it_matters(self):
        _, why = safe_arg("a\x00b")
        self.assertIn("one argument", why)

    def test_no_innocent_argument_is_collateral_damage(self):
        rejected = [(v, safe_arg(v)[1]) for v in INNOCENT if not safe_arg(v)[0]]
        self.assertEqual(rejected, [], f"rejected: {rejected!r}")

    def test_tab_and_newline_are_still_refused(self):
        """Newline was already a shell metacharacter; tab was not, and both
        are separators to something."""
        self.assertFalse(safe_arg("a\tb")[0])
        self.assertFalse(safe_arg("a\nb")[0])


class Ran:
    """What `subprocess.run` would have returned, without running anything."""

    returncode = 0
    stdout = "(not executed)"
    stderr = ""


class GatewayCase(unittest.TestCase):
    """No test here executes a command, and that is load-bearing.

    `--data` moves the audit log and the trajectory corpus, but `_exec` runs
    with `cwd=self.repo` -- the real repository, because these tests need its
    registry. An earlier version of this file called the real
    `bootstrap_scan`, which is `dobby init --scan {root} --overwrite`, and
    rescanning this repository rewrote `.dobby/knowledge/kg.bootstrap.json`
    and `.dobby/inventory.json` on every run. Both are generated and
    gitignored, so `git status` said nothing and the churn was invisible.

    Worse, it hid real damage. While probing the unfixed code, one argument
    resolved to a filesystem root, the rescan wrote a repository node with an
    empty name, and the gateway stopped loading -- see
    tests/test_root_scan_bricks_the_graph.py. The command is intercepted here
    so a test can assert an argument REACHES execution without anything
    executing.
    """

    def setUp(self):
        if not os.path.isdir(os.path.join(REPO, ".dobby")):
            self.skipTest("no .dobby in this checkout")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = os.path.join(self.tmp, "d")
        shutil.copytree(os.path.join(REPO, ".dobby"), self.data)
        self.gateway = M.Gateway(REPO, self.data)
        self.cap = self.gateway.capabilities.get("bootstrap_scan")
        if self.cap is None:
            self.skipTest("bootstrap_scan is not registered here")
        self.commands = []
        original = M.subprocess.run
        self.addCleanup(setattr, M.subprocess, "run", original)

        def intercept(cmd, *args, **kwargs):
            self.commands.append(cmd)
            return Ran()

        M.subprocess.run = intercept

    def run_with(self, value):
        return self.gateway._exec(self.cap, {"scan_root": value})

    @staticmethod
    def rejected(result):
        return (isinstance(result, dict)
                and "rejected" in str(result.get("error", "")))


class NoArgumentBecomesTwo(GatewayCase):
    def test_the_flag_injection_is_refused(self):
        self.assertTrue(self.rejected(self.run_with(SEPARATORS[0])),
                        "a scan was redirected to the filesystem root")

    def test_every_separator_is_refused_at_the_gateway_too(self):
        """Not only by `safe_arg` in isolation: the gateway has to hand it the
        whole value, which is the half that was broken."""
        leaked = [v for v in SEPARATORS if not self.rejected(self.run_with(v))]
        self.assertEqual(leaked, [], f"reached the command line: {leaked!r}")

    def test_the_rejection_is_audited(self):
        import io
        import json

        path = os.path.join(self.data, "state", "audit.jsonl")
        before = 0
        if os.path.exists(path):
            with io.open(path, encoding="utf-8") as fh:
                before = sum(1 for line in fh if line.strip())
        self.run_with(SEPARATORS[0])
        with io.open(path, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()][before:]
        self.assertIn("rejected_arg", [r["kind"] for r in rows])

    def test_a_plain_path_still_reaches_execution(self):
        """A gateway that refuses everything is not secure, it is broken."""
        result = self.run_with("some/path")
        self.assertFalse(self.rejected(result), result)
        self.assertEqual(len(self.commands), 1)

    def test_a_list_argument_is_still_the_way_to_pass_several(self):
        result = self.gateway._exec(self.cap, {"scan_root": ["tests", "docs"]})
        self.assertFalse(self.rejected(result), result)
        self.assertEqual(len(self.commands), 1)

    def test_a_list_element_is_validated_like_a_scalar(self):
        result = self.gateway._exec(self.cap,
                                    {"scan_root": ["tests", "a\x00b"]})
        self.assertTrue(self.rejected(result),
                        "the list path skipped the check")


class TheAssembledCommandKeepsTheTemplatesShape(GatewayCase):
    """Counted in argv tokens, because that is the unit that was leaking."""

    def assemble(self, value):
        """The command `_exec` would run. `GatewayCase` already intercepts."""
        self.run_with(value)
        return self.commands[-1] if self.commands else None

    def test_a_plain_argument_produces_the_templates_own_token_count(self):
        cmd = self.assemble("some/path")
        self.assertIsNotNone(cmd, "the command was never assembled")
        tail = cmd.split("dobby.cli", 1)[1]
        self.assertEqual(shlex.split(tail),
                         ["init", "--scan", "some/path", "--overwrite"])

    def test_a_spacey_path_is_one_token_not_two(self):
        tail = self.assemble("my project").split("dobby.cli", 1)[1]
        self.assertEqual(shlex.split(tail),
                         ["init", "--scan", "my project", "--overwrite"])

    def test_the_separator_never_reaches_assembly_at_all(self):
        self.assertIsNone(self.assemble(SEPARATORS[0]),
                          "a command was built from a rejected argument")


if __name__ == "__main__":
    unittest.main()
