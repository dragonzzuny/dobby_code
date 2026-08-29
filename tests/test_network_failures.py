"""What the scheduler does when the network does what networks do.

Found by injecting failures into `providers/api.call_api` and running what came
back through `classify_provider_error`. Before the fix:

    429 rate limit      CAPACITY            RETRY_ELSEWHERE   correct
    500 server error    NON_RETRYABLE       FAIL              WRONG
    DNS failure         NON_RETRYABLE       FAIL              WRONG
    connection refused  NON_RETRYABLE       FAIL              WRONG
    timeout             TRANSIENT_PROVIDER  RETRY_SAME        correct

Three transient conditions ending a run permanently. The classifier reads text
markers, the list carries "502", "503", "504" and "internal server error", and
a bare 500 with a body of `{"error":"internal"}` matches none of them; a
`URLError` whose reason stringifies to "refused" misses "connection refused" by
two words.

The fix is not a longer marker list. `providers/api` already had the HTTP status
in `result.meta["status"]` and `runtime/workers` dropped it at the one place the
decision is made -- a structured fact sitting beside the prose written about it,
which is the same shape as the token axes that were counted from a description
and the model pin that was asked for and never checked. The status is now read
first, and the markers are the fallback for CLI providers that have no status
to give.

`_UNREACHABLE_MARKERS` is a judgement worth stating: a connection that never
produced a status is treated as TRANSIENT. A DNS name can be permanently wrong
as easily as briefly unresolvable, and this cannot tell them apart -- but the
common cause is a blip, `DEFAULT_POLICY[TRANSIENT_PROVIDER]` bounds it at three
attempts, and a run dying on a DNS hiccup is the failure that led here.
"""

import io
import json
import os
import socket
import sys
import unittest
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.providers import api as A  # noqa: E402
from dobby.runtime.failures import (CAPACITY, DEFAULT_POLICY,  # noqa: E402
                                    NON_RETRYABLE, POLICY_BLOCKED,
                                    TRANSIENT_PROVIDER,
                                    classify_provider_error)

OK_BODY = '{"choices":[{"message":{"content":"ok"}}]}'
SECRET = "sk-SUPERSECRETKEY1234567890"


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))


