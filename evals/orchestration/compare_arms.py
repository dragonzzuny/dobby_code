"""Score each arm's outputs and put the arms side by side.

Scoring is `src/evaluation.py --skip-llm-eval`, which is the benchmark's own
judge-free path: `Plan` comes from a networkx graph edit distance and
`Call Rejection` from a confusion matrix, neither of which calls a model. `FC`
is reported but its `arguments_value` component is the part a judge would score,
so the number here is not the published leaderboard's FC and is labelled.

Tokens come from the processor's own `usage` block. Cost does NOT: `pricing.py`
carries fallback rates for models it does not know and knows none of the ones
these arms run, so its dollar figure is a rate nobody chose applied to real
tokens. dobby's ledger holds the vendor's own figure and is read instead when it
is present.

    python evals/orchestration/compare_arms.py <checkout> <venv-python> <out> \
        <arm> [<arm> ...]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOBBY_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

#: Reported by kakao/OrchestrationBench's README over all 219 EN scenarios, with
#: its LLM judge enabled and each model reached through its vendor API. Carried
#: here as a REFERENCE, never as this run's baseline: a different model
#: generation, a different access path, and a different scoring mode are three
#: confounds at once, and `docs/EVAL_DESIGN.md` cites the paper that calls such
#: a comparison invalid. `Plan` and `Call Rejection` are judge-free on both
#: sides and are the only columns worth looking at across the line.
PUBLISHED_EN = {
    "claude-opus-4-7 (Bedrock)": {"Plan": 83.42, "CallRejection": 80.38,
                                  "FC": 88.38, "Average": 84.06},
    "gpt-5.4-2026-03-05": {"Plan": 76.52, "CallRejection": 72.74,
                           "FC": 81.77, "Average": 77.01},
    "gemini-3-flash-preview": {"Plan": 83.59, "CallRejection": 85.77,
                               "FC": 82.88, "Average": 84.08},
}


def score_arm(checkout: str, python: str, out_dir: str, arm: str) -> dict:
    arm_dir = os.path.join(out_dir, arm)
    if not os.path.isdir(arm_dir):
        return {"error": f"no outputs at {arm_dir}"}
    target = os.path.join(out_dir, f"{arm}_score.json")
    env = dict(os.environ)
    env["DOBBY_ROOT"] = DOBBY_ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [python, "src/evaluation.py",
         "--input", arm_dir,
         "--agent-cards-path", os.path.join(checkout, "data", "EN",
                                            "multiagent_cards"),
         "--eval-config", os.path.join(checkout, "config", "base_config",
                                       "eval_config.yaml"),
         "--output", target,
         "--skip-llm-eval", "--sequential"],
        cwd=checkout, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=3600)
    if not os.path.exists(target):
        return {"error": f"evaluation produced nothing; rc={proc.returncode}",
                "stderr_tail": (proc.stderr or "").strip().splitlines()[-5:]}
    with open(target, encoding="utf-8") as fh:
        return json.load(fh)


def tokens_of(out_dir: str, arm: str) -> dict:
    """Summed over the arm's scenarios. A scenario that produced nothing is
    counted as attempted and not as zero-cost."""
    arm_dir = os.path.join(out_dir, arm)
    total = {"scenarios": 0, "measured": 0, "calls": 0,
             "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    if not os.path.isdir(arm_dir):
        return total
    for name in sorted(os.listdir(arm_dir)):
        if not name.endswith("_out.json"):
            continue
        total["scenarios"] += 1
        try:
            with open(os.path.join(arm_dir, name), encoding="utf-8") as fh:
                usage = (json.load(fh).get("usage") or {})
        except (OSError, ValueError):
            continue
        if not usage:
            continue
        total["measured"] += 1
        total["calls"] += int(usage.get("total_calls") or 0)
        total["input_tokens"] += int(usage.get("total_input_tokens") or 0)
        total["output_tokens"] += int(usage.get("total_output_tokens") or 0)
        total["total_tokens"] += int(usage.get("total_tokens") or 0)
    total["complete"] = total["measured"] == total["scenarios"]
    return total


def dobby_cost(out_dir: str) -> dict:
    """The VENDOR's own figures, from dobby's ledger. Empty when it was not on."""
    sink = os.path.join(out_dir, "_spend")
    if not os.path.isdir(sink):
        return {}
    sys.path.insert(0, DOBBY_ROOT)
    from dobby.spend import summarize

    got = summarize(sink, window_s=None)
    return {
        "calls": got.get("calls"),
        "tokens": got.get("tokens"),
        "cost_usd_reported": got.get("cost_usd_reported"),
        "dollars_complete": got.get("dollars_complete"),
        "providers": {p: {"calls": r["calls"], "tokens": r["tokens"],
                          "cost_usd": r["cost_usd"], "models": r["models"]}
                      for p, r in (got.get("providers") or {}).items()},
    }


def key_metrics(score: dict) -> dict:
    """The four headline numbers, wherever `evaluation.py` put them.

    It nests them: `overall_statistics.key_metrics`, not the top level. A first
    version searched only the top level, found nothing, and the table printed
    0.00 across every column for both arms — a scoring failure wearing the
    costume of a result. `Plan` in the dict is the thing that identifies it, so
    that is what the walk looks for rather than a path that can move again.
    """
    stack = [score]
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        if "Plan" in node:
            return node
        for key in ("key_metrics", "metrics"):
            if isinstance(node.get(key), dict):
                stack.insert(0, node[key])
        stack.extend(v for v in node.values() if isinstance(v, dict))
    return {}


def main(checkout: str, python: str, out_dir: str, *arms: str) -> int:
    checkout, out_dir = os.path.abspath(checkout), os.path.abspath(out_dir)
    rows = {}
    for arm in arms:
        print(f"scoring {arm} ...", flush=True)
        score = score_arm(checkout, python, out_dir, arm)
        rows[arm] = {"metrics": key_metrics(score), "tokens": tokens_of(out_dir, arm),
                     "error": score.get("error"), "raw": score}

    ledger = dobby_cost(out_dir)

    print()
    head = f"{'arm':<16}{'Plan':>8}{'CallRej':>9}{'FC*':>8}{'Avg':>8}{'scen':>6}{'calls':>7}{'tokens':>12}"
    print(head)
    print("-" * len(head))
    for arm, row in rows.items():
        m, t = row["metrics"], row["tokens"]
        if row["error"]:
            print(f"{arm:<16}  {row['error'][:60]}")
            continue
        print(f"{arm:<16}{m.get('Plan', 0) * 100:>8.2f}"
              f"{m.get('Call Rejection Classification Accuracy', 0) * 100:>9.2f}"
              f"{m.get('FC', 0) * 100:>8.2f}{m.get('Average', 0) * 100:>8.2f}"
              f"{t['scenarios']:>6}{t['calls']:>7}{t['total_tokens']:>12,}")

    print()
    print("published EN reference — different model generation, different access")
    print("path (vendor API, not this CLI), judge ENABLED. Plan and CallRej are")
    print("judge-free on both sides; FC is not comparable.")
    print("-" * len(head))
    for name, m in PUBLISHED_EN.items():
        print(f"{name:<16.16}{m['Plan']:>8.2f}{m['CallRejection']:>9.2f}"
              f"{m['FC']:>8.2f}{m['Average']:>8.2f}{'219':>6}")

    if ledger:
        print()
        print("dobby ledger — the providers' OWN cost figures")
        for provider, row in (ledger.get("providers") or {}).items():
            cost = ("—" if row["cost_usd"] is None else f"${row['cost_usd']:.2f}")
            print(f"  {provider:<8} {row['calls']:>4} calls  "
                  f"{row['tokens']:>12,} tokens  {cost:>8}  {row['models']}")
        if not ledger.get("dollars_complete"):
            print("  (a provider billing by subscription reports no dollars; "
                  "the total covers the metered ones only)")

    target = os.path.join(out_dir, "COMPARISON.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump({"arms": {a: {"metrics": r["metrics"], "tokens": r["tokens"],
                                "error": r["error"]} for a, r in rows.items()},
                   "dobby_ledger": ledger,
                   "published_en_reference": PUBLISHED_EN,
                   "note": ("FC here excludes the LLM-judged argument-value "
                            "component; the published FC includes it")},
                  fh, ensure_ascii=False, indent=1)
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit(__doc__.strip().splitlines()[-2].strip())
    raise SystemExit(main(*sys.argv[1:]))
