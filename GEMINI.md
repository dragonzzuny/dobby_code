# GEMINI.md

**Read `AGENTS.md` in this directory — it is the complete operating contract.**
This file is only the Gemini adapter.

Gemini CLI reads `GEMINI.md`; it does not read `AGENTS.md` or `CLAUDE.md` on its
own. Without this file the contract is present in the repository and invisible to
you, which is worse than absent — the rules exist, and nothing enforces them.

Gemini-specific notes:

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
- You have `web` capability, which `dobby fleet` records and most providers here
  do not have. `dobby research run "<need>" --yes` will pick you or `claude` for
  that reason. When you search, report what you RETRIEVED; a citation you did not
  open is worse than none, because nothing downstream can tell the two apart.
- You are one provider among several. When you need a second opinion, get it from
  a *different* provider — your own second pass is correlated with your first, and
  reporting it as independent corroboration is the failure
  `dobby/swarm/diversity.py` exists to catch.