class Injected(unittest.TestCase):
    """Drives the real `call_api` with `urlopen` replaced."""

    def setUp(self):
        self.previous = os.environ.get("DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = SECRET
        self.addCleanup(self.restore)

    def restore(self):
        if self.previous is None:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        else:
            os.environ["DASHSCOPE_API_KEY"] = self.previous

    def call(self, *, exc=None, status=200, body=OK_BODY):
        def urlopen(request, timeout=None):
            if exc is not None:
                raise exc
            return FakeResponse(status, body)

        original = A.urllib.request.urlopen
        A.urllib.request.urlopen = urlopen
        try:
            return A.call_api("dashscope", "hi", allow_network=True,
                              timeout_s=5)
        finally:
            A.urllib.request.urlopen = original

    def verdict(self, **kwargs):
        result, _record = self.call(**kwargs)
        failure = classify_provider_error(
            result.error or "", exit_code=result.exit_code,
            status=(result.meta or {}).get("status"))
        return failure.failure_class, DEFAULT_POLICY[failure.failure_class].action


class ServerSideFaultsAreRetried(Injected):
    def test_a_bare_500_is_transient(self):
        """The measured miss. `internal server error` is a marker; a body of
        `{"error":"internal"}` behind a 500 is not."""
        self.assertEqual(self.verdict(exc=http_error(500, '{"error":"internal"}')),
                         (TRANSIENT_PROVIDER, "RETRY_SAME"))

    def test_the_whole_5xx_range_is_transient(self):
        for code in (500, 502, 503, 504, 599):
            self.assertEqual(
                self.verdict(exc=http_error(code, "x"))[0],
                TRANSIENT_PROVIDER, code)

    def test_a_408_is_transient_too(self):
        self.assertEqual(self.verdict(exc=http_error(408, "slow"))[0],
                         TRANSIENT_PROVIDER)


class UnreachableIsRetried(Injected):
    def test_a_dns_failure_is_transient(self):
        self.assertEqual(
            self.verdict(exc=urllib.error.URLError("getaddrinfo failed")),
            (TRANSIENT_PROVIDER, "RETRY_SAME"))

    def test_a_refused_connection_is_transient(self):
        """The reason stringifies to "refused", two words short of the marker
        that was supposed to catch it."""
        self.assertEqual(
            self.verdict(exc=urllib.error.URLError(
                ConnectionRefusedError("refused")))[0],
            TRANSIENT_PROVIDER)

    def test_a_socket_timeout_is_transient(self):
        self.assertEqual(self.verdict(exc=socket.timeout())[0],
                         TRANSIENT_PROVIDER)


class TheRestKeepTheirMeaning(Injected):
    def test_429_moves_the_work_elsewhere(self):
        self.assertEqual(self.verdict(exc=http_error(429, "rate limit")),
                         (CAPACITY, "RETRY_ELSEWHERE"))

    def test_401_fails_because_retrying_cannot_log_anyone_in(self):
        self.assertEqual(self.verdict(exc=http_error(401, "invalid api key")),
                         (NON_RETRYABLE, "FAIL"))

    def test_403_waits_for_a_human_rather_than_failing_blind(self):
        self.assertEqual(self.verdict(exc=http_error(403, "forbidden"))[0],
                         POLICY_BLOCKED)

    def test_a_4xx_request_error_is_permanent(self):
        self.assertEqual(self.verdict(exc=http_error(400, "bad request"))[0],
                         NON_RETRYABLE)

    def test_a_response_that_is_not_json_is_permanent(self):
        self.assertEqual(self.verdict(body="not json at all")[0],
                         NON_RETRYABLE)

    def test_a_response_of_the_wrong_shape_is_permanent(self):
        """Guessing at another field would make it silent on the next vendor."""
        self.assertEqual(self.verdict(body='{"unexpected":"shape"}')[0],
                         NON_RETRYABLE)


class TheStatusOutranksTheProse(unittest.TestCase):
    def test_a_status_decides_even_when_the_text_says_otherwise(self):
        """A 503 body that happens to contain "invalid api key" is still a 503.
        The status is the fact; the body is somebody's description."""
        failure = classify_provider_error("HTTP 503: invalid api key",
                                          status=503)
        self.assertEqual(failure.failure_class, TRANSIENT_PROVIDER)

    def test_with_no_status_the_markers_still_decide(self):
        """CLI providers have no status to give, and they are most of them."""
        self.assertEqual(
            classify_provider_error("not logged in").failure_class,
            NON_RETRYABLE)
        self.assertEqual(
            classify_provider_error("rate limit exceeded").failure_class,
            CAPACITY)

    def test_the_status_is_recorded_in_the_evidence(self):
        failure = classify_provider_error("HTTP 500", status=500)
        self.assertEqual(failure.evidence.get("status"), 500)

    def test_an_unknown_status_falls_through_to_the_text(self):
        """A 3xx is not in the table; the prose gets its turn."""
        failure = classify_provider_error("connection reset", status=302)
        self.assertEqual(failure.failure_class, TRANSIENT_PROVIDER)


class TheKeyNeverLeaves(Injected):
    """A redaction that only works on the happy path is not a redaction."""

    def leaks(self, blob):
        return SECRET in str(blob)

    def test_not_through_a_normal_result(self):
        result, record = self.call()
        self.assertFalse(self.leaks(result.text))
        self.assertFalse(self.leaks(json.dumps(record.__dict__, default=str)))
        self.assertFalse(self.leaks(A.audit_line(record)))

    def test_not_through_an_error_body_that_echoes_the_request(self):
        echo = json.dumps({"error": f"bad: Authorization: Bearer {SECRET}"})
        result, _ = self.call(exc=http_error(400, echo))
        self.assertFalse(self.leaks(result.error))
        self.assertIn("REDACTED", result.error)

    def test_not_through_a_response_body_that_echoes_it(self):
        body = json.dumps({"choices": [{"message":
                                        {"content": f"key is {SECRET}"}}]})
        result, _ = self.call(body=body)
        self.assertFalse(self.leaks(result.text))
        self.assertIn("REDACTED", result.text)


if __name__ == "__main__":
    unittest.main()
