# Weak-model failure catalog (generic)

The failure modes this kit exists to prevent, each with its concrete defense.
During bootstrap, extend this catalog with the host project's specific
instances (real incidents beat generic taxonomy — see author-evals skill).

| failure mode | definition | kit defense |
|---|---|---|
| One-Shot Collapse | implementing everything in one pass, no checkpoints; any error sinks the whole attempt | P-DECOMPOSE + ledger; incremental-clean-state rule (ledgered-task §4) |
| Premature Completion | "done" while a required verification is missing/failing | P-VALIDATE-OUTPUT; evaluator verdict required; completion protocol |
| Documentation-as-Truth | trusting docs/config/comments over the running system | P-CONFIG-VS-REALITY; P-EVIDENCE ("docs are snapshots") |
| Context Flooding | loading whole files/tool catalogs up front | context packs (summaries, budgeted); 3-level skill disclosure; 4-meta-tool MCP |
| Tool Roulette | guessing tools/commands instead of discovering them | capability registry + search_capabilities; KG Tool nodes |
| Unverified Patch | change never exercised end-to-end | validation ladder; slice runner; quality gates authored per artifact |
| Spec Drift | solving a mutated version of the request | ledger holds requirements VERBATIM; self-review re-read before reporting |
| Scope Creep | "while I was at it" changes | P-MINIMAL; findings-not-fixes rule |
| Orchestrator Overkill | multi-agent ceremony where a script suffices | agency ladder; levels 6-7 human-gated (`allow_multi_agent`) |
| Echo-Chamber Review | generator grading its own work | evaluator isolation: sha256-pinned criteria; proposer≠approver on skills |
| Reflection Without Learning | lessons written but never applied or validated | improvement loop: candidates must pass measured gates or land in negative memory |
| Skill Pollution | promoting a procedure from one lucky example | lifecycle evidence floors (≥2 distinct scenarios) + non-self approval |
| Evaluation Gaming | editing criteria/gold/holdout after seeing failures | FORBIDDEN_TARGETS in improve.py; criteria hash pinning; holdout never optimized |
| Stale-Memory Override | newer unverified assertion displacing verified fact | memory authority rule (verification rank gates supersession) |
| False Consensus | majority agent opinion treated as evidence | only command/test outputs count as evidence (P-EVIDENCE); evaluator records carry the observation |
| Handoff Amnesia | restarting from scratch after context loss | trajectory + structured handoff + resume protocol (newest ledger first) |
| First-Match Target | acting on the first path/entity whose name matches the request | P-DECOMPOSE requires exact-path resolution; ambiguity with different outcomes = L3 |
| Silent Assumption | proceeding on an unstated interpretation | L1 assumptions must be recorded in ledger + report with a verification route |
| Infinite Investigation | exploring past the point where a decision changes | hypothesis-ledger stop rule; 3-dead-checks re-derivation |
| Non-Reproducible Experiment | ad-hoc numbers nobody can regenerate | load-bearing numbers from repeatable commands recorded in reports |
