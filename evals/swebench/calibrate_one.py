"""Measure what the GOLD patch achieves on THIS machine, for one instance.

Every arm is scored against this ceiling and never against the published
`resolved`. Docker is absent here (re-measured 2026-08-26), so the environment
is django's own `tests/runtests.py` under whatever interpreter is passed in, and
a test the gold patch cannot make pass here is excluded from scoring rather than
charged to an agent that also could not.

Separate from `score_one.py` for the same reason `score_one.py` is separate from
`one_arm.py`: the calibration is the standard, so it is produced once, written
to disk, and read by every later scoring run. A standard recomputed inside each
scoring run is a standard that can drift between arms.

    python evals/swebench/calibrate_one.py <cache_dir> <work_dir> <instance_id> <python>

Writes `CALIBRATION_<instance_id>.json` next to this file, and REFUSES to
overwrite one that already exists — a calibration rewritten after arms have been
scored against it is how a ceiling gets fitted to a result.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

import local_resolve as LR  # noqa: E402
import runner_arms as RA  # noqa: E402
from dobby.swebench import fetch_instances  # noqa: E402
from score_one import calibration_path_for  # noqa: E402


def main(cache: str, work: str, instance_id: str, python: str) -> int:
    out = calibration_path_for(instance_id)
    if os.path.exists(out):
        print(f"{instance_id}: calibration already at {out}; refusing to "
              f"overwrite. Delete it deliberately if it must be redone.")
        return 0

    match = [i for i in fetch_instances(limit=200)
             if i["instance_id"] == instance_id]
    if not match:
        raise SystemExit(f"{instance_id!r} not in the first 200 instances")
    instance = match[0]

    os.makedirs(work, exist_ok=True)
    template = RA.instance_template(instance, cache)
    repo = RA.fresh_clone(template, os.path.join(work, f"cal__{instance_id}"))

    result = LR.calibrate(instance, repo, python=python)
    result["instance_id"] = instance_id
    result["python"] = python
    result["django_version"] = instance.get("version")

    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    print(f"{instance_id}  django {result['django_version']}  "
          f"ran={result['ran']} crashed={result['crashed']}")
    print(f"  fail_to_pass {len(result['fail_to_pass'])} -> achievable here "
          f"{len(result['fail_to_pass_achievable'])}")
    print(f"  broken by the environment (excluded): "
          f"{len(result['failing_with_gold'])}")
    if result["crashed"] or not result["fail_to_pass_achievable"]:
        print("  -> nothing scoreable here; this instance is VOID for every arm")
        for line in result["tail"][-4:]:
            print(f"     {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
