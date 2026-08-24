"""Runnable acceptance gates: a ledger something OTHER than the author grades.

Why this exists
---------------
`.claude/rules/verification-and-completion.md` already carries the rule this
module enforces — "(1) the producing command exiting 0 is not validation" and
"(6) re-read the ledger top to bottom and verify each evidence path exists and
supports its claim". Both are addressed to the agent. The ledger is prose the
agent writes and the same agent grades, so nothing outside the agent can fail it,
and a report that says `done` with an evidence path nobody opened is
indistinguishable from one that means it.

`dobby/project/evidence.py` does grade artifacts, but only the seven research
KINDS against JSON schemas. It has no way to say "this shell command must exit
zero AND its output must contain this token".

Ported from `Leonxlnx/unlazy` (MIT), which is that missing piece. The ledger
format, the metadata keys, the id rules, the regex delimiter syntax, the
conjunction that defines "met", the oracle field set and the structural error
strings are all reproduced from the reference implementation
(`scripts/lib/gates.mjs`, `scripts/gate-check.mjs`) rather than redesigned,
because P-CONTRACT says to match the consumer's exact format and because a
`GATES.md` should move between the two tools unchanged. What is NOT ported, and
why, is recorded in `reports/LEDGER_runnable_gates.md`.

The three ideas worth the port
------------------------------
1. **Met is a conjunction.** Checkbox marked AND exit 0 AND EXPECT matched. Any
   one of the three alone is the failure the rule above names.
2. **An impossible gate is ABANDONED, never deleted.** Silently dropping a gate
   that turned out unmeetable is Evaluation Gaming (`docs/FAILURE_CATALOG.md`)
   with the evidence removed. `ABANDON:` makes it a transition with a reason.
3. **Approval is bound to an oracle fingerprint, and stored outside the repo.**
   An approval is for one command, one expectation, one cwd, one shell, one
   platform, one PATH. Change any of them and it is void. Outside the repo,
   because an agent that can write the gate must not also be able to commit its
   own approval of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

#: `/^- \[( |x|X)\] (.*)$/` in the reference. A gate is a markdown checkbox, so
#: a ledger stays readable as a document and reviewable in a diff.
GATE_RE = re.compile(r"^- \[( |x|X)\] (.*)$")

#: `/^(\s+)(CHECK|EXPECT|EVIDENCE|CWD):\s?(.*)$/`. The leading indent is load
#: bearing: it is what attaches an attribute to the gate above it, and an
#: unindented one is an error rather than a silently orphaned line.
ATTR_RE = re.compile(r"^(\s+)(CHECK|EXPECT|EVIDENCE|CWD):[ \t]?(.*)$")

#: `/^ABANDON:\s*(\S*)\s*(.*)$/`, unindented, naming a gate declared elsewhere.
ABANDON_RE = re.compile(r"^ABANDON:\s*(\S*)\s*(.*)$")

#: `/^(\S+?):(?:\s+|$)/` — an explicit id ends at the first colon.
ID_RE = re.compile(r"^(\S+?):(?:\s+|$)")

VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: `/^\/([\s\S]*)\/([a-z]*)$/` — an EXPECT delimited by forward slashes is a
#: regex, anything else is a literal substring. Not a guess: a bare `EXPECT: .*`
#: means the three characters, and treating it as a pattern would pass every
#: gate that produced any output at all.
REGEX_RE = re.compile(r"^/([\s\S]*)/([a-z]*)$", re.DOTALL)

#: The reference caps regex source at 1000 characters and gives a match 250ms.
MAX_REGEX_SOURCE = 1000
REGEX_TIMEOUT_MS = 250

DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000

#: Bumped when a change would make an old approval mean something different.
#: An approval carries the schema it was written under, so a stored record from
#: before a semantics change fails to match rather than authorising the new one.
ORACLE_SCHEMA = 1
APPROVAL_SCHEMA = 1

TEXT, REGEX = "text", "regex"


class GateError(Exception):
    """A ledger that cannot be read. Never raised for a gate that merely fails."""


@dataclass(frozen=True)
class Expectation:
    kind: str
    value: str
    flags: str = ""

    def describe(self) -> str:
        return f"/{self.value}/{self.flags}" if self.kind == REGEX else self.value


@dataclass(frozen=True)
class Gate:
    id: str
    title: str
    checked: bool
    line: int
    check: str = ""
    expect: Expectation | None = None
    cwd: str = ""
    evidence: str = ""
    evidence_line: int = -1
    abandoned: str = ""

    @property
    def manual(self) -> bool:
        """A gate no command can decide. It carries an outcome and no CHECK.

        Kept legal on purpose: forcing a command where none exists is how a
        ledger acquires a check that passes without deciding anything.
        """
        return not self.check and not self.abandoned

    @property
    def runnable(self) -> bool:
        return bool(self.check) and not self.abandoned


@dataclass
class Document:
    gates: list[Gate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def by_id(self, gate_id: str) -> Gate | None:
        return next((g for g in self.gates if g.id == gate_id), None)


def _expectation(raw: str) -> tuple[Expectation | None, str]:
    """Parse an EXPECT value. Returns (expectation, error)."""
    match = REGEX_RE.match(raw)
    if not match:
        return Expectation(TEXT, raw), ""
    source, flags = match.group(1), match.group(2)
    if len(source) > MAX_REGEX_SOURCE:
        return None, (f"EXPECT regex source exceeds {MAX_REGEX_SOURCE} "
                      f"characters")
    for flag in flags:
        if flag not in "ims":
            return None, f"EXPECT regex has unsupported flag {flag!r}"
    try:
        re.compile(source)
    except re.error as exc:
        return None, f"EXPECT regex does not compile: {exc}"
    return Expectation(REGEX, source, flags), ""


def parse(text: str) -> Document:
    """Read a ledger. Structural defects are collected, never raised.

    Collected rather than raised because a caller wants every defect in one
    pass; a parser that dies on the first one turns a five-minute fix into five
    round trips.
    """
    doc = Document()
    seen: dict[str, int] = {}
    abandons: dict[str, str] = {}
    pending: list[tuple[str, str, int]] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        gate = Gate(id=current["id"], title=current["title"],
                    checked=current["checked"], line=current["line"],
                    check=current.get("CHECK", ""),
                    expect=current.get("expect"),
                    cwd=current.get("CWD", ""),
                    evidence=current.get("EVIDENCE", ""),
                    evidence_line=current.get("evidence_line", -1))
        if gate.check and gate.expect is None:
            doc.errors.append(
                f"gate {gate.id}: runnable gates require both non-blank CHECK "
                f"and EXPECT")
        elif gate.expect is not None and not gate.check:
            doc.errors.append(
                f"gate {gate.id}: runnable gates require both non-blank CHECK "
                f"and EXPECT")
        doc.gates.append(gate)
        current = None

    for lineno, line in enumerate(text.splitlines(), start=1):
        gate_match = GATE_RE.match(line)
        if gate_match:
            flush()
            raw_title = gate_match.group(2).strip()
            if not raw_title:
                doc.errors.append(f"line {lineno}: gate outcome is blank")
                continue
            id_match = ID_RE.match(raw_title)
            if not id_match:
                doc.errors.append(
                    f"line {lineno}: gate needs an explicit ID followed "
                    f"immediately by a colon, got {raw_title[:60]!r} - "
                    f"an id cannot contain a space")
                continue
            gate_id = id_match.group(1)
            title = raw_title[id_match.end():].strip()
            if not VALID_ID_RE.match(gate_id):
                doc.errors.append(
                    f"line {lineno}: invalid gate id {gate_id!r} - ids are "
                    f"a letter or digit then letters, digits, . _ -")
                continue
            if gate_id in seen:
                doc.errors.append(
                    f"line {lineno}: duplicate gate id {gate_id} (first "
                    f"declared on line {seen[gate_id]})")
                continue
            seen[gate_id] = lineno
            current = {"id": gate_id, "title": title, "line": lineno,
                       "checked": gate_match.group(1).lower() == "x"}
            continue

        attr_match = ATTR_RE.match(line)
        if attr_match:
            key, value = attr_match.group(2), attr_match.group(3).strip()
            if current is None:
                doc.errors.append(
                    f"line {lineno}: orphan {key} is not attached to a gate")
                continue
            if key in current or (key == "EXPECT" and "expect" in current):
                doc.errors.append(
                    f"line {lineno}: duplicate {key} for gate {current['id']}")
                continue
            if key == "EXPECT":
                expectation, error = _expectation(value)
                if error:
                    doc.errors.append(f"line {lineno}: {error}")
                    continue
                current["expect"] = expectation
                current["EXPECT"] = value
            else:
                current[key] = value
                if key == "EVIDENCE":
                    current["evidence_line"] = lineno
            if key in ("CHECK",) and not value:
                doc.errors.append(
                    f"line {lineno}: CHECK and EXPECT cannot be blank")
            continue

        # An attribute written flush against the margin is the commonest way a
        # ledger silently loses a check: it looks attached and is not.
        bare = re.match(r"^(CHECK|EXPECT|EVIDENCE|CWD):", line)
        if bare:
            doc.errors.append(
                f"line {lineno}: unindented {bare.group(1)} is not attached to "
                f"a gate; indent attribute lines with spaces")
            continue

        abandon = ABANDON_RE.match(line)
        if abandon:
            gate_id, reason = abandon.group(1), abandon.group(2).strip()
            if not gate_id:
                doc.errors.append(
                    f"line {lineno}: ABANDON needs a gate id and reason")
            elif not reason:
                doc.errors.append(
                    f"line {lineno}: ABANDON {gate_id} needs a non-blank reason")
            elif gate_id in abandons:
                doc.errors.append(f"line {lineno}: duplicate ABANDON for "
                                  f"{gate_id}")
            else:
                abandons[gate_id] = reason
                pending.append((gate_id, reason, lineno))
            continue

    flush()

    for gate_id, reason, lineno in pending:
        gate = doc.by_id(gate_id)
        if gate is None:
            doc.errors.append(
                f"line {lineno}: ABANDON names unknown gate {gate_id}")
            continue
        doc.gates[doc.gates.index(gate)] = replace(gate, abandoned=reason)

    return doc


# -- the oracle ------------------------------------------------------------

def _shell() -> str:
    """The shell a CHECK will ACTUALLY run under, not the user's login shell.

    `subprocess.run(shell=True)` execs `/bin/sh -c` on POSIX no matter what
    `$SHELL` says, and `COMSPEC` on Windows. A first version read `$SHELL`, so
    an approval on a host with bash as the login shell recorded a binding to
    `/bin/bash` while the command ran under `/bin/sh` — a fingerprint naming
    something that never executed, and the difference is not cosmetic when the
    two disagree about brace expansion or `[[`. Not caught by any test here
    because this machine is `nt`, where the two happened to coincide.
    """
    if os.name == "nt":
        return os.environ.get("COMSPEC", "cmd.exe")
    return "/bin/sh"


def oracle(gate: Gate, *, cwd: str, timeout_s: int = DEFAULT_TIMEOUT_S,
           max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> dict:
    """Everything an approval is FOR.

    The point of listing PATH and platform is that a command is not a stable
    object. `python -m pytest` means one thing with a venv on PATH and another
    without it, and a check approved on Linux says nothing about the same string
    under `cmd.exe`. This repository has already paid for that class of
    assumption twice — `text=True` without `encoding=` dying on a Korean path,
    and `py_compile(cfile=os.devnull)` failing only on Windows.
    """
    resolved = gate.cwd or cwd
    return {
        "schema": ORACLE_SCHEMA,
        "check": gate.check,
        "expect": gate.expect.describe() if gate.expect else "",
        "expect_kind": gate.expect.kind if gate.expect else "",
        "cwd": os.path.abspath(resolved),
        "shell": _shell(),
        "timeoutMs": int(timeout_s * 1000),
        "maxOutputBytes": int(max_output_bytes),
        "regexTimeoutMs": REGEX_TIMEOUT_MS,
        "platform": sys.platform,
        "path": os.environ.get("PATH", ""),
    }


def signature(oracle_doc: dict) -> str:
    """A stable digest of the oracle. Sorted keys, so field order cannot flip it."""
    blob = json.dumps(oracle_doc, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def approval_dir() -> str:
    """Outside the repository by default.

    A record kept beside the ledger can be written by whoever wrote the ledger,
    which makes the approval a formality. `DOBBY_APPROVAL_DIR` exists so tests
    can redirect it; it is not a way to move approvals into a work tree.

    Deliberately NOT unlazy's `~/.unlazy/approved`. The two tools enforce
    different execution limits, and honouring each other's records would let an
    approval granted under one set of bounds authorise a run under another.
    """
    override = os.environ.get("DOBBY_APPROVAL_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.expanduser("~"), ".dobby", "approved")


def approval_path(ledger: str, gate: Gate, oracle_doc: dict) -> str:
    key = f"{os.path.abspath(ledger)}\0{gate.id}\0{signature(oracle_doc)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(approval_dir(), f"{digest}.json")


def is_approved(ledger: str, gate: Gate, oracle_doc: dict) -> bool:
    """All three must match: ledger path, gate id, oracle signature."""
    path = approval_path(ledger, gate, oracle_doc)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return False
    return (record.get("schema") == APPROVAL_SCHEMA
            and record.get("gate") == gate.id
            and record.get("file") == os.path.abspath(ledger)
            and record.get("signature") == signature(oracle_doc))


def approve(ledger: str, gate: Gate, oracle_doc: dict) -> str:
    path = approval_path(ledger, gate, oracle_doc)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "schema": APPROVAL_SCHEMA,
        "file": os.path.abspath(ledger),
        "gate": gate.id,
        "signature": signature(oracle_doc),
        "oracle": oracle_doc,
        # Wall clock, recorded for the human reading the record later. Nothing
        # in the match depends on it, so a clock skew cannot validate anything.
        "approvedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return path


# -- running ---------------------------------------------------------------

_REGEX_CHILD = (
    "import re,sys\n"
    "src=sys.argv[1];flags=sys.argv[2]\n"
    "f=0\n"
    "f|=re.I if 'i' in flags else 0\n"
    "f|=re.M if 'm' in flags else 0\n"
    "f|=re.S if 's' in flags else 0\n"
    "data=sys.stdin.buffer.read().decode('utf-8','replace')\n"
    "sys.exit(0 if re.search(src,data,f) else 1)\n"
)


#: Measured once per process by `_spawn_overhead_s`. Not a constant, because it
#: is a property of the machine and differs by an order of magnitude between a
#: warm Linux CI box and this one.
_SPAWN_S: float | None = None


def _spawn_overhead_s() -> float:
    """What an interpreter start costs before any matching happens.

    The reference gives a match 250ms inside an already-running worker thread.
    A child process pays for `python -c` first, and on this machine that alone
    exceeded the whole budget: a correct `/^gate-\\w+$/m` was killed as if it
    had backtracked. Measured by the test that caught it.

    So the budget is stated as what it is — one interpreter start plus the
    reference's match allowance — rather than borrowing a number whose meaning
    does not survive the change of mechanism. Doubling the measured start is
    headroom for the variance between two spawns, not a guess at the match.
    """
    global _SPAWN_S
    if _SPAWN_S is None:
        started = time.monotonic()
        try:
            subprocess.run([sys.executable, "-c", ""], capture_output=True,
                           timeout=60)
            _SPAWN_S = max(time.monotonic() - started, 0.0)
        except (OSError, subprocess.SubprocessError):
            # Unmeasurable here; assume a slow start rather than a fast one, so
            # the failure mode is a late kill and not a false timeout.
            _SPAWN_S = 1.0
    return _SPAWN_S


def _match_regex(expect: Expectation, output: str) -> tuple[bool, str]:
    """Match in a child process, so the time bound is real.

    Python cannot interrupt a regex mid-match: `re` runs in C and releases
    nothing a signal or a thread flag could reach, so the reference's
    250ms worker-thread bound has no in-process equivalent here. A thread would
    return a timeout verdict while the match kept a core busy for as long as it
    liked, which is a bound in the report and not in the machine. A child can
    actually be killed.

    The cost is one interpreter start per regex gate. Gates already spawn a
    shell for CHECK, so this does not change the order of magnitude, and text
    expectations — the common case — never reach here.
    """
    budget = REGEX_TIMEOUT_MS / 1000.0 + 2 * _spawn_overhead_s()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _REGEX_CHILD, expect.value, expect.flags],
            input=output.encode("utf-8", "replace"),
            capture_output=True, timeout=budget)
    except subprocess.TimeoutExpired:
        return False, (f"EXPECT regex exceeded its {REGEX_TIMEOUT_MS}ms match "
                       f"allowance (budget {budget:.2f}s including one "
                       f"interpreter start) and was killed")
    if proc.returncode not in (0, 1):
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, f"EXPECT regex failed to run: {tail[-1] if tail else ''}"
    return proc.returncode == 0, ""


def matches(expect: Expectation | None, output: str) -> tuple[bool, str]:
    if expect is None:
        return False, "no EXPECT declared"
    if expect.kind == TEXT:
        # Substring, as the reference does with `output.includes(value)`.
        return expect.value in output, ""
    return _match_regex(expect, output)


def run_gate(gate: Gate, *, ledger: str, cwd: str = ".",
             timeout_s: int = DEFAULT_TIMEOUT_S,
             max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
             require_approval: bool = True) -> dict:
    """Execute one gate and grade it. Never raises for a failing gate.

    `met` is the conjunction the whole module exists for: the box is ticked, the
    process exited zero, AND the output matched. Reporting any one of the three
    as completion is the defect
    `.claude/rules/verification-and-completion.md` (1) names.
    """
    oracle_doc = oracle(gate, cwd=cwd, timeout_s=timeout_s,
                        max_output_bytes=max_output_bytes)
    verdict: dict[str, Any] = {
        "id": gate.id, "title": gate.title, "checked": gate.checked,
        "kind": "runnable", "oracle": oracle_doc, "met": False,
        "exit_code": None, "matched": False, "output": "", "reason": "",
    }

    if gate.abandoned:
        verdict.update(kind="abandoned", reason=gate.abandoned)
        return verdict
    if gate.manual:
        # A manual gate is decided by a human, so this reports it and refuses to
        # score it. Counting it as met would be the module grading a claim
        # nobody checked.
        verdict.update(kind="manual",
                       reason="manual oracle: no command can decide this")
        return verdict
    if require_approval and not is_approved(ledger, gate, oracle_doc):
        verdict.update(kind="unapproved",
                       reason="not approved for this exact oracle; run approve "
                              "after reading the command")
        return verdict

    work_dir = oracle_doc["cwd"]
    if not os.path.isdir(work_dir):
        verdict.update(reason=f"CWD does not exist: {work_dir}")
        return verdict

    started = time.monotonic()
    try:
        # Bytes, not text. Capturing str would decode with the console codepage,
        # which is the measured cp949 crash on this machine's Korean paths, and
        # the byte cap below could not be applied to characters anyway.
        proc = subprocess.run(gate.check, shell=True, cwd=work_dir,
                              capture_output=True, timeout=timeout_s)
        raw = (proc.stdout or b"") + (proc.stderr or b"")
        code = proc.returncode
        note = ""
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or b"") + (exc.stderr or b"")
        code, note = None, f"CHECK exceeded {timeout_s}s and was killed"
    except OSError as exc:
        raw, code, note = b"", None, f"CHECK could not start: {exc}"

    truncated = len(raw) > max_output_bytes
    output = raw[:max_output_bytes].decode("utf-8", "replace")
    matched, why = matches(gate.expect, output)

    verdict.update(
        exit_code=code, matched=matched, output=output, truncated=truncated,
        wall_s=round(time.monotonic() - started, 3),
        met=bool(gate.checked and code == 0 and matched),
        reason=note or why or _why_not(gate, code, matched, truncated))
    return verdict


def _why_not(gate: Gate, code: int | None, matched: bool,
             truncated: bool = False) -> str:
    """Why the gate is not met, naming the CAUSE and not just the symptom.

    Truncation is called out separately because it is not a content failure:
    the deciding token may have been produced and then cut off by the byte cap,
    and "EXPECT did not match" sends an operator to read the command when the
    fix is `--max-output-bytes`. Measured with a 50-byte cap on output whose
    token arrives at byte 300.
    """
    missing = []
    if not gate.checked:
        missing.append("checkbox not marked")
    if code != 0:
        missing.append(f"exit {code}")
    if not matched:
        missing.append("EXPECT did not match"
                       + (" (output was TRUNCATED at the byte cap, so the "
                          "deciding token may have been cut off — raise "
                          "--max-output-bytes before believing this)"
                          if truncated else ""))
    return "; ".join(missing)


def evidence_line(verdict: dict) -> str:
    """The EVIDENCE value: shell, cwd, exit status, the verdict, decisive output.

    `met=` is written explicitly rather than left to be re-derived from `exit`
    and `matched`, because `recorded_met` reads it back and a reader that has to
    recompute a conjunction is a second place for the conjunction to be got
    wrong.
    """
    oracle_doc = verdict.get("oracle") or {}
    decisive = " ".join((verdict.get("output") or "").split())[:200]
    return (f"{EVIDENCE_PREFIX}{oracle_doc.get('shell', '?')} "
            f"cwd={oracle_doc.get('cwd', '?')} "
            f"exit={verdict.get('exit_code')} "
            f"matched={verdict.get('matched')} "
            f"met={bool(verdict.get('met'))} :: {decisive}")


def apply_evidence(text: str, verdicts: Sequence[dict]) -> str:
    """Write each verdict back into its gate's EVIDENCE line.

    Only gates that already declare an EVIDENCE line are rewritten. Inserting
    one would move every line below it and invalidate the line numbers the rest
    of this pass is holding.
    """
    doc = parse(text)
    lines = text.splitlines(keepends=False)
    for verdict in verdicts:
        gate = doc.by_id(verdict.get("id", ""))
        if gate is None or gate.evidence_line < 1:
            continue
        original = lines[gate.evidence_line - 1]
        indent = re.match(r"^(\s*)", original).group(1)
        lines[gate.evidence_line - 1] = (f"{indent}EVIDENCE: "
                                         f"{evidence_line(verdict)}")
    tail = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + tail


#: Every EVIDENCE line this module writes starts here. It is how a machine-
#: written result is told from whatever the author typed — see `was_verified`.
EVIDENCE_PREFIX = "shell="


#: The verdict token inside a machine-written EVIDENCE line. Present so a
#: recorded result can be told from a recorded PASS.
MET_TOKEN = "met=True"


def was_verified(gate: Gate) -> bool:
    """Whether THIS module recorded a result on the gate — pass or fail.

    Not "does the gate have an EVIDENCE line". A placeholder is an EVIDENCE
    line, and so is a sentence an author wrote by hand; treating either as a
    completed check is the failure the module exists to prevent, and it was
    measured — `EVIDENCE: not yet run` caused `verify` to skip two unmet gates
    and report the ledger `ok`.
    """
    return gate.evidence.startswith(EVIDENCE_PREFIX)


def recorded_met(gate: Gate) -> bool:
    """Whether this module recorded the gate as MET.

    The distinction from `was_verified` is the module's own thesis turned on
    itself: a recorded result is not a pass. Measured — after a run wrote
    evidence to a passing and a failing gate alike, the next `verify` skipped
    both, and the failing one vanished from the report it was supposed to be
    blocking.
    """
    return was_verified(gate) and MET_TOKEN in gate.evidence


def summarise(verdicts: Sequence[dict], errors: Sequence[str]) -> dict:
    """The shape a caller decides on. `ok` is deliberately conservative.

    Three ways to be not-ok, and the third is the one that was measured:

    - Structural errors, even when every gate that parsed passed. A duplicate id
      or an orphaned CHECK means the ledger does not say what it appears to say,
      and grading the readable half of an unreadable document is how a missing
      gate reads as a met one.
    - Any runnable gate unmet, or any gate unapproved.
    - **Nothing ran.** An empty ledger, a ledger of nothing but manual gates, and
      a ledger whose every gate was abandoned all verify vacuously. `all()` over
      an empty list is True, so the naive form reported `ok` for a run that
      checked nothing — which is exactly the claim this module refuses to let an
      agent make about itself.
    """
    runnable = [v for v in verdicts if v["kind"] == "runnable"]
    unapproved = [v["id"] for v in verdicts if v["kind"] == "unapproved"]
    nothing_ran = not runnable
    return {
        "gates": len(verdicts),
        "runnable": len(runnable),
        "met": sum(1 for v in runnable if v["met"]),
        "unmet": [v["id"] for v in runnable if not v["met"]],
        "manual": [v["id"] for v in verdicts if v["kind"] == "manual"],
        "abandoned": [v["id"] for v in verdicts if v["kind"] == "abandoned"],
        "unapproved": unapproved,
        "nothing_ran": nothing_ran,
        "errors": list(errors),
        "ok": (not errors
               and not nothing_ran
               and all(v["met"] for v in runnable)
               and not unapproved),
    }
