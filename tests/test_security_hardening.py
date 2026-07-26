"""Adversarial tests for the security layer.

The guard and the redactor both passed their original tests while missing whole
categories. Found by asking "what would a real destructive command on THIS
platform look like" rather than by re-reading the code:

- 9 of 19 destructive commands were permitted, including every Windows delete
  verb (`del`, `erase`, `rd`, `Remove-Item`) and every git subcommand that
  discards uncommitted work (`git clean -fdx`, `git reset --hard`,
  `git checkout -- .`). The kit runs `shell=True`, which is `cmd.exe` on
  Windows, so a POSIX-only guard is not a guard on its most common platform.
- 4 of 16 credential shapes survived redaction: Slack tokens, PEM private-key
  headers, Google API keys, GitLab PATs.

Both lists are held here as data so a future edit that narrows them fails
loudly.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.security import (DESTRUCTIVE, DESTRUCTIVE_SUBCOMMANDS,
                                 SHELL_METACHARACTERS, safe_arg,
                                 cap_output, envelope_untrusted, guard_command,
                                 redact_secrets)

#: (command, must_be_blocked)
GUARD_CASES = [
    # POSIX
    ("rm -rf .env", True),
    ("rm -rf .git", True),
    ("rm -rf .git/hooks", True),
    ("shred -u secrets.pem", True),
    ("truncate -s 0 .env", True),
    # Windows separators — the guard's patterns are written with "/"
    (r"rm -rf .git\hooks", True),
    (r"rm -rf src\.env", True),
    (r"del C:\proj\.env", True),
    # cmd.exe delete verbs
    ("del .env", True),
    ("erase .env", True),
    ("rd /s /q .git", True),
    ("rmdir /s /q .git", True),
    ("DEL .env", True),                       # case-insensitive
    # PowerShell
    ("Remove-Item -Recurse -Force .git", True),
    ("Remove-Item .env", True),
    ("ri .env", True),
    # git subcommands that destroy uncommitted work
    ("git clean -fdx", True),
    ("git reset --hard HEAD~5", True),
    ("git checkout -- .", True),
    ("git restore .", True),
    ("git push --force origin main", True),
    ("git branch -D feature", True),
    ("git worktree remove wt", True),
    # compound commands
    ("make build && git clean -fdx", True),
    ("echo hi && rm .env", True),
    ("true; rm -rf .git", True),
    # must stay allowed
    ("python -m pytest", False),
    ("python -m dobby.cli doctor", False),
    ("rm build.cache", False),
    ("rm -rf node_modules", False),
    ("git status", False),
    ("git reset HEAD~1", False),
    ("git checkout main", False),
    ("git push origin main", False),
    ("git branch -d merged", False),          # -d is the SAFE form
    ("mv src/a.py src/b.py", False),
    ("npm run build", False),
]

MUST_REDACT = [
    "api_key=sk-abc1234567890123456",
    "API_KEY: sk-abcdefghijklmnop1234",
    "password: hunter2",
    "Authorization: Bearer eyJhbGciOi",
    "sk-abcdefghijklmnop1234",
    "sk-ant-api03-abcdefghijklmnop",
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnopqrst1234",
    "gho_abcdefghijklmnopqrst1234",
    "github_pat_11ABCDEFG0abcdefghijklmno",
    "glpat-abcdefghijklmnopqrst",
    "xoxb-123456789012-abcdefghijkl",
    "AIzaSyAbcdefghijklmnopqrstuvwxyz12345",
    "npm_abcdefghijklmnopqrstuvwxyz0123456789",
    "sk_live_abcdefghijklmnop1234",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
]

MUST_KEEP = [
    "the token bucket algorithm",
    "password reset flow",
    "skip this line entirely",
    "BEGIN PUBLIC KEY",
    "AIza",
]


class TestCommandGuard(unittest.TestCase):
    def test_every_case(self):
        failures = []
        for command, should_block in GUARD_CASES:
            allowed, reason = guard_command(command)
            if (not allowed) != should_block:
                failures.append(
                    f"{command!r}: expected "
                    f"{'BLOCK' if should_block else 'allow'}, got "
                    f"{'BLOCK' if not allowed else 'allow'} ({reason})")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_windows_delete_verbs_are_known(self):
        """The kit runs shell=True, which is cmd.exe on Windows."""
        for verb in ("del", "erase", "rd", "remove-item"):
            self.assertIn(verb, DESTRUCTIVE)

    def test_git_subcommands_are_covered(self):
        bases = {(b, s) for b, s, _ in DESTRUCTIVE_SUBCOMMANDS}
        for pair in (("git", "clean"), ("git", "reset"), ("git", "checkout")):
            self.assertIn(pair, bases)

    def test_destructive_subcommand_blocks_without_a_path_argument(self):
        """`git clean -fdx` has no target to compare against a pattern."""
        allowed, reason = guard_command("git clean -fdx")
        self.assertFalse(allowed)
        self.assertIn("uncommitted", reason)

    def test_flag_case_distinguishes_safe_from_destructive(self):
        self.assertFalse(guard_command("git branch -D x")[0])
        self.assertTrue(guard_command("git branch -d x")[0])

    def test_raw_rescan_catches_tokenizer_mangling(self):
        """shlex turns `.git\\hooks` into `.githooks` in POSIX mode."""
        allowed, reason = guard_command(r"rm -rf .git\hooks")
        self.assertFalse(allowed)
        self.assertIn("protected path", reason)

    def test_unparseable_with_destructive_token_is_blocked(self):
        allowed, _ = guard_command('rm -rf "unterminated')
        self.assertFalse(allowed)

    def test_unparseable_without_destructive_token_is_allowed(self):
        self.assertTrue(guard_command('echo "unterminated')[0])

    def test_host_protected_paths_are_honoured(self):
        allowed, reason = guard_command("rm -rf secrets/prod.yaml",
                                        [r".*secrets/.*"])
        self.assertFalse(allowed)

    def test_allow_suffixes_still_deletable(self):
        self.assertTrue(guard_command("rm .git.cache")[0])

    def test_empty_command(self):
        self.assertTrue(guard_command("")[0])


class TestRedaction(unittest.TestCase):
    def test_every_credential_shape_is_redacted(self):
        missed = [s for s in MUST_REDACT if redact_secrets(s) == s]
        self.assertEqual(missed, [], f"credentials survived redaction: {missed}")

    def test_prose_is_not_redacted(self):
        false_positives = [s for s in MUST_KEEP if redact_secrets(s) != s]
        self.assertEqual(false_positives, [],
                         f"prose was redacted: {false_positives}")

    def test_redaction_is_applied_inside_larger_text(self):
        text = ("Traceback:\n  File x.py\n  header "
                "Authorization: Bearer sk-abcdefghijklmnop1234\ndone")
        out = redact_secrets(text)
        self.assertNotIn("sk-abcdefghijklmnop1234", out)
        self.assertIn("Traceback", out)

    def test_pem_header_marks_the_block(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n"
        self.assertNotEqual(redact_secrets(block), block)


#: Payloads that must never reach a shell. Each was verified to execute under
#: `cmd.exe` when only `shlex.quote` protected it: POSIX single-quoting is not
#: quoting on Windows, so `echo 'x && echo INJECTED'` printed INJECTED.
INJECTIONS = [
    "x && whoami",
    "x | more",
    "x; echo pwned",
    "`id`",
    "$(id)",
    "%PATH%",
    "!DELAYED!",
    "x\necho pwned",
    "x > /etc/passwd",
    "x < /etc/shadow",
    "*.py",
    'a"b',
    "a'b",
    "{a,b}",
    "x[0]",
]

#: Arguments a real capability receives. Rejecting these makes the gateway
#: unusable, which is how a security control gets switched off.
LEGITIMATE_ARGS = [
    r"C:\Users\dynap\proj",
    r"C:\Program Files (x86)\Tool",
    r"src\main.py",
    "src/main.py",
    ".", "..", "tests",
    "my project",
    "v1.2.3",
    "feature/branch-name",
    "--overwrite",
    "2026-07-26",
]


class TestArgumentValidation(unittest.TestCase):
    """Quoting is not enough; arguments are refused, not escaped."""

    def test_every_injection_is_refused(self):
        leaked = [p for p in INJECTIONS if safe_arg(p)[0]]
        self.assertEqual(leaked, [], f"injection payloads accepted: {leaked}")

    def test_every_legitimate_argument_is_accepted(self):
        rejected = [(a, safe_arg(a)[1]) for a in LEGITIMATE_ARGS
                    if not safe_arg(a)[0]]
        self.assertEqual(rejected, [],
                         f"legitimate arguments rejected: {rejected}")

    def test_windows_paths_are_not_collateral_damage(self):
        """A first version rejected every backslash and broke Windows entirely."""
        self.assertTrue(safe_arg(r"C:\Users\x\project")[0])
        self.assertNotIn("\\", SHELL_METACHARACTERS)

    def test_program_files_parentheses_allowed(self):
        self.assertTrue(safe_arg(r"C:\Program Files (x86)")[0])
        self.assertNotIn("(", SHELL_METACHARACTERS)

    def test_command_substitution_still_blocked_without_parens_in_the_set(self):
        """`$` and backtick are refused, so a bare paren cannot start one."""
        self.assertFalse(safe_arg("$(id)")[0])
        self.assertFalse(safe_arg("`id`")[0])

    def test_oversized_argument_refused(self):
        ok, why = safe_arg("a" * 5000)
        self.assertFalse(ok)
        self.assertIn("4096", why)

    def test_empty_argument_allowed(self):
        self.assertTrue(safe_arg("")[0])

    def test_reason_explains_why_escaping_is_not_used(self):
        _, why = safe_arg("x && y")
        self.assertIn("cmd.exe", why)


class TestOutputCaps(unittest.TestCase):
    def test_cap_output_bounds_length(self):
        self.assertLessEqual(len(cap_output("x" * 10_000, 500)), 600)

    def test_cap_output_leaves_short_text(self):
        self.assertEqual(cap_output("short", 500), "short")

    def test_untrusted_envelope_marks_its_content(self):
        env = envelope_untrusted("some tool output", source="exec:test")
        self.assertTrue(env.get("untrusted"),
                        "tool output must be marked as data, not instructions")


if __name__ == "__main__":
    unittest.main()


class TestMachineLevelTargetsAreRefusedRegardless(unittest.TestCase):
    """`rm -rf /` passed the guard, and a test that ran it is how that surfaced.

    `DEFAULT_PROTECTED` covers `.git`, `.pem`, `.key` and `.env` — the repository's
    integrity and its secrets — and it is CONFIGURABLE: a host that sets
    `protected_paths` replaces it wholesale. Measured before this check existed:

        rm -rf /                  ALLOW
        rm -rf ~                  ALLOW
        rd /s /q C:/Users         ALLOW
        rm -rf C:/Windows         ALLOW

    The `C:/`-with-a-trailing-backslash spellings were refused, but only because
    that backslash makes `shlex.split` raise and the unparseable branch is
    conservative. Written `C:/` the same command passed. Protection that depends on
    which slash was typed is luck.

    This guard is the only thing between a data-defined command — from
    `capabilities.json`, `criteria/*.json`, `slice_plans.json`, or a
    `--score-command` — and `shell=True`, so these targets are refused separately
    from the configurable list and cannot be switched off by a config file.
    """

    CATASTROPHIC = [
        "rm -rf /", "rm -rf /*", "rm -rf //",
        "rm -rf / candidate.txt",
        "rm -rf ~", "rm -rf ~/", "rm -rf $HOME", "rm -rf ${HOME}",
        "rm -rf /etc", "rm -rf /usr", "rm -rf /bin", "rm -rf /var",
        "rm -rf /home", "rm -rf /root",
        "rm -rf C:/", "rm -rf c:", "rm -rf D:/*",
        "rm -rf C:/Windows", "rm -rf C:/Windows/System32",
        "rd /s /q C:/Users", "rd /s /q C:/Program Files",
        "del /f /s /q %USERPROFILE%",
        "Remove-Item -Recurse -Force C:/ProgramData",
    ]

    ORDINARY = [
        "rm -rf ./build", "rm -rf build", "rm -f candidate.txt",
        "rm -rf ./node_modules", "rm -rf dist/*",
        "rm -rf ~/project/dist", "rm -rf /home/runner/work/x/tmp",
        "rm -rf C:/Users/someone/project/build",
        "python -m pytest", "git status",
    ]

    def test_every_machine_level_target_is_refused(self):
        allowed = [c for c in self.CATASTROPHIC if guard_command(c)[0]]
        self.assertEqual(allowed, [], f"these would run: {allowed}")

    def test_the_refusal_says_a_config_cannot_relax_it(self):
        _, why = guard_command("rm -rf /")
        self.assertIn("regardless of protected_paths", why)

    def test_an_empty_protected_list_does_not_disable_it(self):
        """The whole point: a host replacing protected_paths keeps this."""
        allowed, why = guard_command("rm -rf /", [])
        self.assertFalse(allowed, why)

    def test_a_custom_protected_list_does_not_disable_it(self):
        allowed, why = guard_command("rm -rf ~", [r".*/.secret$"])
        self.assertFalse(allowed, why)

    def test_ordinary_cleanup_is_still_allowed(self):
        """A guard that blocks routine work gets disabled and protects nothing."""
        blocked = [(c, guard_command(c)[1]) for c in self.ORDINARY
                   if not guard_command(c)[0]]
        self.assertEqual(blocked, [], f"these were wrongly refused: {blocked}")

    def test_case_and_slash_do_not_change_the_answer(self):
        for spelling in ("rm -rf C:/WINDOWS", "rm -rf c:/windows",
                         "rm -rf C:/Windows/", "rm -rf 'C:/Windows'"):
            self.assertFalse(guard_command(spelling)[0], spelling)

    def test_the_resolved_home_directory_is_covered_not_just_the_tilde(self):
        """`/home/runner` on a runner is `~`; the guard must see through that."""
        import os
        home = os.path.expanduser("~")
        if not home or home in ("/", ""):
            self.skipTest("no resolvable home directory here")
        self.assertFalse(guard_command(f"rm -rf {home}")[0], home)
        # A child of home is ordinary work.
        self.assertTrue(guard_command(f"rm -rf {home}/project/build")[0])

    def test_a_non_destructive_command_naming_root_is_untouched(self):
        """Only destructive commands are examined; `ls /` is not the guard's business."""
        self.assertTrue(guard_command("ls /")[0])
        self.assertTrue(guard_command("du -sh /home")[0])
