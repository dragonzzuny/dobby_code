"""D_dobby alone, twelve tasks, so the `provider="codex"` fix can be priced
before another 48-run is bought.

The full runner has no arm filter and adding one would edit a file this session
did not write. This driver reuses `run_arm` unchanged and journals every row the
moment it exists, for the reason recorded in runner_s1s2.run(): a lost
aggregation once cost an hour of real provider calls.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runner_s1s2 import ARM_DOBBY, mark_void_solo, run_arm  # noqa: E402


def main(base: str, out: str) -> None:
    from corpus_s1_s2 import corpus

    tasks = corpus()
    os.makedirs(base, exist_ok=True)
    journal = out.replace(".json", ".jsonl")

    rows = []
    for index, task in enumerate(tasks, start=1):
        print(f"[{index}/{len(tasks)}] {task.task_id} :: {ARM_DOBBY}",
              flush=True)
        started = time.monotonic()
        try:
            row = run_arm(task, base, ARM_DOBBY)
        except Exception as exc:                  # noqa: BLE001
            row = {"task_id": task.task_id, "arm": ARM_DOBBY, "void": True,
                   "note": f"{type(exc).__name__}: {exc}"[:300]}
        rows.append(row)
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        print(f"    -> verified={row.get('verified')} "
              f"state={row.get('state')} calls={row.get('claude_calls')} "
              f"planned_by={row.get('planned_by')} "
              f"{time.monotonic() - started:.0f}s", flush=True)

    rows = mark_void_solo(rows)
    verified = sum(1 for r in rows if r.get("verified"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"arm": ARM_DOBBY, "rows": rows,
                   "verified": verified, "total": len(rows)},
                  fh, ensure_ascii=False, indent=1, default=str)
    print(f"WROTE {out}  verified={verified}/{len(rows)}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
