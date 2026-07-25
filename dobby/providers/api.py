"""OpenAI-compatible API transport for `api`-kind providers.

This module is the ONLY place in the engine that opens a network connection, and
it is written to make that fact hard to forget.

Why it is gated rather than merely optional
-------------------------------------------
The MCP gateway ships with no network tool at all. That is not an oversight: it
structurally removes the exfiltration leg of the "lethal trifecta" (untrusted
input + privileged access + a way out). Everything else in the kit can therefore
process attacker-influenced text — an issue body, a scraped page, a dependency's
README — knowing that the worst outcome is a wrong *local* action.

This module reintroduces that leg. A prompt assembled from repository content and
sent to a third-party endpoint is an egress path, and the payload is usually the
most valuable text in the session. So:

- Every call requires `allow_network=True` passed explicitly. There is no config
  default that silently enables it and no environment variable that flips it on.
- `synthesize` and `adjudicate` never route here (enforced in `catalog.py`),
  because those roles see the whole aggregated context.
- The request body is redacted before transmission, not after. Redacting a
  response is useless — the secret has already left the machine.
- Every call is auditable: the caller gets back the byte count actually sent, so
  a ledger can record what left rather than what was intended to leave.

Standard library only
---------------------
`urllib.request`, not `requests` or an SDK. The kit's dependency contract is
"Python plus PyYAML", and adding an HTTP library — or a vendor SDK that pulls a
dozen transitive dependencies — to reach an optional, off-by-default feature is
a bad trade. The OpenAI-compatible chat-completions shape is stable enough to
hand-roll, and hand-rolling keeps the exact bytes visible in this file where they
can be reviewed.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import time
import urllib.error
import urllib.request

from ..core.security import cap_output, redact_secrets
from .base import ProviderResult, ProviderSpec
from .catalog import registry

#: Endpoints per provider id. Kept here rather than in the catalog because a base
#: URL is transport detail, and the catalog is about capability and cost.
#:
#: NOT VERIFIED on the authoring machine: no key was present, so no request was
#: ever issued. These URLs come from each vendor's published OpenAI-compatible
#: documentation and are marked accordingly in `verified_on` (empty) upstream.
BASE_URLS: dict[str, str] = {
    "kimi": "https://api.moonshot.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}

#: Default model per provider, used when the caller does not name one.
DEFAULT_MODELS: dict[str, str] = {
    "kimi": "moonshot-v1-32k",
    "dashscope": "qwen-plus",
}

#: Hard ceiling on a single request body. A prompt larger than this is almost
#: always an accident — a whole file tree pasted in — and shipping it to a third
#: party is both expensive and the worst case for exfiltration.
MAX_REQUEST_BYTES = 512_000


class NetworkNotAllowed(RuntimeError):
    """Raised when an api provider is invoked without an explicit opt-in.

    An exception rather than a failed `ProviderResult`, deliberately. Every other
    provider failure is ordinary and belongs in a fan-out's failure list; this one
    means the caller tried to open a network path it had not been granted, which
    is a programming error that must stop rather than degrade.
    """


@dataclasses.dataclass
class ApiCallRecord:
    """What actually left the machine. For the audit log."""

    provider: str
    base_url: str
    model: str
    request_bytes: int
    response_bytes: int
    duration_s: float
    status: int | None
    error: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _endpoint(pid: str) -> str:
    base = os.environ.get(f"DOBBY_{pid.upper()}_BASE_URL") or BASE_URLS.get(pid)
    if not base:
        raise ValueError(
            f"no base URL known for api provider {pid!r}; set "
            f"DOBBY_{pid.upper()}_BASE_URL")
    return base.rstrip("/") + "/chat/completions"


def _api_key(spec: ProviderSpec) -> str:
    for var in spec.required_env:
        value = os.environ.get(var)
        if value:
            return value
    raise NetworkNotAllowed(
        f"{spec.id}: none of {list(spec.required_env)} is set. The key is read "
        "from the environment and is never written to disk, a config file, or "
        "an audit record")


def call_api(pid: str, prompt: str, *, allow_network: bool,
             model: str | None = None,
             system: str | None = None,
             timeout_s: int | None = None,
             max_tokens: int = 4096,
             temperature: float = 0.7,
             output_cap: int = 24_000) -> tuple[ProviderResult, ApiCallRecord]:
    """Send one chat-completions request. Returns the result and an audit record.

    `allow_network` is a required keyword with no default. A caller must state
    the intent at the call site; a defaulted parameter would let network egress
    be enabled by a refactor that nobody reviewed as a security change.
    """
    spec = registry().get(pid)
    if spec.kind != "api":
        raise ValueError(f"{pid} is not an api provider; use run_provider")
    if not allow_network:
        raise NetworkNotAllowed(
            f"{pid} requires allow_network=True. Enabling it adds a network "
            "egress path that the MCP gateway deliberately does not have — see "
            "docs/THREAT_MODEL.md before turning it on")

    key = _api_key(spec)
    url = _endpoint(pid)
    chosen_model = model or DEFAULT_MODELS.get(pid) or "default"

    messages = []
    if system:
        messages.append({"role": "system", "content": redact_secrets(system)})
    # Redact BEFORE sending. Redacting a response would be theatre: by then the
    # secret has already crossed the network boundary.
    messages.append({"role": "user", "content": redact_secrets(prompt)})

    payload = json.dumps({
        "model": chosen_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")

    if len(payload) > MAX_REQUEST_BYTES:
        record = ApiCallRecord(provider=pid, base_url=url, model=chosen_model,
                               request_bytes=len(payload), response_bytes=0,
                               duration_s=0.0, status=None,
                               error="request too large; not sent")
        return ProviderResult(
            provider=pid, ok=False,
            error=(f"request body is {len(payload)} bytes, over the "
                   f"{MAX_REQUEST_BYTES} ceiling. Nothing was sent. A prompt "
                   "this large is usually an accident, and shipping it to a "
                   "third party is the worst case for both cost and disclosure"),
            meta={"request_bytes": len(payload)}), record

    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Identify the client honestly; several gateways log and rate-limit
            # on this and an absent UA is treated as a scraper.
            "User-Agent": "dobby-harness/0.1",
        })

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request,
                                    timeout=timeout_s or spec.timeout_s) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        duration = round(time.monotonic() - started, 2)
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
        except Exception:  # noqa: BLE001
            pass
        record = ApiCallRecord(pid, url, chosen_model, len(payload), 0,
                               duration, exc.code, f"HTTP {exc.code}")
        return ProviderResult(
            provider=pid, ok=False, duration_s=duration,
            # The response body can echo the request, so redact it too.
            error=f"HTTP {exc.code}: {redact_secrets(detail)}",
            meta={"status": exc.code}), record
    except urllib.error.URLError as exc:
        duration = round(time.monotonic() - started, 2)
        record = ApiCallRecord(pid, url, chosen_model, len(payload), 0,
                               duration, None, str(exc.reason))
        return ProviderResult(
            provider=pid, ok=False, duration_s=duration,
            error=f"network error reaching {url}: {exc.reason}"), record
    except socket.timeout:
        duration = round(time.monotonic() - started, 2)
        record = ApiCallRecord(pid, url, chosen_model, len(payload), 0,
                               duration, None, "timeout")
        return ProviderResult(
            provider=pid, ok=False, duration_s=duration,
            error=f"timeout after {timeout_s or spec.timeout_s}s"), record

    duration = round(time.monotonic() - started, 2)
    record = ApiCallRecord(pid, url, chosen_model, len(payload), len(body),
                           duration, status)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ProviderResult(
            provider=pid, ok=False, duration_s=duration,
            error=f"response was not JSON: {cap_output(body, 400)}",
            meta={"status": status}), record

    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        # An OpenAI-compatible endpoint that does not return this shape is a
        # configuration problem, and guessing at an alternative field would make
        # the failure silent on the next vendor.
        return ProviderResult(
            provider=pid, ok=False, duration_s=duration,
            error=("response did not match the OpenAI chat-completions shape "
                   f"(no choices[0].message.content): {cap_output(body, 400)}"),
            meta={"status": status}), record

    safe = redact_secrets(text)
    capped = cap_output(safe, output_cap)
    usage = data.get("usage") or {}
    return ProviderResult(
        provider=pid, ok=True, text=capped, exit_code=0,
        duration_s=duration, truncated=len(safe) > len(capped),
        meta={
            "status": status,
            "model": data.get("model", chosen_model),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "request_bytes": len(payload),
            "response_bytes": len(body),
            "egress": True,
        }), record


def audit_line(record: ApiCallRecord) -> str:
    """One-line audit entry. Records what LEFT, not what was intended to leave."""
    return (f"{time.strftime('%Y-%m-%dT%H:%M:%S')} egress "
            f"provider={record.provider} model={record.model} "
            f"sent={record.request_bytes}B received={record.response_bytes}B "
            f"status={record.status} {record.duration_s}s"
            + (f" error={record.error}" if record.error else ""))
