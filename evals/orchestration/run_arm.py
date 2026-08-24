"""One arm over a scenario subset, one scenario at a time, resumable.

`integration/task.py` shells out to `uv`, which is not installed here, and it
runs all 219 EN plus 222 KO scenarios in one invocation. This drives the same
worker — `src/stepwise_scenario_processor.py` — per scenario instead. Four
background runs were killed in the session that wrote this; per-scenario means a
kill costs one scenario, and a rerun skips whatever already landed.

    python evals/orchestration/run_arm.py <checkout> <venv-python> <arm> <out> [n]

`arm` is a key from install.ARMS. `n` is how many scenarios, lowest id first, so
two arms asked for the same n get the same set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOBBY_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def scenario_ids(checkout: str, limit: int, language: str = "EN") -> list[str]:
    """Lowest numeric id first. Deterministic, so arms are comparable."""
    folder = os.path.join(checkout, "data", language, "scenario_data")
    ids = []
    for name in os.listdir(folder):
        stem, ext = os.path.splitext(name)
        if ext == ".yaml" and stem.isdigit():
            ids.append(stem)
    return sorted(ids, key=int)[:limit]


def already_done(out_dir: str, arm: str, scenario: str) -> bool:
    """A scenario whose output exists is not paid for twice."""
    path = os.path.join(out_dir, arm, f"{scenario}_out.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(json.load(fh).get("history"))
    except (OSError, ValueError):
        return False


def run_one(checkout: str, python: str, arm: str, scenario: str, out_dir: str,
            language: str = "EN", timeout_s: int = 1800) -> dict:
    env = dict(os.environ)
    env["DOBBY_ROOT"] = DOBBY_ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("DOBBY_SPEND_DIR", os.path.join(out_dir, "_spend"))

    data = os.path.join(checkout, "data", language)
    cmd = [
        python, "src/stepwise_scenario_processor.py",
        "--model", arm,
        "--agent-cards", os.path.join(data, "multiagent_cards"),
        # ABSOLUTE. `data_loader` resolves a relative pattern against the config
        # file's PARENT directory, so `data/EN/...` became `config/data/EN/...`
        # and matched nothing — silently, as a warning and an empty run.
        "--data-path", os.path.join(data, "scenario_data", f"{scenario}.yaml"),
        "--max-scenarios", "1",
        "--num-iter", "1",
        "--batch-size", "1",
        "--max-retries", "1",
        "--output-dir", out_dir,
        "--log-level", "WARNING",
    ]
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=checkout, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          timeout=timeout_s)
    return {"scenario": scenario, "arm": arm,
            "returncode": proc.returncode,
            "wall_s": round(time.monotonic() - started, 1),
            "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:]}


def usage_of(out_dir: str, arm: str, scenario: str) -> dict:
    """The processor's own usage block, with its cost REMOVED.

    `pricing.py` carries fallback prices for models it does not know and knows
    none of ours, so its `total_cost` is a rate nobody chose applied to tokens
    somebody did. The tokens are real and are kept; the dollar figure is dropped
    and dobby's ledger holds the vendor's own instead.
    """
    path = os.path.join(out_dir, arm, f"{scenario}_out.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            usage = dict((json.load(fh).get("usage") or {}))
    except (OSError, ValueError):
        return {}
    usage.pop("total_cost", None)
    usage["cost_note"] = ("benchmark cost dropped: pricing.py has no rate for "
                          "this model and falls back to one nobody chose")
    return usage


def main(checkout: str, python: str, arm: str, out_dir: str,
         limit: int = 20) -> int:
    checkout, out_dir = os.path.abspath(checkout), os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    journal = os.path.join(out_dir, f"{arm}_runs.jsonl")

    ids = scenario_ids(checkout, limit)
    print(f"{arm}: {len(ids)} scenario(s) — {', '.join(ids)}", flush=True)

    for index, scenario in enumerate(ids, start=1):
        if already_done(out_dir, arm, scenario):
            print(f"[{index}/{len(ids)}] {scenario} already done", flush=True)
            continue
        print(f"[{index}/{len(ids)}] {scenario} ...", end=" ", flush=True)
        try:
            row = run_one(checkout, python, arm, scenario, out_dir)
        except subprocess.TimeoutExpired:
            row = {"scenario": scenario, "arm": arm, "returncode": None,
                   "wall_s": None, "stderr_tail": ["timeout"]}
        row["usage"] = usage_of(out_dir, arm, scenario)
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tokens = (row["usage"] or {}).get("total_tokens")
        print(f"rc={row['returncode']} {row['wall_s']}s tokens={tokens}",
              flush=True)
        if row["returncode"] not in (0, None) and row["stderr_tail"]:
            print("     " + " | ".join(row["stderr_tail"])[:200], flush=True)
    print(f"journal: {journal}", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 5:
        raise SystemExit(__doc__.strip().splitlines()[8].strip())
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4],
                          int(sys.argv[5]) if len(sys.argv) > 5 else 20))
