# Threat model — harness kit (generic)

Assets: the host project's crown jewels (config `protected_paths`), artifact
integrity, evaluation integrity (gold/criteria/holdout), user trust in reports,
the host machine.

| # | threat | vector | mitigation (implemented) | residual risk |
|---|---|---|---|---|
| T1 | destruction of protected content | destructive command targeting crown jewels | `guard_command` + config `protected_paths` (generic defaults: .git, *.pem, *.key, .env); enforced in evaluator + MCP gateway; tested | direct shell outside the gateway is NOT guarded — pair with the agent product's own permission system/hooks |
| T2 | prompt injection via data | file/tool output contains instructions | all gateway output wrapped in untrusted-data envelope (tested); no network/email capabilities exist → exfiltration leg structurally absent | envelope depends on the client rendering it as data |
| T3 | tool poisoning | malicious capability smuggled into the registry | registry is a local, human-edited allowlist; exec = fixed shell-quoted templates (model never composes raw shell); unknown ids refused (tested); audit log | registry file writable by anyone with repo access; no signing yet |
| T4 | evaluation gaming | loop edits gold/criteria/holdout | FORBIDDEN_TARGETS path check (tested); criteria sha256-pinned per evaluation (tested); holdout excluded from search fitness (tested) | manual human edits bypass the loop — changelog discipline is the control |
| T5 | skill pollution | self-promoted / single-example skills | evidence floors + proposer≠approver (tested); revision resets evidence | approver identity is a string, not authenticated |
| T6 | memory poisoning | unverified "fact" displacing verified one | authority rule blocks lower-verification supersession (tested); verified-first recall | content labeled `verified` fraudulently needs provenance discipline |
| T7 | secret leakage | tool output echoes credentials | redact_secrets on all evaluator/MCP output (tested) | regex-based; novel formats pass |
| T8 | resource exhaustion | runaway loops, giant outputs | BudgetMeter hard stops (tested); 600s timeouts; 20k-char caps; single-candidate bounded improvement | no global cross-invocation budget |
| T9 | excessive agency | default multi-agent orchestration | ladder 6-7 requires `allow_multi_agent` opt-in (tested) | — |
| T10 | false completion | "done" without evidence | evaluator verdict required; model-judgment gaps reported NOT RUN; P-REPORT always fires | a non-compliant model can still write prose lies — host-side review + evals are the backstop |

Recommended host-side hardening (outside the kit): PreToolUse-style hooks
denying destructive commands on protected paths at the agent-product level;
read-only allowlists for the registered capabilities; version-pinning/signing
of the capability registry; authenticated approver identities.
