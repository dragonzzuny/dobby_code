"""The installer is the front door, and it was never executed before this file.

Two defects were found the first time it ran on a real host:

1. It selected the Windows Store `python3` redirector stub — a name that
   resolves like a binary and executes nothing — then refused to install on a
   machine with a working Python 3.11.
2. It copied the source repo's RUNTIME STATE into the host: the audit log,
   session trajectories, and `state/sandbox/*`, which holds the captured stdout
   of arbitrary commands. Those paths are all in `.gitignore` — the repo already
   declared them "not part of the product" and the installer copied them anyway.

Both are the same mistake in different clothes: trusting a declaration instead
of measuring, and keeping two lists in sync by hand.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.platform import bash_path, child_env
from pathlib import Path

INSTALL_SH = os.path.join(REPO, "install.sh")
INSTALL_PS1 = os.path.join(REPO, "install.ps1")
GITIGNORE = os.path.join(REPO, ".gitignore")

#: These tests belong to the KIT, not to an installed project.
#:
#: The installer copies `tests/` into every host and then runs the suite as its
#: own verification step. A host has no `install.sh` — it is not a distribution —
#: so without this guard every user's install would end in "FAIL engine tests"
#: because the installer's verification tripped over a test that only makes
#: sense in the source repo. Measured before the guard existed: 5 failures and
#: 9 errors inside a freshly installed host.
_IS_THE_KIT = os.path.exists(INSTALL_SH) and os.path.exists(GITIGNORE)
_SKIP_REASON = ("not the dobby kit (no install.sh next to tests/) — these tests "
                "describe the distribution, not an installed project")


@unittest.skipUnless(_IS_THE_KIT, _SKIP_REASON)
class _KitOnly(unittest.TestCase):
    """Base for every test in this module. Skips cleanly inside a host."""


def _runtime_state_from(script_path: str) -> set[str]:
    """Extract the RuntimeState / RUNTIME_STATE list an installer declares."""
    text = Path(script_path).read_text(encoding="utf-8")
    entries: set[str] = set()
    # sh:  RUNTIME_STATE="a b \<newline> c"
    m = re.search(r"RUNTIME_STATE=\"([^\"]+)\"", text)
    if m:
        entries |= {e for e in m.group(1).replace("\\\n", " ").split()}
    # ps1: $RuntimeState = @('a', 'b', ...)
    m = re.search(r"\$RuntimeState\s*=\s*@\(([^)]+)\)", text, re.DOTALL)
    if m:
        entries |= {e.strip().strip("'\"")
                    for e in m.group(1).split(",") if e.strip()}
    return {e for e in entries if e}


def _gitignored_dobby_paths() -> set[str]:
    """The `.dobby/...` patterns `.gitignore` excludes, normalized."""
    out: set[str] = set()
    for line in Path(GITIGNORE).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(".dobby/"):
            continue
        out.add(line[len(".dobby/"):].rstrip("/"))
    return out


class TestExclusionListsAgree(_KitOnly):
    """`.gitignore` and both installers must name the same runtime state."""

    def test_shell_installer_covers_every_gitignored_path(self):
        declared = _runtime_state_from(INSTALL_SH)
        self.assertTrue(declared, "install.sh declares no RUNTIME_STATE")
        missing = _gitignored_dobby_paths() - declared
        self.assertEqual(
            missing, set(),
            f"install.sh would copy runtime state that .gitignore excludes: "
            f"{sorted(missing)}")

    def test_powershell_installer_covers_every_gitignored_path(self):
        declared = _runtime_state_from(INSTALL_PS1)
        self.assertTrue(declared, "install.ps1 declares no RuntimeState")
        missing = _gitignored_dobby_paths() - declared
        self.assertEqual(
            missing, set(),
            f"install.ps1 would copy runtime state that .gitignore excludes: "
            f"{sorted(missing)}")

    def test_both_installers_declare_the_same_list(self):
        self.assertEqual(_runtime_state_from(INSTALL_SH),
                         _runtime_state_from(INSTALL_PS1),
                         "the two installers would behave differently")

    def test_sandbox_captures_are_covered(self):
        """The worst case: captured stdout of arbitrary commands."""
        declared = _runtime_state_from(INSTALL_SH)
        self.assertTrue(
            any(p == "state" or p.startswith("state") for p in declared),
            "state/ must be excluded; state/sandbox holds captured command output")


def _engine_tests_from(script_path: str) -> set[str]:
    """The test modules an installer runs as its health check."""
    text = Path(script_path).read_text(encoding="utf-8")
    found: set[str] = set()
    for m in re.finditer(r"tests\.test_[a-z_]+", text):
        found.add(m.group(0))
    return found


class TestEngineHealthList(_KitOnly):
    """The installer's health list named a module that does not exist.

    `tests.test_policies` was written from memory, never checked, and turned the
    installer's own verification into a guaranteed ERROR. Any list of names that
    is not derived from the filesystem drifts from it.
    """

    def test_every_named_module_exists(self):
        for script in (INSTALL_SH, INSTALL_PS1):
            for module in _engine_tests_from(script):
                path = os.path.join(REPO, module.replace(".", os.sep) + ".py")
                self.assertTrue(
                    os.path.exists(path),
                    f"{os.path.basename(script)} runs {module}, which does not "
                    f"exist at {path}")

    def test_both_installers_check_the_same_modules(self):
        self.assertEqual(_engine_tests_from(INSTALL_SH),
                         _engine_tests_from(INSTALL_PS1))

    def test_the_health_check_actually_passes(self):
        modules = sorted(_engine_tests_from(INSTALL_SH))
        self.assertTrue(modules, "install.sh names no health modules")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *modules, "-q"],
            cwd=REPO, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=child_env(), timeout=300)
        self.assertEqual(proc.returncode, 0,
                         f"the installer's own health check fails:\n"
                         f"{proc.stderr[-600:]}")

    def test_health_check_is_a_subset_not_the_whole_suite(self):
        """A 600-test install step is disproportionate to the question asked."""
        all_tests = {f"tests.{f[:-3]}" for f in os.listdir(
            os.path.join(REPO, "tests")) if f.startswith("test_")}
        health = _engine_tests_from(INSTALL_SH)
        self.assertTrue(health < all_tests,
                        "the health check should be a proper subset")


class TestInterpreterProbe(_KitOnly):
    """Existence is not a measurement."""

    def test_shell_installer_probes_rather_than_trusting_command_v(self):
        text = Path(INSTALL_SH).read_text(encoding="utf-8")
        self.assertIn("sys.version_info[0]*100", text,
                      "install.sh must ask each candidate to COMPUTE a version; "
                      "a Store stub resolves and prints its own name")

    def test_powershell_installer_probes_too(self):
        text = Path(INSTALL_PS1).read_text(encoding="utf-8")
        self.assertIn("sys.version_info[0]*100", text)

    def test_both_try_more_than_one_candidate(self):
        for path in (INSTALL_SH, INSTALL_PS1):
            text = Path(path).read_text(encoding="utf-8")
            for name in ("python3", "python", "py"):
                self.assertIn(name, text, f"{path} does not try {name}")


@unittest.skipUnless(
    bash_path(),
    "install.sh declares #!/usr/bin/env bash and uses `set -o pipefail` "
    "plus ${BASH_SOURCE[0]}; no shell here supports them")
class TestShellInstallEndToEnd(_KitOnly):
    """Actually run it. The defects above were invisible to every other test."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="dobby-host-")
        self.addCleanup(shutil.rmtree, self.host, True)
        os.makedirs(os.path.join(self.host, "src"), exist_ok=True)
        with open(os.path.join(self.host, "package.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"name":"demo"}')
        with open(os.path.join(self.host, "AGENTS.md"), "w",
                  encoding="utf-8") as f:
            f.write("# the host's own contract\n")

    def _install(self, *extra):
        # The probed BASH. Two failures are being avoided at once, and each was
        # introduced by fixing the other.
        #
        # The literal "bash" resolved to C:\Windows\System32\bash.exe on a Windows
        # runner - the WSL launcher, which with no distribution installed printed
        # a UTF-16LE error and exited 1, after which seven assertions failed
        # complaining about missing files. Substituting posix_shell_path() fixed
        # Windows and broke Ubuntu in the same commit: /bin/sh there is dash, and
        # dash answers `set: Illegal option -o pipefail`.
        #
        # install.sh declares #!/usr/bin/env bash and uses BASH_SOURCE. The guard
        # must vet that capability, not "a POSIX shell" and not a name.
        shell = bash_path()
        self.assertIsNotNone(shell, "guard should have skipped this class")
        return subprocess.run(
            [shell, INSTALL_SH, self.host, *extra],
            cwd=REPO, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=child_env(), timeout=600)

    def test_install_succeeds_on_this_machine(self):
        proc = self._install()
        self.assertEqual(proc.returncode, 0,
                         f"install failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertNotIn("3.10+ required", proc.stdout + proc.stderr)

    def test_engine_lands_and_data_lands(self):
        self._install()
        for rel in ("dobby/cli.py", "dobby/core/kg.py", "mcp",
                    ".dobby/ontology.json", ".dobby/knowledge/kg.json",
                    ".claude/rules", "DESIGN.md", "CLAUDE.md"):
            self.assertTrue(os.path.exists(os.path.join(self.host, rel)),
                            f"missing after install: {rel}")

    def test_all_factory_skills_land(self):
        """Enumerated from the source, not hard-coded.

        The hard-coded list named three skills and passed while a fourth was
        added and never installed — the assertion described the test author's
        memory rather than the distribution. Reading the source directory means
        the next skill added is covered without anyone remembering to.
        """
        self._install()
        want = sorted(d for d in os.listdir(
            os.path.join(REPO, ".claude", "skills"))
            if os.path.isdir(os.path.join(REPO, ".claude", "skills", d)))
        self.assertIn("dobby", want, "the front-door skill is missing from the kit")
        landed = os.listdir(os.path.join(self.host, ".claude", "skills"))
        for name in want:
            self.assertIn(name, landed, f"skill did not install: {name}")
            self.assertTrue(
                os.path.exists(os.path.join(self.host, ".claude", "skills",
                                            name, "SKILL.md")),
                f"skill directory landed without its SKILL.md: {name}")

    def test_slash_commands_land(self):
        """`/dobby` does not exist in a host unless the command file travels."""
        src = os.path.join(REPO, ".claude", "commands")
        want = sorted(f for f in os.listdir(src) if f.endswith(".md"))
        self.assertIn("dobby.md", want)
        self._install()
        dest = os.path.join(self.host, ".claude", "commands")
        self.assertTrue(os.path.isdir(dest), "no .claude/commands in the host")
        for name in want:
            self.assertIn(name, os.listdir(dest),
                          f"slash command did not install: {name}")

    def test_every_harness_gets_a_door_it_can_actually_open(self):
        """One contract, four entry files.

        Each harness reads only the file named for it: Claude Code takes
        CLAUDE.md, Gemini CLI takes GEMINI.md, Qwen Code takes QWEN.md. Codex and
        opencode read AGENTS.md natively, which is why no `.codex/` adapter is
        written — that would duplicate the contract rather than extend its reach.

        Measured before this existed: `claude`, `codex`, `gemini` and `agy`
        binaries all present and usable on the authoring machine, while an
        installed host contained AGENTS.md and CLAUDE.md and nothing else. Gemini
        was a usable provider — with `web` capability, so `research run` selects
        it — working in a project whose operating contract it had no way to find.
        A rule that exists and is invisible is worse than an absent one.
        """
        self._install()
        for entry in ("AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md"):
            path = os.path.join(self.host, entry)
            with self.subTest(entry=entry):
                self.assertTrue(os.path.exists(path), f"{entry} did not install")
                with open(path, encoding="utf-8") as handle:
                    body = handle.read()
                self.assertIn("AGENTS.md", body,
                              f"{entry} does not point at the contract")

    def test_an_adapter_the_host_already_wrote_is_not_overwritten(self):
        """A host's own GEMINI.md outranks ours, exactly as AGENTS.md does."""
        mine = "# my own gemini notes\n"
        with open(os.path.join(self.host, "GEMINI.md"), "w",
                  encoding="utf-8") as handle:
            handle.write(mine)
        self._install()
        with open(os.path.join(self.host, "GEMINI.md"), encoding="utf-8") as handle:
            body = handle.read()
        self.assertTrue(body.startswith(mine), "the host's own file was replaced")
        self.assertIn("dobby", body, "no pointer was appended")

    def test_an_upgrade_backs_the_engine_up_and_names_the_restore_path(self):
        """Before this, an upgrade deleted the engine with no way back.

        `rm -rf $TARGET/dobby` then copy: if the incoming engine was broken or
        the copy died halfway, the working one was already gone. "Re-clone the
        kit" is not a restore path when the host may have been running a version
        the kit no longer has.
        """
        first = self._install()
        self.assertEqual(first.returncode, 0)
        backups = os.path.join(self.host, ".dobby", "backups")
        self.assertFalse(os.path.isdir(backups),
                         "a first install has nothing to back up")

        # Plant a marker so the backup can be shown to hold the PREVIOUS engine.
        marker = os.path.join(self.host, "dobby", "installed_marker.py")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("# from the first install\n")

        second = self._install()
        self.assertEqual(second.returncode, 0)
        self.assertTrue(os.path.isdir(backups), "the upgrade made no backup")
        stamps = sorted(os.listdir(backups))
        self.assertEqual(len(stamps), 1, f"expected one backup, got {stamps}")
        saved = os.path.join(backups, stamps[0], "dobby", "installed_marker.py")
        self.assertTrue(os.path.exists(saved),
                        "the backup does not contain the engine it replaced")
        self.assertFalse(os.path.exists(marker),
                         "the upgrade should have replaced the engine")
        self.assertIn("backed up", second.stdout)
        self.assertIn(stamps[0], second.stdout,
                      "a backup nobody can name is not a restore path")

    def test_backups_are_declared_runtime_state_so_they_never_travel(self):
        """A backup holds a previous engine, possibly one the host modified."""
        for path, label in ((INSTALL_SH, "install.sh"),
                            (INSTALL_PS1, "install.ps1")):
            with self.subTest(installer=label):
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
                self.assertIn("backups", text,
                              f"{label} does not exclude .dobby/backups")

    def test_no_runtime_state_is_copied(self):
        """The leak: audit logs, trajectories, and sandbox captures travelled."""
        # Plant state in the source that must NOT reach the host.
        planted = []
        for rel in ("state/sandbox/planted.out", "state/audit.jsonl",
                    "inventory.json", "specialization.json"):
            path = os.path.join(REPO, ".dobby", rel)
            if os.path.exists(path):
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("PLANTED-SECRET-DO-NOT-TRAVEL\n")
            planted.append(path)
        self.addCleanup(lambda: [os.remove(p) for p in planted
                                 if os.path.exists(p)])

        self._install()

        host_dobby = os.path.join(self.host, ".dobby")
        offenders = []
        for root, _dirs, files in os.walk(host_dobby):
            for name in files:
                path = os.path.join(root, name)
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        if "PLANTED-SECRET-DO-NOT-TRAVEL" in f.read():
                            offenders.append(os.path.relpath(path, self.host))
                except OSError:
                    continue
        self.assertEqual(offenders, [],
                         f"runtime state travelled to the host: {offenders}")

    def test_hosts_own_agents_md_is_not_overwritten(self):
        self._install()
        with open(os.path.join(self.host, "AGENTS.md"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("the host's own contract", body,
                      "the host's contract must win; a pointer is appended")
        self.assertIn("dobby", body)

    def test_reinstall_preserves_curated_data(self):
        self._install()
        kg = os.path.join(self.host, ".dobby", "knowledge", "kg.json")
        with open(kg, "a", encoding="utf-8") as f:
            f.write("\n")           # a stand-in for curation
        before = Path(kg).read_text(encoding="utf-8")
        proc = self._install()
        self.assertIn("EXISTS", proc.stdout)
        self.assertEqual(Path(kg).read_text(encoding="utf-8"), before,
                         "an upgrade must never rewrite curated knowledge")

    def test_reinstall_does_not_report_bootstrap_as_failed(self):
        """Already-instantiated is the normal upgrade state, not a failure."""
        self._install()
        proc = self._install()
        self.assertNotIn("FAIL bootstrap", proc.stdout)

    def test_dry_run_writes_nothing(self):
        proc = self._install("--dry")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.host, "dobby")),
                         "--dry must not write")

    def test_refuses_to_install_into_itself(self):
        proc = subprocess.run(
            [bash_path(), INSTALL_SH, REPO], cwd=REPO,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=child_env(), timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("dobby repo itself", proc.stdout + proc.stderr)




@unittest.skipUnless(
    bash_path(),
    "install.sh declares #!/usr/bin/env bash and uses `set -o pipefail`")
class TestLaunchersLandAndRun(_KitOnly):
    r"""`dobby doctor` instead of `python -m dobby.cli doctor`.

    Two names, one collision, and one measured limit.

    The POSIX launcher is `dobby.sh`, NOT `dobby`. The installer copies the engine
    package to `<host>/dobby/`, so a file named `dobby` beside it is the same name
    in the same directory - the first attempt failed on its first run with
    "line 202: .../dobby: Is a directory". On Windows there is no collision:
    `dobby.cmd` differs from the directory and cmd.exe resolves a bare `dobby`
    through PATHEXT, which is what makes the short form work at all.

    Both launchers hard-code the interpreter the installer PROBED. Writing
    `python3` would reintroduce the Store-redirector defect the probe exists to
    avoid - a name that resolves and executes nothing.

    The .cmd truncates an argument at its first newline. Measured here rather than
    asserted in a comment, because a limit nobody checks is a limit that quietly
    becomes false in both directions.
    """

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="dobby-launcher-")
        self.addCleanup(shutil.rmtree, self.host, True)
        proc = subprocess.run([bash_path(), INSTALL_SH, self.host], cwd=REPO,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=child_env(), timeout=900)
        self.assertEqual(proc.returncode, 0,
                         f"install failed:\n{proc.stdout}\n{proc.stderr}")

    def test_both_launchers_land(self):
        for name in ("dobby.cmd", "dobby.sh"):
            self.assertTrue(os.path.exists(os.path.join(self.host, name)), name)

    def test_the_posix_launcher_is_not_named_dobby(self):
        """It would collide with the engine package directory."""
        self.assertTrue(os.path.isdir(os.path.join(self.host, "dobby")))
        self.assertFalse(os.path.isfile(os.path.join(self.host, "dobby")))

    def test_neither_launcher_hardcodes_a_bare_interpreter_name(self):
        """`python3` resolves to a Store stub on Windows and executes nothing."""
        for name in ("dobby.cmd", "dobby.sh"):
            body = Path(os.path.join(self.host, name)).read_text(encoding="utf-8")
            self.assertIn("-m dobby.cli", body, name)
            first = [w for w in body.split() if "dobby.cli" in body][0]
            self.assertNotRegex(
                body, r'(?m)^\s*(?:exec\s+)?python3?\s+-m dobby\.cli',
                f"{name} trusts a bare interpreter name")

    @unittest.skipUnless(os.name == "nt", "the .cmd launcher is for Windows")
    def test_the_cmd_launcher_runs_and_agrees_with_the_module_form(self):
        launcher = os.path.join(self.host, "dobby.cmd")
        via_launcher = subprocess.run([launcher, "doctor"], cwd=self.host,
                                      capture_output=True, text=True,
                                      encoding="utf-8", errors="replace",
                                      env=child_env(), timeout=600)
        via_module = subprocess.run(
            [sys.executable, "-m", "dobby.cli", "doctor"], cwd=self.host,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=child_env(), timeout=600)
        self.assertEqual(via_launcher.returncode, 0, via_launcher.stderr[-300:])
        self.assertEqual(json.loads(via_launcher.stdout)["verdict"],
                         json.loads(via_module.stdout)["verdict"])

    @unittest.skipUnless(os.name == "nt", "the .cmd launcher is for Windows")
    def test_the_cmd_launcher_truncates_a_multiline_argument(self):
        """The documented limit, as a measurement.

        If this ever starts passing, cmd.exe stopped truncating and the note in
        install.sh should be removed rather than left to mislead.
        """
        launcher = os.path.join(self.host, "dobby.cmd")
        proc = subprocess.run([launcher, "route", "line one\nline two"],
                              cwd=self.host, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env=child_env(), timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-200:])
        self.assertEqual(json.loads(proc.stdout)["task"], "line one")

    @unittest.skipUnless(os.name == "nt", "the .cmd launcher is for Windows")
    def test_a_single_line_argument_survives_the_launcher(self):
        """The case that matters for ordinary use."""
        launcher = os.path.join(self.host, "dobby.cmd")
        task = "add rate limiting to the upload endpoint 100% now"
        proc = subprocess.run([launcher, "route", task], cwd=self.host,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=child_env(), timeout=600)
        self.assertEqual(json.loads(proc.stdout)["task"], task)

    def test_both_installers_write_the_same_launcher_names(self):
        """They drift silently otherwise; the suite already asserts this pattern."""
        sh = Path(INSTALL_SH).read_text(encoding="utf-8")
        ps1 = Path(INSTALL_PS1).read_text(encoding="utf-8")
        for name in ("dobby.cmd", "dobby.sh"):
            self.assertIn(name, sh, f"install.sh does not write {name}")
            self.assertIn(name, ps1, f"install.ps1 does not write {name}")


if __name__ == "__main__":
    unittest.main()
