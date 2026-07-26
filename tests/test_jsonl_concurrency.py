"""Concurrent append-only writing: the defect every ledger in the kit shared.

Every record this kit keeps is JSONL — the spend ledger, the session trajectory,
the MCP audit log, promoted improvements — and all of them used the obvious
idiom, `open(path, "a")` plus `f.write(...)`. Two measurements:

- 10 threads x 200 appends -> **1751 lines, 2 unparseable**. Buffers flush at
  size boundaries that have nothing to do with record boundaries, so one
  writer's half-line lands inside another's.
- 6 processes x 300 appends -> **1519 of 1800 lines, zero corruption**. Records
  lost rather than torn: the Windows CRT implements O_APPEND as seek-then-write,
  so two processes resolve the same end offset and one overwrites the other.

The second number is the one that surprises: no corruption at all, and a third
of the data gone. A test that only checked "is every line valid JSON" would have
passed.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from dobby.core.jsonl import (MAX_RECORD_BYTES, RecordTooLarge, append_jsonl,
                              read_jsonl)
from dobby.core.platform import child_env
from dobby.core.trajectory import Trajectory
from dobby.spend import SPEND_FILE, record


def audit(path, expected):
    """(lines, parseable, corrupt) for a JSONL file."""
    with open(path, encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    good = corrupt = 0
    for line in lines:
        try:
            json.loads(line)
            good += 1
        except json.JSONDecodeError:
            corrupt += 1
    return len(lines), good, corrupt


class TestSingleWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "log.jsonl")

    def test_creates_parent_directories(self):
        append_jsonl(self.path, {"a": 1})
        self.assertTrue(os.path.exists(self.path))

    def test_one_line_per_record(self):
        for i in range(5):
            append_jsonl(self.path, {"i": i})
        _, good, corrupt = audit(self.path, 5)
        self.assertEqual((good, corrupt), (5, 0))

    def test_non_ascii_survives(self):
        append_jsonl(self.path, {"k": "압축 누수 — 圧縮 — тест"})
        records, skipped = read_jsonl(self.path)
        self.assertEqual(records[0]["k"], "압축 누수 — 圧縮 — тест")
        self.assertEqual(skipped, 0)

    def test_newlines_inside_a_value_do_not_split_the_record(self):
        append_jsonl(self.path, {"body": "line1\nline2\nline3"})
        lines, good, corrupt = audit(self.path, 1)
        self.assertEqual((lines, good, corrupt), (1, 1, 0))

    def test_oversized_record_is_refused_not_torn(self):
        with self.assertRaises(RecordTooLarge) as ctx:
            append_jsonl(self.path, {"big": "x" * (MAX_RECORD_BYTES + 10)})
        self.assertIn("handle in the record", str(ctx.exception))

    def test_unserializable_value_falls_back_to_str(self):
        append_jsonl(self.path, {"when": object()})
        records, _ = read_jsonl(self.path)
        self.assertIsInstance(records[0]["when"], str)


class TestReadSkipsAndCounts(unittest.TestCase):
    def test_corrupt_lines_are_counted_not_hidden(self):
        """A caller must be able to say '412 of 414 rows', not present 412."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "log.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"ok":1}\n')
            f.write("{not json\n")
            f.write('{"ok":2}\n')
            f.write("[1,2,3]\n")          # valid JSON, wrong shape
        records, skipped = read_jsonl(path)
        self.assertEqual(len(records), 2)
        self.assertEqual(skipped, 2)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(read_jsonl("/definitely/not/here.jsonl"), ([], 0))

    def test_tail_window(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "log.jsonl")
        for i in range(50):
            append_jsonl(path, {"i": i})
        records, _ = read_jsonl(path, limit=5, tail=True)
        self.assertEqual([r["i"] for r in records], [45, 46, 47, 48, 49])


