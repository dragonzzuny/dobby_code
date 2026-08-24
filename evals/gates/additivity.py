"""Did the revision LOSE anything? Answered from runs already on disk.

    python evals/gates/additivity.py

The approver's objection, verbatim:

    The superseded additivity checks also leave no passing evidence that prior
    protocol instructions remain intact... restore an executable comparison
    proving the revision preserves every prior obligation while adding gate
    routing and approval-before-verification.

The checks it superseded were substring greps over `SKILL.md`, which the same
reviewer had already rejected for measuring the document rather than the
behaviour. So this does not restore them. It derives the same guarantee from the
arms that were already run:

    for every check the CONTROL arm passes, the TREATMENT arm must pass it too

The control is the protocol as it stood before the revision, on the same task,
in the same sandbox shape, graded by the same checks. A check the old protocol
satisfied and the new one does not is an obligation the revision dropped, and it
is named. Nothing is inferred from the text of either document.

What this cannot show is stated rather than glossed: a prior obligation that no
check in this suite measures is invisible here, exactly as it was invisible to
the grep it replaces. The claim is bounded to the checks that exist.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
sys.path.insert(0, HERE)

from run_behavioral import CONTROL, TREATMENT, SCENARIOS  # noqa: E402


def load(scenario: str, provider: str, arm: str):
    path = os.path.join(RUNS, f"{scenario}__{provider}__{arm}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def passed_checks(row) -> dict:
    if row is None:
        return {}
    return {c["id"]: bool(c["passed"]) for c in row.get("checks", [])}


def main() -> int:
    providers = sorted({name.split("__")[1] for name in os.listdir(RUNS)
                        if name.endswith(".json") and name.count("__") == 2})
    rows, regressions = [], []

    for scenario in SCENARIOS:
        for provider in providers:
            control = load(scenario, provider, CONTROL)
            treatment = load(scenario, provider, TREATMENT)
            if control is None or treatment is None:
                rows.append({"scenario": scenario, "provider": provider,
                             "comparable": False,
                             "why": "one arm missing"})
                continue
            before, after = passed_checks(control), passed_checks(treatment)
            shared = sorted(set(before) & set(after))
            lost = [k for k in shared if before[k] and not after[k]]
            gained = [k for k in shared if after[k] and not before[k]]
            rows.append({"scenario": scenario, "provider": provider,
                         "comparable": True,
                         "checks_compared": len(shared),
                         "control_passed": sorted(k for k in shared if before[k]),
                         "lost_by_the_revision": lost,
                         "gained_by_the_revision": gained})
            if lost:
                regressions.append((scenario, provider, lost))

    for row in rows:
        if not row["comparable"]:
            print(f"{row['scenario']:<34} {row['provider']:<8} "
                  f"not comparable: {row['why']}")
            continue
        verdict = "LOST " + str(row["lost_by_the_revision"]) \
            if row["lost_by_the_revision"] else "nothing lost"
        print(f"{row['scenario']:<34} {row['provider']:<8} "
              f"{len(row['control_passed'])}/{row['checks_compared']} passed "
              f"before, +{len(row['gained_by_the_revision'])} gained, "
              f"{verdict}")

    target = os.path.join(HERE, "ADDITIVITY.json")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"note": ("for every check the pre-revision protocol passed, "
                            "the revised one must pass it too; derived from the "
                            "control and treatment arms already run"),
                   "bounded_by": ("a prior obligation no check in this suite "
                                  "measures is invisible here"),
                   "regressions": [{"scenario": s, "provider": p, "lost": l}
                                   for s, p, l in regressions],
                   "rows": rows}, fh, ensure_ascii=False, indent=1)
    print()
    if regressions:
        print(f"REGRESSION: the revision lost {len(regressions)} check(s) the "
              f"old protocol passed")
    else:
        print("No check passed by the pre-revision protocol is failed by the "
              "revision.")
    print(f"wrote {target}")
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
