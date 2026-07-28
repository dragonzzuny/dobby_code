# QWEN.md

**Read `AGENTS.md` in this directory — it is the complete operating contract.**
This file is only the Qwen adapter.

Qwen Code reads `QWEN.md`; it does not read `AGENTS.md` or `CLAUDE.md` on its own.
Without this file the contract is present in the repository and invisible to you,
which is worse than absent — the rules exist, and nothing enforces them.

Qwen-specific notes:

- Start every task with `python -m dobby.cli context "<task>"` and treat the
  returned pack as your briefing. In an installed project `.\dobby` (Windows) and
  `./dobby.sh` (POSIX) do the same thing.
- The invariants agents violate most: (2) numbers only from same-session
  commands, (4) never modify or delete what you did not create, (7) validate
  OUTPUTS — a producing command exiting 0 is not validation.
- Skills in `.claude/skills/` are ordered checklists and are **not Claude-only**.
  The directory is named for where the format originated; every step names the
  command that verifies it, and those commands run here. Read
  `.claude/skills/dobby/SKILL.md` first — it is the entry protocol for a
  free-text request.
- **`qwen` has never been executed by this harness.** Its catalog entry was
  written from documentation and its `verified_on` is empty, which is recorded in
  `docs/RESEARCH_EVIDENCE_MATRIX.md` §10. If you are reading this, you are the
  first run: expect the launch flags in `dobby/providers/catalog.py` to be
  unproven, and report what actually happened rather than assuming the catalog
  was right.
- You have no `web` capability in the catalog. `dobby research run` will refuse
  to use you for search on purpose — answering a search question from memory
  produces output indistinguishable from a real retrieval, which is the one
  failure that module exists to prevent.
- You are one provider among several. When you need a second opinion, get it from
  a *different* provider — your own second pass is correlated with your first.
