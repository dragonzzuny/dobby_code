"""One arm, one instance, appended to a shared journal. Run in the foreground.

Written because four background runs in one session were killed mid-flight with
no output and no surviving process, and the cause is not visible from here. A
whole-pilot invocation loses everything the kill lands after; this loses one arm.
The journal is shared and append-only, so the arms accumulate into the same file
however many invocations it takes.

    python evals/swebench/one_arm.py <base> <journal.jsonl> <instance_id> <arm>
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner_arms as R  # noqa: E402
from dobby.swebench import fetch_instances, gold_files  # noqa: E402


def already_done(journal: str, instance_id: str, arm: str) -> bool:
    """A COMPLETED row is not paid for twice; reruns are resumable.

    A void row does not count. The first version treated any row as done, and a
    clone that failed on a leftover directory therefore blocked its own retry:
    the arm was recorded as spent without a provider ever being called. An
    absence and a failure are different things, and only one of them is a
    reason not to run again.
    """
    if not os.path.exists(journal):
        return False
    with open(journal, encoding="utf-8") as fh:
        lines = fh.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (row.get("instance_id") == instance_id and row.get("arm") == arm
                and not row.get("void")):
            return True
    return False


def main(base: str, journal: str, instance_id: str, arm: str) -> int:
    if arm not in R.ARMS:
        raise SystemExit(f"unknown arm {arm!r}; known: {list(R.ARMS)}")
    if already_done(journal, instance_id, arm):
        print(f"{instance_id} :: {arm} already in {journal}; nothing spent")
        return 0

    pool = fetch_instances(limit=200)
    match = [i for i in pool if i["instance_id"] == instance_id]
    if not match:
        raise SystemExit(f"{instance_id!r} not in the first 200 instances")
    instance = match[0]

    os.makedirs(base, exist_ok=True)
    cache = os.path.join(base, "_templates")
    os.makedirs(cache, exist_ok=True)

    print(f"{instance_id} :: {arm}  ({len(gold_files(instance['patch']))} gold "
          f"file(s))", flush=True)
    started = time.monotonic()
    try:
        row = R.run_arm(instance, base, arm, cache)
    except Exception as exc:                       # noqa: BLE001
        row = {"instance_id": instance_id, "arm": arm, "void": True,
               "note": f"{type(exc).__name__}: {exc}"[:300]}
    with open(journal, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    axes = row.get("axes") or {}
    print(f"  -> state={row.get('state')} recall={row.get('recall')} "
          f"precision={row.get('precision')} extra={row.get('extra_file_count')} "
          f"calls={row.get('calls_total')} tokens={axes.get('total')} "
          f"{time.monotonic() - started:.0f}s", flush=True)
    if row.get("note"):
        print(f"  note: {str(row['note'])[:200]}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:5]))
