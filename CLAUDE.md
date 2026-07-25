# CLAUDE.md

**Read `AGENTS.md` in this directory — it is the complete operating contract.**
This file is only the Claude adapter.

Claude-specific notes:

- Start every task with `python -m dobby.cli context "<task>"` and treat the
  returned pack as your briefing.
- The invariants new agents violate most: (2) numbers only from same-session
  commands, (4) never modify or delete what you did not create, (7) validate
  OUTPUTS — a producing command exiting 0 is not validation.
- Skills in `.claude/skills/` are ordered checklists. Follow them step by step;
  each step names its verification command.
- You are one provider among several. `dobby fleet` shows the others. When you
  need a second opinion, get it from a *different* provider — your own second
  pass is correlated with your first, and reporting it as independent
  corroboration is the failure `dobby/swarm/diversity.py` exists to catch.
- Optional MCP gateway:
  `claude mcp add dobby -- python mcp/dobby_mcp_server.py --repo .`
  Call `get_context_pack` first, then reach tools via `search_capabilities` →
  `get_capability` → `invoke_capability`.
