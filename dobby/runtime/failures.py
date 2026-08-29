"""Failure classification — the input to re-planning, not a counter.

`retry_count` is the wrong primitive and it is the one almost every harness
reaches for. It answers "how many times has this broken" and never "is trying
again the thing that could work". Those come apart immediately:

- A provider 429 wants the SAME provider again, after a wait.
- A schema violation wants a DIFFERENT approach — resending the identical
  prompt to the identical model is the one action guaranteed not to help.
- A failing test wants a repair step that reads the failure, not a retry.
- A missing approval wants a human, and must not consume attempts while it
  waits.
- A malformed task definition wants to stop and say so.

So every failure is classified before anything decides what to do about it, and
the class — never the count — selects the action. The count only bounds it.

The classes are deliberately few. A taxonomy with thirty leaves is one nobody
can classify into consistently, and an inconsistent classification is worse than
a coarse one because the policy table then fires at random.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: What went wrong, in the only granularity the scheduler acts on.
TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
CAPACITY = "CAPACITY"
CONTRACT_VIOLATION = "CONTRACT_VIOLATION"
QUALITY_FAILURE = "QUALITY_FAILURE"
POLICY_BLOCKED = "POLICY_BLOCKED"
NON_RETRYABLE = "NON_RETRYABLE"
#: The node declared a side effect the provider was not permitted to perform.
#: Distinct from POLICY_BLOCKED, which is a human withholding approval: this is
#: the harness having granted less than the work required, and it is the harness
#: that has to change.
PERMISSION_DENIED = "PERMISSION_DENIED"
#: The node declared a side effect, the call reported success, and the effect did
#: not happen. Its own class because it is not a contract violation — the OUTPUT
#: may be perfectly well-shaped — and not a quality failure, because no
#: acceptance check has run yet. It is a worker reporting a result it had no
#: basis for.
EFFECT_NOT_OBSERVED = "EFFECT_NOT_OBSERVED"

FAILURE_CLASSES = (TRANSIENT_PROVIDER, CAPACITY, CONTRACT_VIOLATION,
                   QUALITY_FAILURE, POLICY_BLOCKED, NON_RETRYABLE,
                   PERMISSION_DENIED, EFFECT_NOT_OBSERVED)

#: What to do about it. `WAIT` is not a failure action in the retry sense — it
#: parks the node without consuming an attempt, because a node waiting for a
#: human has not failed at anything.
RETRY_SAME = "RETRY_SAME"
RETRY_ELSEWHERE = "RETRY_ELSEWHERE"
REPAIR = "REPAIR"
WAIT = "WAIT"
FAIL = "FAIL"

ACTIONS = (RETRY_SAME, RETRY_ELSEWHERE, REPAIR, WAIT, FAIL)


@dataclass(frozen=True)
class RetryRule:
    """Policy for one failure class.

    `max_attempts` counts attempts of the NODE, not of a provider, because the
    thing being bounded is how much of the run's budget one node may consume.

    `backoff_s` is the base of an exponential with jitter applied by the caller.
    Zero means "immediately"; a repair step has nothing to wait for.
    """

    action: str
    max_attempts: int
    backoff_s: float = 0.0
    #: Whether the next attempt must avoid the provider that just failed.
    avoid_last_provider: bool = False
    #: Human-readable reason, surfaced in the run report so a stuck run explains
    #: itself without anybody reading this file.
    rationale: str = ""


#: The policy table. Editing this changes runtime behaviour and nothing else —
#: which is the point of classifying first and deciding second.
DEFAULT_POLICY: dict[str, RetryRule] = {
    TRANSIENT_PROVIDER: RetryRule(
        RETRY_SAME, max_attempts=3, backoff_s=2.0,
        rationale="the provider failed in a way that is usually not about this "
                  "prompt; the same call after a wait is the cheapest fix"),
    CAPACITY: RetryRule(
        RETRY_ELSEWHERE, max_attempts=3, backoff_s=5.0, avoid_last_provider=True,
        rationale="rate limits do not clear by asking harder; move the work to "
                  "another provider or wait out the window"),
    CONTRACT_VIOLATION: RetryRule(
        REPAIR, max_attempts=2, backoff_s=0.0, avoid_last_provider=True,
        rationale="the output did not satisfy its declared shape; resending the "
                  "identical prompt to the identical model cannot fix that"),
    QUALITY_FAILURE: RetryRule(
        REPAIR, max_attempts=2, backoff_s=0.0,
        rationale="an acceptance check said no and said why; the failure text is "
                  "the input to the repair, not a reason to start over"),
    POLICY_BLOCKED: RetryRule(
        WAIT, max_attempts=1, backoff_s=0.0,
        rationale="a human has not approved this; waiting is correct and costs "
                  "no attempts"),
    NON_RETRYABLE: RetryRule(
        FAIL, max_attempts=1, backoff_s=0.0,
        rationale="nothing about repeating this changes its outcome"),
    PERMISSION_DENIED: RetryRule(
        FAIL, max_attempts=1, backoff_s=0.0,
        rationale="the same call with the same grant is refused the same way; "
                  "this is a configuration to change, not a call to repeat"),
    EFFECT_NOT_OBSERVED: RetryRule(
        REPAIR, max_attempts=2, backoff_s=0.0,
        rationale="the provider answered and changed nothing; the repair states "
                  "that plainly, which resending the identical prompt does not"),
}


@dataclass
class Failure:
    """A classified failure, with the evidence that produced the class."""

    failure_class: str
    detail: str
    #: Free-form, machine-readable specifics — exit code, provider id, the
    #: acceptance check that failed. Kept out of `detail` so a report can group
    #: by them without parsing prose.
    evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.failure_class not in FAILURE_CLASSES:
            raise ValueError(
                f"unknown failure class {self.failure_class!r}; "
                f"expected one of {FAILURE_CLASSES}")

    def rule(self, policy: dict | None = None) -> RetryRule:
        return (policy or DEFAULT_POLICY)[self.failure_class]

    def to_dict(self) -> dict:
        return {"class": self.failure_class, "detail": self.detail,
                "evidence": self.evidence}


#: Substrings that identify a transient provider fault in the error text these
#: CLIs actually emit. Matched case-insensitively against the whole message.
#:
#: This is pattern matching on prose, which is fragile, and it is fragile in the
#: safe direction: an unmatched transient failure is classified NON_RETRYABLE and
#: the run stops with the provider's own words in the report. The opposite
#: default — treat anything unrecognised as retryable — burns the budget on a
#: permanently broken call and reports "3 attempts failed" instead of the reason.
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "temporarily unavailable", "econnreset", "etimedout", "socket hang up",
    "502", "503", "504", "bad gateway", "service unavailable",
    "gateway timeout", "internal server error",
)

_CAPACITY_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests", "quota",
    "overloaded", "capacity", "usage limit", "throttl",
)

#: A provider that refuses because a permission was never granted is blocked by
#: policy, not broken. `agy` in headless mode auto-denies tools it cannot prompt
#: for, and reports exactly that on stderr — see providers/run.py, which already
#: preserves the message this reads.
_POLICY_MARKERS = (
    "permission", "auto-denied", "auto denied", "not authorized",
    "unauthorized", "forbidden", "requires approval", "consent",
)

#: Authentication is NOT transient. A retry loop against an expired login is the
#: classic way to spend a budget on nothing.
_AUTH_MARKERS = ("not logged in", "login required", "authentication failed",
                 "invalid api key", "no api key", "expired token", "401", "403")


#: HTTP status -> class, when a status is available. Read BEFORE the prose,
#: because a status code is a fact and an error string is a description of one.
#:
#: Measured by injecting failures into `providers/api.call_api` and classifying
#: what came back. Before this existed:
#:
#:     429 rate limit    CAPACITY            RETRY_ELSEWHERE   correct
#:     500 server error  NON_RETRYABLE       FAIL              WRONG
#:     DNS failure       NON_RETRYABLE       FAIL              WRONG
#:     connection refused NON_RETRYABLE      FAIL              WRONG
#:     timeout           TRANSIENT_PROVIDER  RETRY_SAME        correct
#:
#: The prose markers list "502", "503", "504" and "internal server error", and
#: a bare 500 with a body saying `{"error":"internal"}` matches none of them; a
#: `URLError` whose reason stringifies to "refused" misses "connection refused"
#: by two words. Both are transient conditions that were ending runs, and the
#: status that would have said so was already sitting in `result.meta`.
def _from_status(status: int) -> "Failure | None":
    if status == 408 or 500 <= status <= 599:
        return Failure(TRANSIENT_PROVIDER,
                       f"HTTP {status} from the provider, which is the "
                       f"provider's side and not this request's",
                       {"status": status})
    if status == 429:
        return Failure(CAPACITY, f"HTTP {status}: rate limited",
                       {"status": status})
    if status in (401, 407):
        return Failure(NON_RETRYABLE,
                       f"HTTP {status}: authentication failed — retrying "
                       f"cannot log anyone in", {"status": status})
    if status == 403:
        return Failure(POLICY_BLOCKED,
                       f"HTTP {status}: the provider refused for a permission "
                       f"it could not obtain without a human",
                       {"status": status})
    if 400 <= status <= 499:
        return Failure(NON_RETRYABLE,
                       f"HTTP {status}: the request was refused, and the same "
                       f"request will be refused again", {"status": status})
    return None


#: Reasons a connection never produced a status at all. Retried, because the
#: common cause is a blip and the policy bounds it at three attempts -- and
#: because a run dying on a DNS hiccup is the failure this was found by.
_UNREACHABLE_MARKERS = (
    "network error reaching", "getaddrinfo", "name or service not known",
    "temporary failure in name resolution", "refused", "unreachable",
    "no route to host", "connection aborted", "ssl", "handshake",
)


def classify_provider_error(error: str, *, exit_code: int | None = None,
                            empty_output: bool = False,
                            status: int | None = None) -> Failure:
    """Turn a provider's failure into a class the scheduler can act on.

    `status` is read first when there is one. It is the structured fact; the
    error string is somebody's description of it, and the two disagreed -- see
    `_from_status`.

    `empty_output` is separated from the error text because exit-0-with-no-stdout
    is a distinct condition: the process succeeded and produced nothing, which is
    a broken contract rather than a broken provider.
    """
    text = (error or "").lower()
    evidence = {"exit_code": exit_code, "error": (error or "")[:400],
                "status": status}

    if empty_output:
        return Failure(CONTRACT_VIOLATION,
                       "the provider exited successfully and produced no output; "
                       "an empty answer must not be treated as a considered one",
                       evidence)
    if status is not None:
        decided = _from_status(int(status))
        if decided is not None:
            return Failure(decided.failure_class, decided.detail, evidence)

    if any(m in text for m in _AUTH_MARKERS):
        return Failure(NON_RETRYABLE,
                       "authentication failed — retrying cannot log anyone in",
                       evidence)
    if any(m in text for m in _POLICY_MARKERS):
        return Failure(POLICY_BLOCKED,
                       "the provider refused for a permission it could not "
                       "obtain without a human", evidence)
    if any(m in text for m in _CAPACITY_MARKERS):
        return Failure(CAPACITY, "provider capacity or rate limit", evidence)
    if any(m in text for m in _TRANSIENT_MARKERS):
        return Failure(TRANSIENT_PROVIDER, "transient provider fault", evidence)
    if any(m in text for m in _UNREACHABLE_MARKERS):
        return Failure(TRANSIENT_PROVIDER,
                       "the provider could not be reached, which is a "
                       "condition of the network rather than of this request",
                       evidence)
    if not text:
        return Failure(NON_RETRYABLE,
                       "the provider failed without saying why", evidence)
    return Failure(NON_RETRYABLE,
                   f"unrecognised provider failure, treated as permanent so the "
                   f"budget is not spent on a guess: {error[:200]}", evidence)


def classify_verifier_failure(failed_requirements: list[str],
                              *, schema_error: str | None = None) -> Failure:
    """A verifier's `no` is either a shape problem or a quality problem.

    The distinction decides who fixes it. A shape problem is the worker's output
    format, and a format-repair step with the schema in hand fixes it. A quality
    problem is the CONTENT — a failing test, an unresolved citation — and the
    repair needs the failure text, not the schema.
    """
    if schema_error:
        return Failure(CONTRACT_VIOLATION, schema_error,
                       {"failed": list(failed_requirements)})
    return Failure(QUALITY_FAILURE,
                   "; ".join(failed_requirements) or "acceptance checks failed",
                   {"failed": list(failed_requirements)})


#: Exit codes a shell reports when it could not run the command at all. These
#: are task-definition errors — a typo'd command — and no amount of retrying
#: makes the binary appear.
_SHELL_CANNOT_RUN = {126, 127}


def classify_command_failure(exit_code: int, stderr: str,
                             *, timed_out: bool = False) -> Failure:
    """Classify a deterministic command (a test run, a linter, a build)."""
    evidence = {"exit_code": exit_code, "stderr": (stderr or "")[:400]}
    if timed_out:
        return Failure(TRANSIENT_PROVIDER, "the command exceeded its wall clock",
                       evidence)
    if exit_code in _SHELL_CANNOT_RUN:
        return Failure(NON_RETRYABLE,
                       f"the shell could not run the command (exit {exit_code}); "
                       f"this is a task definition error, not a flaky step",
                       evidence)
    return Failure(QUALITY_FAILURE,
                   f"command exited {exit_code}", evidence)


def backoff_delay(rule: RetryRule, attempt: int, *,
                  jitter: float = 0.0) -> float:
    """Exponential backoff for `attempt` (1-based), with caller-supplied jitter.

    Jitter is a PARAMETER rather than a `random()` call so a test can assert the
    delay exactly. Randomness that cannot be turned off makes the retry path the
    one part of the runtime nobody writes a test for.
    """
    if rule.backoff_s <= 0:
        return 0.0
    return round(rule.backoff_s * (2 ** max(0, attempt - 1)) + jitter, 3)


_WS = re.compile(r"\s+")


def summarize(failures: list[Failure]) -> dict:
    """Counts by class, for a run report that says what kind of trouble it had."""
    counts: dict[str, int] = {}
    for f in failures:
        counts[f.failure_class] = counts.get(f.failure_class, 0) + 1
    return {"total": len(failures), "by_class": counts,
            "last": _WS.sub(" ", failures[-1].detail).strip() if failures else ""}
