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

---

## Findings from the 2026-07-26 adversarial audit

Three controls were tested by attacking them rather than by re-reading them.
All three had gaps; all three are fixed and held by `tests/test_security_hardening.py`.

### 1. Argument injection through the capability gateway (highest severity)

**Was:** `_exec` interpolated caller arguments after `shlex.quote`. That produces
POSIX single-quoting, and `cmd.exe` does not treat single quotes as quoting —
it passes them through literally. The kit runs `shell=True`, which on Windows is
`cmd.exe`. Measured directly: `echo 'x && echo INJECTED'` printed `INJECTED`.

The gateway's stated premise is that the model never composes raw shell. On
Windows it effectively could, and the destructive-command guard was not a
backstop for it — `&& curl <host>` matches no protected path.

**Now:** arguments are **validated, not escaped**. `security.safe_arg` refuses
any argument containing a shell metacharacter, before quoting, with the
rejection recorded in the audit log. Escaping was rejected as the fix because
its correctness depends on which shell is downstream, and the gateway does not
control that.

The metacharacter set deliberately excludes `\`, `(`, and `)`: a first version
included them and rejected every Windows absolute path and every
`C:\Program Files (x86)\...`, which would have made the control something users
switch off. Their shell danger (`$(...)`, subshells, escaping) requires `$` or a
backtick, both of which are refused.

### 2. The destructive-command guard did not know Windows

**Was:** `DESTRUCTIVE` held POSIX verbs only. On the platform the kit most often
runs on, `del .env`, `erase .env`, `rd /s /q .git`, and
`Remove-Item -Recurse -Force .git` were all permitted. Protected-path patterns
were written with `/`, so `rm -rf .git\hooks` matched nothing. And `git`'s own
destructive subcommands — `git clean -fdx`, `git reset --hard`,
`git checkout -- .` — contain no destructive token at all and passed unexamined,
despite destroying work that is by definition not yet recoverable from anywhere.

Measured before the fix: **9 of 19** destructive commands permitted.

**Now:** cmd.exe and PowerShell verbs are known, matching is case-insensitive,
path arguments are normalized to `/` before comparison, and a second pass runs
over the raw command because `shlex` in POSIX mode turns `.git\hooks` into
`.githooks`. Destructive *subcommands* are blocked on sight, with flags matched
case-sensitively so `git branch -D` is refused and the routine `git branch -d`
is not.

### 3. Secret redaction missed four credential families

**Was:** Slack tokens, PEM private-key headers, Google API keys, and GitLab PATs
survived `redact_secrets`, which runs on provider output before it reaches a
ledger, on API request bodies before transmission, and on sandbox previews.

**Now:** twelve additional shapes, including AWS temporary keys, GitHub
fine-grained PATs, npm and Stripe tokens, and JWTs. False positives are checked
in both directions — prose containing "token" or "password" is not redacted.

### Standing limits, unchanged

- The sandbox's network control remains **best-effort discouragement, not a
  block**. `Result.network_blocked` reports `False` for exactly this reason.
- The guard is a defence against accidents and mis-specified commands, not
  against an adversary with arbitrary code execution. Once a capability runs, it
  runs with the harness's own privileges.
- Argument validation protects the gateway's own command construction. A host
  that registers a capability template containing shell syntax has composed that
  shell itself, and no validation here can un-compose it.
