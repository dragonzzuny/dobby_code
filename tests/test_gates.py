"""The gate ledger, graded by something other than whoever wrote it.

Every test here corresponds to a way a ledger can appear to pass while proving
nothing — which is the whole reason `dobby/gates.py` exists rather than another
paragraph in the rules. The conjunction tests (checkbox AND exit 0 AND EXPECT)
are the load-bearing ones: each of the three alone is a real report this
repository's rules already forbid and nothing could previously detect.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby import gates  # noqa: E402

#: Commands built from the running interpreter, so the suite does not depend on
#: `sh` builtins that `cmd.exe` spells differently — the platform assumption
#: this repository has already been bitten by twice.
PY = f'"{sys.executable}"'
OK = f"{PY} -c \"print('gate-ok')\""
FAILING = f"{PY} -c \"print('gate-ok'); raise SystemExit(3)\""
QUIET = f"{PY} -c \"print('something-else')\""


class Sandbox(unittest.TestCase):
    """Approvals redirected to a temp dir, so no test can touch a real one."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dobby-gates-")
        self.approvals = os.path.join(self.tmp, "approved")
        self._prev = os.environ.get("DOBBY_APPROVAL_DIR")
        os.environ["DOBBY_APPROVAL_DIR"] = self.approvals
        self.ledger = os.path.join(self.tmp, "GATES.md")

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("DOBBY_APPROVAL_DIR", None)
        else:
            os.environ["DOBBY_APPROVAL_DIR"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text: str) -> str:
        with open(self.ledger, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return self.ledger

    def one(self, text: str) -> gates.Gate:
        doc = gates.parse(text)
        self.assertEqual(doc.errors, [], "unexpected structural errors")
        self.assertEqual(len(doc.gates), 1)
        return doc.gates[0]


class ParseTest(Sandbox):
    def test_reads_a_runnable_gate(self):
        gate = self.one("- [ ] G1: suite is green\n"
                        "  CHECK: pytest -q\n"
                        "  EXPECT: 0 failed\n")
        self.assertEqual(gate.id, "G1")
        self.assertEqual(gate.title, "suite is green")
        self.assertFalse(gate.checked)
        self.assertEqual(gate.check, "pytest -q")
        self.assertEqual(gate.expect, gates.Expectation(gates.TEXT, "0 failed"))
        self.assertTrue(gate.runnable)

    def test_checkbox_case_is_accepted_either_way(self):
        for mark in ("x", "X"):
            gate = self.one(f"- [{mark}] G1: done\n"
                            f"  CHECK: {OK}\n  EXPECT: gate-ok\n")
            self.assertTrue(gate.checked)

    def test_id_must_be_explicit_and_valid(self):
        no_colon = gates.parse("- [ ] just a sentence with no id\n")
        self.assertIn("gate needs an explicit ID followed by a colon",
                      " ".join(no_colon.errors))
        bad = gates.parse("- [ ] -bad*id: title\n")
        self.assertIn("invalid gate id", " ".join(bad.errors))
        blank = gates.parse("- [ ] \n")
        self.assertIn("gate outcome is blank", " ".join(blank.errors))

    def test_duplicate_id_is_an_error_naming_the_first_line(self):
        doc = gates.parse("- [ ] G1: one\n"
                          "  CHECK: a\n  EXPECT: b\n"
                          "- [ ] G1: two\n"
                          "  CHECK: a\n  EXPECT: b\n")
        joined = " ".join(doc.errors)
        self.assertIn("duplicate gate id G1", joined)
        self.assertIn("first declared on line 1", joined)

    def test_unindented_attribute_is_not_silently_orphaned(self):
        doc = gates.parse("- [ ] G1: one\n"
                          "CHECK: pytest -q\n")
        self.assertIn("unindented CHECK is not attached to a gate",
                      " ".join(doc.errors))

    def test_orphan_and_duplicate_attributes(self):
        orphan = gates.parse("  CHECK: pytest\n")
        self.assertIn("orphan CHECK is not attached to a gate",
                      " ".join(orphan.errors))
        dup = gates.parse("- [ ] G1: one\n"
                          "  CHECK: a\n  CHECK: b\n  EXPECT: c\n")
        self.assertIn("duplicate CHECK for gate G1", " ".join(dup.errors))

    def test_check_without_expect_is_rejected_both_ways(self):
        only_check = gates.parse("- [ ] G1: one\n  CHECK: pytest\n")
        self.assertIn("require both non-blank CHECK and EXPECT",
                      " ".join(only_check.errors))
        only_expect = gates.parse("- [ ] G1: one\n  EXPECT: 0 failed\n")
        self.assertIn("require both non-blank CHECK and EXPECT",
                      " ".join(only_expect.errors))

    def test_a_manual_gate_is_legal(self):
        gate = self.one("- [ ] M1: a human confirmed the layout\n")
        self.assertTrue(gate.manual)
        self.assertFalse(gate.runnable)


class ExpectationTest(Sandbox):
    def test_slash_delimited_expect_is_a_regex(self):
        gate = self.one("- [ ] G1: one\n"
                        "  CHECK: x\n  EXPECT: /^ok-\\d+$/im\n")
        self.assertEqual(gate.expect.kind, gates.REGEX)
        self.assertEqual(gate.expect.flags, "im")

    def test_bare_expect_is_a_substring_not_a_pattern(self):
        # The defect this prevents: `EXPECT: .*` read as a pattern matches any
        # output at all, so the gate passes without deciding anything.
        gate = self.one("- [ ] G1: one\n  CHECK: x\n  EXPECT: .*\n")
        self.assertEqual(gate.expect.kind, gates.TEXT)
        matched, _ = gates.matches(gate.expect, "unrelated output")
        self.assertFalse(matched)
        matched, _ = gates.matches(gate.expect, "literally .* here")
        self.assertTrue(matched)

    def test_bad_regex_is_a_structural_error(self):
        bad = gates.parse("- [ ] G1: one\n  CHECK: x\n  EXPECT: /(unclosed/\n")
        self.assertIn("does not compile", " ".join(bad.errors))
        flag = gates.parse("- [ ] G1: one\n  CHECK: x\n  EXPECT: /ok/z\n")
        self.assertIn("unsupported flag", " ".join(flag.errors))
        long = gates.parse("- [ ] G1: one\n  CHECK: x\n"
                           f"  EXPECT: /{'a' * 1001}/\n")
        self.assertIn("exceeds 1000", " ".join(long.errors))

    def test_regex_match_runs_and_is_time_bounded(self):
        expect = gates.Expectation(gates.REGEX, r"^gate-\w+$", "m")
        matched, why = gates.matches(expect, "gate-ok\n")
        self.assertTrue(matched, why)
        matched, _ = gates.matches(expect, "nothing here\n")
        self.assertFalse(matched)

    def test_catastrophic_regex_is_killed_not_merely_reported(self):
        # A thread-based bound would return this verdict while the match kept a
        # core busy. The child process is what makes the bound real.
        expect = gates.Expectation(gates.REGEX, r"(a+)+$", "")
        matched, why = gates.matches(expect, "a" * 40 + "b")
        self.assertFalse(matched)
        self.assertIn("250ms", why)


class AbandonTest(Sandbox):
    def test_abandon_records_a_reason_on_the_named_gate(self):
        doc = gates.parse("- [ ] G1: needs a GPU\n"
                          "  CHECK: nvidia-smi\n  EXPECT: NVIDIA\n"
                          "ABANDON: G1 no GPU on this host\n")
        self.assertEqual(doc.errors, [])
        gate = doc.by_id("G1")
        self.assertEqual(gate.abandoned, "no GPU on this host")
        self.assertFalse(gate.runnable)

    def test_a_blank_reason_is_an_error(self):
        doc = gates.parse("- [ ] G1: one\n  CHECK: a\n  EXPECT: b\n"
                          "ABANDON: G1\n")
        self.assertIn("needs a non-blank reason", " ".join(doc.errors))

    def test_missing_id_duplicate_and_unknown_gate(self):
        missing = gates.parse("ABANDON:\n")
        self.assertIn("ABANDON needs a gate id and reason",
                      " ".join(missing.errors))
        dup = gates.parse("- [ ] G1: one\n  CHECK: a\n  EXPECT: b\n"
                          "ABANDON: G1 first\nABANDON: G1 second\n")
        self.assertIn("duplicate ABANDON for G1", " ".join(dup.errors))
        unknown = gates.parse("ABANDON: G9 never declared\n")
        self.assertIn("ABANDON names unknown gate G9", " ".join(unknown.errors))

    def test_abandoned_gate_is_reported_not_scored(self):
        text = ("- [ ] G1: one\n  CHECK: a\n  EXPECT: b\n"
                "ABANDON: G1 impossible here\n")
        gate = gates.parse(text).by_id("G1")
        verdict = gates.run_gate(gate, ledger=self.write(text), cwd=self.tmp)
        self.assertEqual(verdict["kind"], "abandoned")
        self.assertFalse(verdict["met"])
        self.assertEqual(verdict["reason"], "impossible here")


class ConjunctionTest(Sandbox):
    """met = checkbox AND exit 0 AND EXPECT. Each third alone must not pass."""

    def approve_and_run(self, text: str, gate_id: str = "G1") -> dict:
        path = self.write(text)
        gate = gates.parse(text).by_id(gate_id)
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        return gates.run_gate(gate, ledger=path, cwd=self.tmp)

    def test_all_three_present_is_met(self):
        verdict = self.approve_and_run(
            f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n")
        self.assertTrue(verdict["met"], verdict["reason"])
        self.assertEqual(verdict["exit_code"], 0)
        self.assertTrue(verdict["matched"])

    def test_exit_zero_without_a_match_is_not_met(self):
        verdict = self.approve_and_run(
            f"- [x] G1: ok\n  CHECK: {QUIET}\n  EXPECT: gate-ok\n")
        self.assertEqual(verdict["exit_code"], 0)
        self.assertFalse(verdict["met"])
        self.assertIn("EXPECT did not match", verdict["reason"])

    def test_a_match_with_a_nonzero_exit_is_not_met(self):
        verdict = self.approve_and_run(
            f"- [x] G1: ok\n  CHECK: {FAILING}\n  EXPECT: gate-ok\n")
        self.assertTrue(verdict["matched"])
        self.assertEqual(verdict["exit_code"], 3)
        self.assertFalse(verdict["met"])
        self.assertIn("exit 3", verdict["reason"])

    def test_an_unticked_box_is_not_met_however_well_it_runs(self):
        verdict = self.approve_and_run(
            f"- [ ] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n")
        self.assertEqual(verdict["exit_code"], 0)
        self.assertTrue(verdict["matched"])
        self.assertFalse(verdict["met"])
        self.assertIn("checkbox not marked", verdict["reason"])

    def test_a_manual_gate_is_never_scored_met(self):
        text = "- [x] M1: a human looked at it\n"
        gate = gates.parse(text).by_id("M1")
        verdict = gates.run_gate(gate, ledger=self.write(text), cwd=self.tmp)
        self.assertEqual(verdict["kind"], "manual")
        self.assertFalse(verdict["met"])


class ApprovalTest(Sandbox):
    TEXT = f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n"

    def gate(self, text: str | None = None) -> gates.Gate:
        return gates.parse(text or self.TEXT).by_id("G1")

    def test_unapproved_gate_does_not_execute(self):
        gate = self.gate()
        verdict = gates.run_gate(gate, ledger=self.write(self.TEXT),
                                 cwd=self.tmp)
        self.assertEqual(verdict["kind"], "unapproved")
        self.assertIsNone(verdict["exit_code"])
        self.assertFalse(verdict["met"])

    def test_approval_lands_outside_the_repository(self):
        path = self.write(self.TEXT)
        gate = self.gate()
        record_path = gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        self.assertTrue(record_path.startswith(self.approvals))
        self.assertFalse(record_path.startswith(REPO))
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["gate"], "G1")
        self.assertEqual(record["file"], os.path.abspath(path))
        self.assertEqual(record["schema"], gates.APPROVAL_SCHEMA)

    def test_changing_the_command_voids_the_approval(self):
        path = self.write(self.TEXT)
        gate = self.gate()
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        self.assertTrue(gates.is_approved(path, gate,
                                          gates.oracle(gate, cwd=self.tmp)))
        swapped = self.gate(f"- [x] G1: ok\n  CHECK: {QUIET}\n"
                            f"  EXPECT: gate-ok\n")
        self.assertFalse(gates.is_approved(path, swapped,
                                           gates.oracle(swapped, cwd=self.tmp)))

    def test_changing_the_expectation_voids_the_approval(self):
        path = self.write(self.TEXT)
        gate = self.gate()
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        swapped = self.gate(f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: other\n")
        self.assertFalse(gates.is_approved(path, swapped,
                                           gates.oracle(swapped, cwd=self.tmp)))

    def test_changing_cwd_or_path_voids_the_approval(self):
        path = self.write(self.TEXT)
        gate = self.gate()
        base = gates.oracle(gate, cwd=self.tmp)
        gates.approve(path, gate, base)
        elsewhere = gates.oracle(gate, cwd=REPO)
        self.assertFalse(gates.is_approved(path, gate, elsewhere))
        moved = dict(base, path=base["path"] + os.pathsep + "/nowhere")
        self.assertFalse(gates.is_approved(path, gate, moved))

    def test_an_approval_does_not_transfer_to_another_ledger(self):
        path = self.write(self.TEXT)
        gate = self.gate()
        oracle_doc = gates.oracle(gate, cwd=self.tmp)
        gates.approve(path, gate, oracle_doc)
        other = os.path.join(self.tmp, "OTHER.md")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write(self.TEXT)
        self.assertFalse(gates.is_approved(other, gate, oracle_doc))

    def test_signature_is_order_independent(self):
        gate = self.gate()
        doc = gates.oracle(gate, cwd=self.tmp)
        shuffled = dict(reversed(list(doc.items())))
        self.assertEqual(gates.signature(doc), gates.signature(shuffled))


class RobustnessTest(Sandbox):
    def test_undecodable_output_does_not_crash(self):
        # The measured failure this guards: `text=True` with no `encoding=`
        # raised UnicodeDecodeError under the console codepage on this machine.
        command = (f"{PY} -c \"import sys;"
                   f" sys.stdout.buffer.write(b'gate-ok\\xff\\xfe')\"")
        text = f"- [x] G1: bytes\n  CHECK: {command}\n  EXPECT: gate-ok\n"
        path = self.write(text)
        gate = gates.parse(text).by_id("G1")
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        verdict = gates.run_gate(gate, ledger=path, cwd=self.tmp)
        self.assertTrue(verdict["met"], verdict["reason"])

    def test_output_is_capped_in_bytes(self):
        command = f"{PY} -c \"print('gate-ok'); print('x' * 5000)\""
        text = f"- [x] G1: big\n  CHECK: {command}\n  EXPECT: gate-ok\n"
        path = self.write(text)
        gate = gates.parse(text).by_id("G1")
        oracle_doc = gates.oracle(gate, cwd=self.tmp, max_output_bytes=64)
        gates.approve(path, gate, oracle_doc)
        verdict = gates.run_gate(gate, ledger=path, cwd=self.tmp,
                                 max_output_bytes=64)
        self.assertTrue(verdict["truncated"])
        self.assertLessEqual(len(verdict["output"].encode("utf-8")), 64)

    def test_a_missing_cwd_fails_without_raising(self):
        missing = os.path.join(self.tmp, "nope")
        text = (f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n"
                f"  CWD: {missing}\n")
        path = self.write(text)
        gate = gates.parse(text).by_id("G1")
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        verdict = gates.run_gate(gate, ledger=path, cwd=self.tmp)
        self.assertFalse(verdict["met"])
        self.assertIn("CWD does not exist", verdict["reason"])

    def test_a_command_that_cannot_start_is_a_verdict_not_an_exception(self):
        text = ("- [x] G1: ok\n"
                "  CHECK: this-binary-does-not-exist-anywhere\n"
                "  EXPECT: gate-ok\n")
        path = self.write(text)
        gate = gates.parse(text).by_id("G1")
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        verdict = gates.run_gate(gate, ledger=path, cwd=self.tmp)
        self.assertFalse(verdict["met"])


class EvidenceTest(Sandbox):
    def test_evidence_line_is_rewritten_in_place(self):
        text = (f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n"
                f"  EVIDENCE: not yet run\n")
        path = self.write(text)
        gate = gates.parse(text).by_id("G1")
        gates.approve(path, gate, gates.oracle(gate, cwd=self.tmp))
        verdict = gates.run_gate(gate, ledger=path, cwd=self.tmp)
        updated = gates.apply_evidence(text, [verdict])
        self.assertIn("EVIDENCE: shell=", updated)
        self.assertIn("exit=0", updated)
        self.assertNotIn("not yet run", updated)
        # The rest of the ledger is untouched.
        self.assertEqual(len(updated.splitlines()), len(text.splitlines()))

    def test_a_gate_without_an_evidence_line_is_left_alone(self):
        text = f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n"
        verdict = {"id": "G1", "oracle": {}, "exit_code": 0, "matched": True,
                   "output": "gate-ok"}
        self.assertEqual(gates.apply_evidence(text, [verdict]), text)


class SummaryTest(Sandbox):
    def test_structural_errors_make_the_summary_not_ok(self):
        got = gates.summarise([], ["duplicate gate id G1"])
        self.assertFalse(got["ok"])

    def test_unapproved_gates_make_the_summary_not_ok(self):
        verdicts = [{"id": "G1", "kind": "unapproved", "met": False}]
        got = gates.summarise(verdicts, [])
        self.assertFalse(got["ok"])
        self.assertEqual(got["unapproved"], ["G1"])

    def test_all_met_with_no_errors_is_ok(self):
        verdicts = [{"id": "G1", "kind": "runnable", "met": True}]
        got = gates.summarise(verdicts, [])
        self.assertTrue(got["ok"])
        self.assertEqual(got["met"], 1)
        self.assertEqual(got["unmet"], [])

    def test_a_run_that_checked_nothing_is_not_ok(self):
        # Measured: `all()` over an empty list is True, so the naive summary
        # reported `ok` for a ledger where two unmet gates had been skipped.
        for verdicts in ([],
                         [{"id": "M1", "kind": "manual", "met": False}],
                         [{"id": "A1", "kind": "abandoned", "met": False}]):
            got = gates.summarise(verdicts, [])
            self.assertFalse(got["ok"], verdicts)
            self.assertTrue(got["nothing_ran"])


class VerifiedMarkerTest(Sandbox):
    """`verify` must skip only what THIS tool recorded, never author prose."""

    def gate_with(self, evidence: str) -> gates.Gate:
        text = (f"- [x] G1: ok\n  CHECK: {OK}\n  EXPECT: gate-ok\n"
                f"  EVIDENCE: {evidence}\n")
        return gates.parse(text).by_id("G1")

    def test_a_placeholder_is_not_a_verification(self):
        for placeholder in ("not yet run", "TODO", "looks fine to me", ""):
            self.assertFalse(gates.was_verified(self.gate_with(placeholder)),
                             placeholder)

    def test_a_machine_written_line_is_a_verification(self):
        verdict = {"id": "G1", "oracle": {"shell": "sh", "cwd": "/tmp"},
                   "exit_code": 0, "matched": True, "met": True,
                   "output": "gate-ok"}
        line = gates.evidence_line(verdict)
        self.assertTrue(line.startswith(gates.EVIDENCE_PREFIX))
        self.assertTrue(gates.was_verified(self.gate_with(line)))
        self.assertTrue(gates.recorded_met(self.gate_with(line)))

    def test_a_recorded_failure_does_not_exempt_itself(self):
        # Measured: evidence was written for a passing and a failing gate
        # alike, and the next verify skipped both. The failing one vanished
        # from the report it existed to block.
        verdict = {"id": "G1", "oracle": {"shell": "sh", "cwd": "/tmp"},
                   "exit_code": 0, "matched": False, "met": False,
                   "output": "unrelated"}
        gate = self.gate_with(gates.evidence_line(verdict))
        self.assertTrue(gates.was_verified(gate))
        self.assertFalse(gates.recorded_met(gate))

    def test_an_unmet_gate_is_named(self):
        verdicts = [{"id": "G1", "kind": "runnable", "met": True},
                    {"id": "G2", "kind": "runnable", "met": False}]
        got = gates.summarise(verdicts, [])
        self.assertFalse(got["ok"])
        self.assertEqual(got["unmet"], ["G2"])


if __name__ == "__main__":
    unittest.main()
