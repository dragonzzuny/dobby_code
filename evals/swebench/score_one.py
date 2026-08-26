"""Test-score one arm's surviving tree against the gold calibration.

Separate from `one_arm.py` because scoring is cheap and repeatable while running
an arm is neither: a scoring bug should not cost another set of provider calls.
Scores IN PLACE — the tree has already given up its token record to the journal
and is not needed afterwards — so it is destructive to that tree and idempotent
only in the sense that `git apply` of the same test patch twice will refuse.

    python evals/swebench/score_one.py <base> <out.json> <instance_id> <arm> <python>
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..")))

import local_resolve as LR  # noqa: E402
from dobby.swebench import fetch_instances  # noqa: E402


def calibration_path_for(instance_id: str) -> str:
    """Where the gold calibration for `instance_id` lives.

    One function so the writer and the reader cannot disagree. They did: the
    calibration was saved under a hand-built name and looked up under
    `instance_id.replace("__", "-")`, and five scoring runs reported a missing
    file that was sitting next to them.
    """
    return os.path.join(_HERE, f"CALIBRATION_{instance_id}.json")


def main(base: str, out: str, instance_id: str, arm: str, python: str) -> int:
    match = [i for i in fetch_instances(limit=200)
             if i["instance_id"] == instance_id]
    if not match:
        raise SystemExit(f"{instance_id!r} not in the first 200 instances")
    instance = match[0]

    calibration_path = calibration_path_for(instance_id)
    if not os.path.exists(calibration_path):
        raise SystemExit(f"no calibration at {calibration_path}; run the gold "
                         f"calibration before scoring any arm against it")
    with open(calibration_path, encoding="utf-8") as fh:
        calibration = json.load(fh)

    tree = os.path.join(base, f"{instance_id}__{arm}")
    if not os.path.isdir(tree):
        raise SystemExit(f"no surviving tree at {tree}")

    result = LR.score(instance, tree, calibration, python=python)
    result["arm"] = arm
    result["instance_id"] = instance_id

    existing = {}
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            existing = json.load(fh)
    # Keyed by instance AND arm. It was keyed by arm alone, so scoring a second
    # instance silently overwrote the first and a five-instance run left three
    # rows on disk — one per arm, all of them the last instance scored. The
    # console output was right and the record was wrong, which is the worse of
    # the two ways round.
    existing[f"{instance_id}::{arm}"] = result
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=1)

    print(f"{arm:<10} resolved_local={result['resolved_local']} "
          f"F2P={result['fail_to_pass_fixed']}/{result['fail_to_pass_total']} "
          f"fix_rate={result['fix_rate']} "
          f"regressions={result['regression_count']} ran={result['ran']}")
    if result["regressions"]:
        for name in result["regressions"][:5]:
            print("   regressed:", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:6]))
