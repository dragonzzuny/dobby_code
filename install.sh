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

# Preconditions are MEASURED, not assumed (invariant 3) — and existence is not
# a measurement.
#
# On Windows, `python3` usually resolves to the Microsoft Store App Installer
# redirector: a stub that prints the word "Python", executes nothing, and is
# found by `command -v` like any real binary. An installer that trusts name
# resolution therefore selects a non-functional interpreter and refuses to
# install on a machine with a perfectly good Python 3.11 — observed on the
# authoring machine, where this script failed at the front door.
#
# So each candidate is asked to COMPUTE something. A stub can echo its own name;
# it cannot return 311.
PY=""
for cand in python3 python py python3.13 python3.12 python3.11 python3.10; do
  command -v "$cand" >/dev/null 2>&1 || continue
  ver="$("$cand" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || true)"
  case "$ver" in
    ''|*[!0-9]*) continue ;;          # no answer, or not a number: not a Python
  esac
  [ "$ver" -ge 310 ] 2>/dev/null || continue
  PY="$cand"
  break
done

if [ -z "$PY" ]; then
  say "no working Python 3.10+ found. Candidates tried: python3 python py"
  say "Names that resolve but do not execute (e.g. the Windows Store stub at"
  say "  ~/AppData/Local/Microsoft/WindowsApps/python3) are skipped on purpose."
  die "install a real Python 3.10+ and re-run"
fi

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
# Runtime state that must NEVER travel to a host project. These are the same
# paths `.gitignore` excludes, and `tests/test_install.py` asserts the two lists
# agree — because they drift silently otherwise.
#
# This matters more than it looks. Installing from a working tree (the documented
# path: clone, then run this script) copies whatever the source repo has
# accumulated. Before this exclusion existed, a real install carried the source
# machine's audit log, session trajectories, and — worst — `state/sandbox/*`,
# which holds the captured stdout of arbitrary commands. Sandbox captures are
# precisely the content that must not move between machines.
RUNTIME_STATE="state knowledge/kg.bootstrap.json inventory.json memory \
compression_guideline.json specialization.json"

copy_data_excluding_state() {
  src="$1"; dst="$2"
  run mkdir -p "$dst"
  (cd "$src" && find . -type f | sed 's|^\./||') | while read -r rel; do
    skip=""
    for pat in $RUNTIME_STATE; do
      case "$rel" in
        "$pat"|"$pat"/*) skip=1; break ;;
      esac
    done
    [ -n "$skip" ] && continue
    if [ "$DRY" = "--dry" ]; then
      say "  would copy: $rel"
    else
      mkdir -p "$dst/$(dirname "$rel")"
      cp "$src/$rel" "$dst/$rel"
    fi
  done
}

say ""
say "project data (preserved if it already exists; runtime state never copied):"
for dir in .dobby evals; do
  if [ -e "$TARGET/$dir" ]; then
    say "  $dir/ EXISTS — left untouched (your curated knowledge)"
  elif [ "$dir" = ".dobby" ]; then
    say "  $dir/ created from the distribution defaults (state/ excluded)"
    copy_data_excluding_state "$SRC/$dir" "$TARGET/$dir"
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

# ---- launchers, so `dobby doctor` works instead of `python -m dobby.cli` ---
#
# Both forms are written because a host is used from more than one shell, and
# both hard-code the interpreter this installer PROBED. Writing `python3` would
# reintroduce the defect the probe above exists to avoid: on Windows that name
# resolves to a Store redirector stub that executes nothing.
#
# The POSIX form is `dobby.sh`, NOT `dobby`. The installer copies the engine
# package to `<host>/dobby/`, so a file named `dobby` beside it is the same name in
# the same directory - the first attempt failed with "Is a directory" on the first
# run. On Windows there is no collision: `dobby.cmd` differs from the directory,
# and cmd.exe resolves a bare `dobby` through PATHEXT.
#
# The .cmd carries one limit, measured rather than assumed: cmd.exe truncates an
# argument at its first NEWLINE, so `dobby panel "line one<newline>line two"`
# arrives as "line one". Single-line arguments, percent signs, quotes and
# ampersands all survive. For a multi-line prompt use `python -m dobby.cli`
# directly, which is a real executable and carries anything.
say ""
say "launchers:"
if [ "$DRY" != "--dry" ]; then
  printf '@echo off\r\nsetlocal\r\n"%s" -m dobby.cli %%*\r\n' "$PY" \
    > "$TARGET/dobby.cmd"
  printf '#!/bin/sh\n# Generated by install.sh. Interpreter probed, not guessed.\nexec "%s" -m dobby.cli "$@"\n' \
    "$PY" > "$TARGET/dobby.sh"
  chmod +x "$TARGET/dobby.sh" 2>/dev/null || true
fi
say "  dobby.cmd  (cmd.exe / PowerShell: dobby doctor  |  .\\dobby doctor)"
say "  dobby.sh   (sh / bash / zsh: ./dobby.sh doctor)"
say "             named .sh because <host>/dobby/ is the engine package"
say "  note: a .cmd truncates an argument at its first newline; for a"
say "        multi-line prompt use python -m dobby.cli"

say ""
if [ "$DRY" = "--dry" ]; then
  say "dry run complete; nothing was written."
  exit 0
fi

# ---- verify the install, do not assume it (invariant 7) ------------------
say "verifying:"
cd "$TARGET"
# Engine health, not the whole suite. Running 600 tests as an install step is
# disproportionate to the question being asked — "did the engine land and does
# it import and run here?" — and it made every install wait half a minute. These
# six modules are the load-bearing core; a failure in any of them means the
# install is not usable, and the full suite remains one command away.
ENGINE_TESTS="tests.test_kg tests.test_router tests.test_skills tests.test_evaluator tests.test_security tests.test_memory tests.test_trajectory"
if "$PY" -m unittest $ENGINE_TESTS -q >/dev/null 2>&1; then
  say "  PASS engine health (core modules)"
  say "       full suite: cd $TARGET && $PY -m unittest discover -s tests"
else
  say "  FAIL engine health — run: $PY -m unittest $ENGINE_TESTS"
fi
if [ -f ".dobby/knowledge/kg.bootstrap.json" ]; then
  # Already instantiated. This is the normal state on an UPGRADE, and treating
  # it as a failure taught the user to ignore the installer's own verdict.
  say "  PASS bootstrap (already instantiated; refresh with: "
  say "       $PY -m dobby.cli init --scan . --overwrite)"
elif "$PY" -m dobby.cli init --scan . >/dev/null 2>&1; then
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
