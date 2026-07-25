---
name: bootstrap-project
description: First-run repository archaeology — instantiate the harness kit for a host project. Use when .dobby/knowledge/kg.bootstrap.json does not exist, or when the user asks to set up/install/initialize the harness.
---

# bootstrap-project

**Trigger:** first run in a new repository; "set up / install / initialize the
harness"; `.dobby/knowledge/kg.bootstrap.json` absent.
**Non-trigger:** already bootstrapped (refresh instead:
`{python} -m dobby.cli init --scan <root> --overwrite`); ordinary tasks.

Each step names its verification. Do not skip steps; do not start substantive
project work before step 8 passes.

1. **Scan.** From the kit folder: `{python} -m dobby.cli init --scan <host-root>`
   (`--scan ..` when the kit sits inside the host as a subfolder; results land
   in the KIT's own `.dobby/`)
   ✓ prints nodes/edges counts; `.dobby/inventory.json` +
   `.dobby/knowledge/kg.bootstrap.json` exist.
2. **Read the host's own instructions** (README, AGENTS.md/CLAUDE.md,
   CONTRIBUTING, CI configs — the inventory lists them). Record conflicts
   between them as `contradicts` edges, not silent merges.
3. **Curate the knowledge graph** (`.dobby/knowledge/kg.json`): add the
   domain entities the scan cannot infer — datasets/services/components, their
   REAL locations, known defects, risks, conventions. Every node/edge carries
   provenance; facts you measured = `verified`, guesses = `weakly_inferred`.
   ✓ `python3 -c "from harness.kg import *; ..."` not needed — any invalid
   node fails loudly on next `cli context` call; run one.
4. **Protect the crown jewels.** Fill `protected_paths` (regex list) in
   `.dobby/config.json`: originals, data trees, credentials, anything
   without a restore path.
   ✓ `python3 - <<'EOF'` … `from harness.security import guard_command,
   load_protected; import json; cfg=json.load(open('.dobby/config.json'));
   print(guard_command('rm <a-protected-path>', load_protected(cfg)))` → False.
5. **Register capabilities**: the host's build/test/lint/domain-check commands
   as fixed templates in `.dobby/registry/capabilities.json` (least
   privilege: no free-form shell).
   ✓ MCP `search_capabilities` or `cli route` surfaces them.
6. **Author project policies** in `.dobby/policies/policies.json`: keep the
   nine universal ones; add domain policies with trigger keywords in the
   host's language(s). Mirror any prose rule the host already has.
   ✓ `{python} -m dobby.cli route "<a risky domain task>"` fires them.
7. **Wire the entry point**: create or append the HOST ROOT `AGENTS.md` and
   `CLAUDE.md`: "Agent harness: read <kit-path>/AGENTS.md before any task."
   (Append-only if they exist — P-PRESERVE.)
8. **Verify the installation.**
   `{python} -m unittest discover -s tests -q` → OK, and
   `{python} -m dobby.cli slice --scenario SELF-CHECK` → evaluator verdict PASS.
9. **Author evals next** (separate skill: author-evals) — the kit is not
   "measured" for this project until project gold + scenarios exist. Say so in
   your report.

**Failure modes:** scan finds nothing (wrong --repo path); invalid curated node
(missing provenance — fix the node, don't relax the ontology); protected regex
too broad (blocks legitimate output dirs — test with guard_command both ways).
