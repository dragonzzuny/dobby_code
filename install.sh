#!/usr/bin/env bash
# Install dobby into a host project. Idempotent: re-running is safe and is the
# supported upgrade path.
#
# Usage:
#   ./install.sh /path/to/host-project        install or upgrade
#   ./install.sh /path/to/host-project --dry  show what would change
#
# What it does, and what it deliberately does NOT do:
#
#   ENGINE (dobby/, mcp/, tests/) is COPIED and overwritten on upgrade. It is
#   repo-agnostic code that carries no project knowledge, so replacing it is safe.
#
#   DATA (.dobby/, evals/) is copied ONLY IF ABSENT. This is the project's curated
#   knowledge: its knowledge graph, protected paths, policies, and gold labels.
#   Overwriting it on upgrade would silently destroy every session of curation,
#   which is the single most damaging thing an installer of this kind can do.
#
#   ENTRY POINTS (AGENTS.md, CLAUDE.md, DESIGN.md) are never overwritten. A host
#   project's own contract wins; the installer appends a pointer instead.
#
# Exit codes: 0 ok, 1 usage/precondition failure.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
DRY="${2:-}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
say() { printf '%s\n' "$1"; }
run() { if [ "$DRY" = "--dry" ]; then say "  would: $*"; else "$@"; fi; }

[ -n "$TARGET" ] || die "usage: ./install.sh /path/to/host-project [--dry]"
[ -d "$TARGET" ] || die "target is not a directory: $TARGET"
TARGET="$(cd "$TARGET" && pwd)"
[ "$TARGET" != "$SRC" ] || die "target is the dobby repo itself; pick a host project"

# Preconditions verified against the system, not assumed (invariant 3).
PY=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "no python interpreter on PATH (need 3.10+)"

"$PY" - <<'PYCHECK' || die "python 3.10+ required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PYCHECK

if ! "$PY" -c "import yaml" >/dev/null 2>&1; then
  say "warning: PyYAML is not importable with $PY."
  say "         dobby optimize / improve-auto need it. Install with:"
  say "           $PY -m pip install PyYAML"
fi

say "dobby -> $TARGET"
say ""

# ---- engine: copy and overwrite -------------------------------------------
say "engine (overwritten on upgrade):"
for dir in dobby mcp tests; do
  say "  $dir/"
  run rm -rf "$TARGET/$dir"
  run cp -R "$SRC/$dir" "$TARGET/$dir"
done

# ---- data: copy only if absent -------------------------------------------
say ""
say "project data (preserved if it already exists):"
for dir in .dobby evals; do
  if [ -e "$TARGET/$dir" ]; then
    say "  $dir/ EXISTS — left untouched (your curated knowledge)"
  else
    say "  $dir/ created from the distribution defaults"
    run cp -R "$SRC/$dir" "$TARGET/$dir"
  fi
done

# Rules and skills: merge per-file so a host's own additions survive.
say ""
say "rules and skills (per-file, existing files kept):"
run mkdir -p "$TARGET/.claude/rules" "$TARGET/.claude/skills" "$TARGET/reports" "$TARGET/docs"
for f in "$SRC"/.claude/rules/*.md; do
  base="$(basename "$f")"
  if [ -e "$TARGET/.claude/rules/$base" ]; then
    say "  rules/$base exists — kept"
  else
    run cp "$f" "$TARGET/.claude/rules/$base"
  fi
done
for d in "$SRC"/.claude/skills/*/; do
  base="$(basename "$d")"
  if [ -e "$TARGET/.claude/skills/$base" ]; then
    say "  skills/$base exists — kept"
  else
    run cp -R "$d" "$TARGET/.claude/skills/$base"
  fi
done
for f in "$SRC"/docs/*.md; do
  base="$(basename "$f")"
  [ -e "$TARGET/docs/$base" ] && say "  docs/$base exists — kept" && continue
  run cp "$f" "$TARGET/docs/$base"
done

# ---- entry points: append a pointer, never overwrite ---------------------
say ""
say "entry points:"
POINTER="Agent harness: read AGENTS.md in this repository before any task (dobby)."
for f in AGENTS.md CLAUDE.md; do
  if [ ! -e "$TARGET/$f" ]; then
    say "  $f created"
    run cp "$SRC/$f" "$TARGET/$f"
  elif grep -q "dobby" "$TARGET/$f" 2>/dev/null; then
    say "  $f already references dobby — unchanged"
  else
    say "  $f exists — appending a one-line pointer (your contract wins)"
    if [ "$DRY" != "--dry" ]; then
      printf '\n%s\n' "$POINTER" >> "$TARGET/$f"
    fi
  fi
done
if [ ! -e "$TARGET/DESIGN.md" ]; then
  say "  DESIGN.md created (edit the tokens for your product)"
  run cp "$SRC/DESIGN.md" "$TARGET/DESIGN.md"
else
  say "  DESIGN.md exists — kept"
fi

say ""
if [ "$DRY" = "--dry" ]; then
  say "dry run complete; nothing was written."
  exit 0
fi

# ---- verify the install, do not assume it (invariant 7) ------------------
say "verifying:"
cd "$TARGET"
if "$PY" -m unittest discover -s tests -q >/dev/null 2>&1; then
  say "  PASS engine tests"
else
  say "  FAIL engine tests — run: $PY -m unittest discover -s tests"
fi
if "$PY" -m dobby.cli init --scan . >/dev/null 2>&1; then
  say "  PASS bootstrap scan"
else
  say "  FAIL bootstrap scan — run: $PY -m dobby.cli init --scan ."
fi

say ""
say "next:"
say "  cd $TARGET"
say "  $PY -m dobby.cli doctor      # what works here, and what does not"
say "  $PY -m dobby.cli context \"your first task\""
say ""
say "then curate .dobby/knowledge/kg.json and .dobby/config.json protected_paths"
say "(see .claude/skills/bootstrap-project/SKILL.md)."
