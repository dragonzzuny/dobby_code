"""The header said the body is what leaks, and then let the body through.

`mcp/dobby_mcp_server.py` claims "output size caps + secret redaction" on
every capability result. `redact_secrets` is that promise. Four shapes walked
past it, each measured against the function as it stood:

    -----BEGIN RSA PRIVATE KEY-----      header replaced, every base64 line
    MIIEowIBAAKCAQEA...                  of the key intact. The pattern's own
    -----END RSA PRIVATE KEY-----        comment read "the body is what
                                         leaks".

    {"password": "hunter2"}              unchanged. The rule wanted
                                         `password` immediately before the
                                         delimiter; JSON puts a quote there.

    AWS_SECRET_ACCESS_KEY=wJalrX...      unchanged. `secret` is not the last
                                         token before `=`, and `_` is a word
                                         character so `\\b` never matched.

    postgres://user:hunter2@host/db      unchanged. No rule for URL
    https://user:hunter2@github.com      credentials at all.

And one partial, which reads as a pass and is not: `Authorization: Bearer
<token>` redacted the word `Bearer` and left the token, because the rule stops
at the first whitespace.

Every one of these is a shape that turns up in ordinary command output --
`env`, a connection string in a stack trace, a JSON config dump, a key file
cat by mistake -- and `_exec` pipes command output straight into the model's
context.

The widening is only as wide as the evidence. `password reset flow` and `the
token bucket algorithm` still pass through untouched; a delimiter is what
separates an assignment from prose, and the corpus below is the guard against
paying for coverage with false positives that eat real output.
"""

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.security import envelope_untrusted, redact_secrets  # noqa: E402

PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEowIBAAKCAQEA7f3ab991c2QmJvZHkgb2YgdGhlIGtleQ==\n"
       "c2Vjb25kIGxpbmUgb2YgdGhlIGtleSBib2R5\n"
       "-----END RSA PRIVATE KEY-----")

