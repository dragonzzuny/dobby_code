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

### 4. The command guard permitted erasing the machine

**Was:** `guard_command` blocked a destructive command only when one of its
arguments matched a **configurable** pattern. `DEFAULT_PROTECTED` covers `.git`,
`.pem`, `.key` and `.env` — repository integrity and secrets — and a host that
sets `protected_paths` replaces that list wholesale. Nothing protected the machine
itself. Measured:

```
rm -rf /                  ALLOW
rm -rf ~                  ALLOW
rd /s /q C:/Users         ALLOW
rm -rf C:/Windows         ALLOW
rd /s /q C:/Program Files ALLOW
```

The `C:\`-with-a-trailing-backslash spellings *were* refused, but only because
that backslash makes `shlex.split` raise and the unparseable branch is
conservative. Written `C:/` the same command passed. Protection that depends on
which slash the caller typed is luck, not a control.

Found by writing a test that asserted a `rm -rf /` score command would be
blocked. It was not blocked — it **executed**, and only GNU `rm`'s own
`--no-preserve-root` failsafe prevented the outcome.

**Now:** machine-level targets are refused in a separate check that the
configurable list cannot switch off — filesystem and drive roots, `~`, the
resolved home directory, `$HOME`/`%USERPROFILE%`, and the standard system
directories. Matches are exact, so `rm -rf ./build`, `rm -rf ~/project/dist` and
`rm -rf /home/runner/work/x/tmp` all still run: a guard that blocks routine
cleanup gets switched off and then protects nothing.

Two bugs in the first version of that fix, both found by probing it rather than
reading it: `rstrip("/")` turned `"/"` into `""` and fell through the empty-string
guard, so the single case the check existed for still passed; and a path
containing a space evaded it entirely, because `shlex` splits `C:/Program Files`
into two tokens and neither matches. The second is closed by a boundary-anchored
scan over the whole command.

### 5. `--score-command` runs model output, and the sandbox is not in that path

**Status: open, and stated rather than mitigated.**

`dobby search` scores each candidate by running a command against the file the
candidate was written to. For most real objectives that command *executes
model-generated code* — a test suite, a build, an interpreter.

`dobby/sandbox.py` exists for exactly this shape of risk and **is not wired into
this path**. `command_scorer` runs the command through `guard_command` with
`shell=True`, which is a defence against a mis-specified command, not against
code the harness itself just asked a model to write.

What this means concretely: a score command is as privileged as the harness. The
candidate file lands under `.dobby/state/search/`, which is gitignored and
excluded by both installers, so model output does not *travel*; nothing stops it
*running* if the score command runs it.

Anyone wiring a real objective should assume that. The demonstration run recorded
in the evidence matrix deliberately scores **static** properties — `compile()`
parses without executing, and the feature checks are regex and AST — precisely so
that the first live search did not have to take this risk. Routing the scorer
through the sandbox is the fix and has not been done.

### Standing limits, unchanged

- The sandbox's network control remains **best-effort discouragement, not a
  block**. `Result.network_blocked` reports `False` for exactly this reason.
- The guard is a defence against accidents and mis-specified commands, not
  against an adversary with arbitrary code execution. Once a capability runs, it
  runs with the harness's own privileges.
- Argument validation protects the gateway's own command construction. A host
  that registers a capability template containing shell syntax has composed that
  shell itself, and no validation here can un-compose it.