class TestThreadedWriters(unittest.TestCase):
    """10 threads x 200 appends lost 249 lines and corrupted 2 before the fix."""

    def _hammer(self, write_one, expected):
        threads = [threading.Thread(target=write_one, args=(i,))
                   for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return expected

    def test_raw_append_jsonl(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "log.jsonl")

        def write_one(i):
            for k in range(200):
                append_jsonl(path, {"t": i, "k": k, "pad": "x" * 60})

        self._hammer(write_one, 2000)
        lines, good, corrupt = audit(path, 2000)
        self.assertEqual(corrupt, 0, "interleaved writes corrupted records")
        self.assertEqual(good, 2000, f"records lost: {good} of 2000")

    def test_spend_ledger(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def write_one(i):
            for _ in range(200):
                record(tmp.name, provider=f"p{i}", duration_s=1.0, ok=True,
                       label="x" * 40)

        self._hammer(write_one, 2000)
        lines, good, corrupt = audit(os.path.join(tmp.name, SPEND_FILE), 2000)
        self.assertEqual((good, corrupt), (2000, 0))

    def test_trajectory(self):
        """A torn line here is a lost DECISION on session resume."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        traj = Trajectory(tmp.name, "concurrent task")

        def write_one(i):
            for k in range(100):
                traj.append("execute", {"thread": i, "k": k, "pad": "y" * 60})

        threads = [threading.Thread(target=write_one, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines, good, corrupt = audit(traj.path, 801)
        self.assertEqual(corrupt, 0)
        self.assertEqual(good, 801, "task_start plus 800 events")


WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[3])
    from dobby.core.jsonl import append_jsonl
    tag, path = sys.argv[1], sys.argv[2]
    for i in range(300):
        append_jsonl(path, {"tag": tag, "i": i, "pad": "z" * 60})
""")


class TestMultiProcessWriters(unittest.TestCase):
    """The case an in-process lock cannot cover.

    On Windows `O_APPEND` is seek-then-write, not atomic, so two processes
    resolve the same end offset and one overwrites the other. Before the
    advisory file lock: 1519 of 1800 lines, zero corruption — the loss signature
    of an overwrite rather than an interleave.
    """

    def test_six_processes_lose_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        writer = os.path.join(tmp.name, "writer.py")
        with open(writer, "w", encoding="utf-8") as f:
            f.write(WRITER)
        target = os.path.join(tmp.name, "log.jsonl")

        procs = [subprocess.Popen(
            [sys.executable, writer, f"proc{i}", target, REPO],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=child_env()) for i in range(6)]
        errors = []
        for p in procs:
            _, err = p.communicate(timeout=300)
            if p.returncode != 0:
                errors.append((err or b"").decode("utf-8", "replace")[-300:])
        self.assertEqual(errors, [], "writer process failed")

        lines, good, corrupt = audit(target, 1800)
        self.assertEqual(corrupt, 0, "records torn across processes")
        self.assertEqual(good, 1800, f"records lost: {good} of 1800")

    def test_every_writer_is_represented(self):
        """Loss was uniform-looking; check no single writer was erased."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        writer = os.path.join(tmp.name, "writer.py")
        with open(writer, "w", encoding="utf-8") as f:
            f.write(WRITER)
        target = os.path.join(tmp.name, "log.jsonl")
        procs = [subprocess.Popen(
            [sys.executable, writer, f"proc{i}", target, REPO],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=child_env()) for i in range(4)]
        for p in procs:
            p.communicate(timeout=300)
        records, skipped = read_jsonl(target)
        self.assertEqual(skipped, 0)
        by_tag = {}
        for r in records:
            by_tag[r["tag"]] = by_tag.get(r["tag"], 0) + 1
        self.assertEqual(sorted(by_tag), [f"proc{i}" for i in range(4)])
        for tag, n in by_tag.items():
            self.assertEqual(n, 300, f"{tag} wrote {n} of 300")


if __name__ == "__main__":
    unittest.main()