#: (text, the substring that must not survive)
LEAKS = [
    (PEM, "MIIEowIBAAKCAQEA"),
    (PEM.replace("-----END RSA PRIVATE KEY-----", ""), "MIIEowIBAAKCAQEA"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n", "b3BlbnNzaC1rZXk"),
    ('{"password": "hunter2"}', "hunter2"),
    ("{'api_key': 'sk-shortone'}", "sk-shortone"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY", "wJalrXUtnFEMI"),
    ("DATABASE_PASSWORD_VALUE=hunter2", "hunter2"),
    ("MY_API_KEY_HERE=abc123xyz", "abc123xyz"),
    ("postgres://user:hunter2@host:5432/db", "hunter2"),
    ("https://user:hunter2@github.com/x/y.git", "hunter2"),
    ("redis://default:hunter2@cache:6379", "hunter2"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9zzzz", "eyJhbGciOiJIUzI1NiJ9zzzz"),
    ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
]

#: Output that must survive intact. Coverage bought with false positives eats
#: the tracebacks and test summaries this gateway exists to carry.
INNOCENT = [
    "the token bucket algorithm",
    "password reset flow",
    "skip this line entirely",
    "BEGIN PUBLIC KEY",
    "AIza",
    "Ran 2322 tests in 12.3s",
    "time: 14:32:07",
    'File "x.py", line 42:',
    "http://localhost:8080/health",
    "note: see docs/SECURITY.md",
    "key: value",
    "Traceback (most recent call last):",
    "-----BEGIN CERTIFICATE-----",
    # This project's own telemetry. The first widening matched `token`
    # mid-word and ate all of it -- `tests/test_usage_survives_cap` failed
    # twice, because a redacted usage line is a call whose cost was thrown
    # away. Counted in the source: `output_tokens` 36 times, `input_tokens`
    # 26, `thinking_tokens` 23, and eleven more field names besides.
    '{"total_tokens": 1234}',
    '{"input_tokens":10,"output_tokens":20}',
    '{"usage":{"input_tokens":100,"output_tokens":200}}',
    '"thinking_tokens": 9',
    '"verdict_token": "x"',
    "tokens: 4096",
    "token_count: 12",
    "billable_tokens=100",
    "cache_read_input_tokens: 5",
    "approx_tokens: 3",
    # An LLM token and a parser token are not credentials.
    "tokenizer: fast",
]


class NothingInThatListSurvives(unittest.TestCase):
    def test_no_credential_reaches_the_output(self):
        leaked = [(text[:40], secret) for text, secret in LEAKS
                  if secret in redact_secrets(text)]
        self.assertEqual(leaked, [], f"survived redaction: {leaked!r}")

    def test_the_key_body_goes_with_the_header(self):
        """The specific claim the old comment made and did not keep."""
        out = redact_secrets(PEM)
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertNotIn("c2Vjb25kIGxpbmU", out)

    def test_an_unterminated_key_is_still_a_key(self):
        """Truncated key material is key material. Output caps truncate."""
        out = redact_secrets(PEM.split("-----END")[0])
        self.assertNotIn("MIIEowIBAAKCAQEA", out)

    def test_text_after_the_block_is_kept(self):
        """The greedy unterminated rule must not eat the whole transcript when
        the block is properly closed."""
        out = redact_secrets(PEM + "\nRan 3 tests in 0.1s\nOK")
        self.assertIn("Ran 3 tests", out)
        self.assertIn("OK", out)

    def test_the_bearer_token_goes_not_just_the_word_bearer(self):
        out = redact_secrets("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9zzzz")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9zzzz", out)

    def test_a_url_keeps_its_host_so_the_reader_can_still_debug(self):
        out = redact_secrets("postgres://user:hunter2@host:5432/db")
        self.assertNotIn("hunter2", out)
        self.assertIn("host:5432/db", out)
        self.assertIn("postgres://", out)


class NoInnocentOutputIsEaten(unittest.TestCase):
    def test_ordinary_text_passes_through_unchanged(self):
        changed = [(s, redact_secrets(s)) for s in INNOCENT
                   if redact_secrets(s) != s]
        self.assertEqual(changed, [], f"redacted innocent output: {changed!r}")

    def test_prose_about_passwords_is_still_prose(self):
        """A delimiter is what makes it an assignment."""
        text = "the password reset flow sends a token to the user"
        self.assertEqual(redact_secrets(text), text)

    def test_a_public_key_header_is_not_a_private_key(self):
        block = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZI\n"
        self.assertEqual(redact_secrets(block), block)

    def test_a_usage_record_survives_intact(self):
        """The regression that caught the first widening, kept as a case.

        A redacted usage line is a call whose cost was thrown away, and this
        repository's whole cost accounting reads these fields.
        """
        line = ('{"type":"usage","input_tokens":1200,"output_tokens":340,'
                '"cache_read_tokens":0,"thinking_tokens":88}')
        self.assertEqual(redact_secrets(line), line)

    def test_a_real_token_assignment_is_still_redacted(self):
        """Sparing the count nouns must not spare the credential."""
        out = redact_secrets("token=ghp_abcdefghijklmnopqrst1234")
        self.assertNotIn("ghp_abcdefghijklmnopqrst1234", out)


class TheWideningIsNotQuadratic(unittest.TestCase):
    """The first fix for the gaps above was a ReDoS.

    `[a-z0-9_-]*` in front of an alternation makes the engine try every start
    position against every prefix length. `redact_secrets` runs on untrusted
    command output BEFORE `cap_output` trims it, so the input size is the
    attacker's choice. Measured on a single long line:

        1000 chars   0.105s
        5000 chars   1.823s
       20000 chars  32.632s

    Replaced by two patterns and a fixed-width lookbehind. The assertion is a
    RATIO rather than a wall-clock threshold: quadratic growth is the defect,
    and a slower machine multiplies both sides of a ratio equally.
    """

    #: Long enough that quadratic behaviour is unmistakable, short enough that
    #: the test does not take 30 seconds if it ever regresses.
    SMALL, LARGE = 2000, 20000

    @staticmethod
    def elapsed(text):
        import time

        start = time.perf_counter()
        redact_secrets(text)
        return time.perf_counter() - start

    def test_ten_times_the_input_is_not_a_hundred_times_the_work(self):
        small = max(self.elapsed("a" * self.SMALL), 1e-6)
        large = self.elapsed("a" * self.LARGE)
        self.assertLess(large / small, 25,
                        f"{small:.4f}s -> {large:.4f}s for 10x the input")

    def test_a_long_line_of_word_characters_is_not_pathological(self):
        """The exact shape that took 32 seconds: no delimiter anywhere, so
        every position is a failed match that used to be expensive."""
        self.assertLess(self.elapsed("A_B_C_" * 3400), 2.0)

    def test_a_large_output_is_still_redacted_correctly(self):
        """Fast and wrong is not the fix."""
        out = redact_secrets("x" * 20000 + "\nAWS_SECRET_ACCESS_KEY=wJalrXUtn")
        self.assertNotIn("wJalrXUtn", out)


class TheGatewayEnvelopeCarriesIt(unittest.TestCase):
    """Redaction that only the unit test sees is not a defence."""

    def test_command_output_is_redacted_inside_the_envelope(self):
        out = envelope_untrusted(f"scanning...\n{PEM}\ndone",
                                 source="exec:probe")
        self.assertNotIn("MIIEowIBAAKCAQEA", out["content"])
        self.assertIn("scanning", out["content"])
        self.assertTrue(out["untrusted"])

    def test_redaction_happens_before_the_size_cap(self):
        """Capping first could split a secret and leave the front half."""
        secret = "sk-" + "a" * 40
        out = envelope_untrusted("x" * 19990 + f"\n{secret}\n",
                                 source="exec:probe")
        self.assertNotIn(secret, out["content"])


if __name__ == "__main__":
    unittest.main()
