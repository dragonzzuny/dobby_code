"""Grasshopper-inspired population optimizer over harness configurations,
with honest baselines (random search, hill-climb).

Search unit: a retrieval/context-policy configuration vector (the weights of
dobby/core/kg.py retrieval + context size k). Fitness is MEASURABLE ON THIS
MACHINE with no LLM: gold-labeled context-retrieval quality on eval scenarios
(evals/retrieval_gold.yaml) minus a token-cost penalty. End-to-end task-success
fitness requires model runs and is explicitly out of scope here (ledger A4).

GOA mechanics (Saremi & Mirjalili 2017):
  social force  s(r) = f*exp(-r/l) - exp(-r)      (f=0.5, l=1.5)
  position      x_i <- c * sum_j c*(ub-lb)/2 * s(|xj-xi|) * unit(xj-xi) + T
  coefficient   c decreases cmax -> cmin  (exploration -> exploitation)
The published evidence for GOA *specifically* beating generic population
search is weak (metaphor-heavy metaheuristics critique — see research
matrix); therefore this module also implements random search and hill-climb,
and reports all three. GOA is retained only if it wins on this fitness.

Anti-gaming: the optimizer reads gold labels but can only change config
vectors; it cannot edit gold, scenarios, or criteria (no write path exists).
Holdout scenarios are excluded from fitness during search and evaluated once
at the end.
"""

from __future__ import annotations

import json
import math
import random

from .kg import KnowledgeGraph

# search space: (name, low, high)
SPACE = [
    ("lexical", 0.0, 3.0),
    ("graph", 0.0, 2.0),
    ("authority", 0.0, 1.5),
    ("recency", 0.0, 1.0),
    ("unverified_penalty", 0.0, 1.0),
    ("keyword_bonus", 0.0, 3.0),
    ("context_k", 3.0, 15.0),      # rounded to int at use
]
DIM = len(SPACE)


def vec_to_config(x: list[float]) -> dict:
    cfg = {name: max(lo, min(hi, v))
           for (name, lo, hi), v in zip(SPACE, x)}
    cfg["context_k"] = int(round(cfg["context_k"]))
    return cfg


class RetrievalFitness:
    """recall@k of gold nodes - alpha * (retrieved token cost / budget)."""

    def __init__(self, kg: KnowledgeGraph, gold: dict, alpha: float = 0.15,
                 token_budget: int = 1500):
        self.kg = kg
        self.gold = gold            # {split: [{id, task, required_nodes}]}
        self.alpha = alpha
        self.token_budget = token_budget

    def __call__(self, x_or_cfg, split: str = "dev") -> dict:
        cfg = x_or_cfg if isinstance(x_or_cfg, dict) else vec_to_config(x_or_cfg)
        k = cfg.get("context_k", 8)
        weights = {kk: v for kk, v in cfg.items() if kk != "context_k"}
        per_case, total = {}, 0.0
        cases = self.gold.get(split, [])
        for case in cases:
            pack = self.kg.context_pack(case["task"], weights=weights, k=k,
                                        token_budget=self.token_budget)
            got = {item["id"] for item in pack["items"]}
            req = set(case["required_nodes"])
            recall = len(got & req) / len(req) if req else 1.0
            cost_pen = self.alpha * (pack["approx_tokens"] / self.token_budget)
            score = recall - cost_pen
            per_case[case["id"]] = round(score, 4)
            total += score
        n = max(1, len(cases))
        return {"score": total / n, "per_case": per_case,
                "mean_recall_proxy": total / n, "split": split, "config": cfg}


# ---------------------------------------------------------------- GOA ------
def _s(r: float, f: float = 0.5, l: float = 1.5) -> float:
    return f * math.exp(-r / l) - math.exp(-r)


def goa_optimize(fitness, pop_size: int = 12, iters: int = 30, seed: int = 0,
                 cmax: float = 1.0, cmin: float = 0.00004,
                 diversity_eps: float = 0.05) -> dict:
    rng = random.Random(seed)
    lows = [lo for _, lo, _ in SPACE]
    highs = [hi for _, _, hi in SPACE]

    def clamp(x):
        return [max(lo, min(hi, v)) for v, lo, hi in zip(x, lows, highs)]

    def rand_vec():
        return [rng.uniform(lo, hi) for lo, hi in zip(lows, highs)]

    # diverse init: random + corner-ish seeds (mission §7.2: not paraphrases)
    pop = [rand_vec() for _ in range(pop_size - 2)]
    pop.append([hi for hi in highs])            # everything-on
    pop.append([lo if i > 0 else hi for i, (nm, lo, hi) in enumerate(SPACE)])  # lexical-only
    scores = [fitness(x)["score"] for x in pop]
    best_i = max(range(pop_size), key=lambda i: scores[i])
    best_x, best_s = pop[best_i][:], scores[best_i]
    elite_archive = [(best_s, best_x[:])]
    history = [best_s]
    evals = pop_size

    for it in range(iters):
        c = cmax - (it + 1) * (cmax - cmin) / iters
        new_pop = []
        for i in range(pop_size):
            xi = pop[i]
            move = [0.0] * DIM
            for j in range(pop_size):
                if i == j:
                    continue
                xj = pop[j]
                d = math.dist(xi, xj)
                if d < 1e-12:
                    continue
                sr = _s(d % 2.0 + 1e-9)      # normalized distance trick (GOA impl.)
                for k in range(DIM):
                    move[k] += c * (highs[k] - lows[k]) / 2.0 * sr * (xj[k] - xi[k]) / d
            cand = clamp([c * m + t for m, t in zip(move, best_x)])
            new_pop.append(cand)
        pop = new_pop
        scores = [fitness(x)["score"] for x in pop]
        evals += pop_size
        it_best = max(range(pop_size), key=lambda i: scores[i])
        if scores[it_best] > best_s:
            best_s, best_x = scores[it_best], pop[it_best][:]
            elite_archive.append((best_s, best_x[:]))
        history.append(best_s)
        # diversity restart: population collapsed into near-duplicates (§7.4)
        spread = max(math.dist(pop[a], pop[b])
                     for a in range(pop_size) for b in range(a + 1, pop_size))
        if spread < diversity_eps:
            for r in range(pop_size // 2):
                pop[r] = rand_vec()
    return {"method": "goa", "best_score": best_s,
            "best_config": vec_to_config(best_x), "history": history,
            "evals": evals, "elite_archive_size": len(elite_archive)}


# ------------------------------------------------------------ baselines ----
def random_search(fitness, budget_evals: int, seed: int = 0) -> dict:
    rng = random.Random(seed)
    best_s, best_x = -1e9, None
    history = []
    for _ in range(budget_evals):
        x = [rng.uniform(lo, hi) for _, lo, hi in SPACE]
        s = fitness(x)["score"]
        if s > best_s:
            best_s, best_x = s, x
        history.append(best_s)
    return {"method": "random", "best_score": best_s,
            "best_config": vec_to_config(best_x), "history": history,
            "evals": budget_evals}


def hill_climb(fitness, budget_evals: int, seed: int = 0,
               step_frac: float = 0.15) -> dict:
    rng = random.Random(seed)
    x = [rng.uniform(lo, hi) for _, lo, hi in SPACE]
    best_s = fitness(x)["score"]
    history = [best_s]
    used = 1
    while used < budget_evals:
        k = rng.randrange(DIM)
        lo, hi = SPACE[k][1], SPACE[k][2]
        cand = x[:]
        cand[k] = max(lo, min(hi, cand[k] + rng.gauss(0, step_frac * (hi - lo))))
        s = fitness(cand)["score"]
        used += 1
        if s >= best_s:
            best_s, x = s, cand
        history.append(best_s)
    return {"method": "hillclimb", "best_score": best_s,
            "best_config": vec_to_config(x), "history": history, "evals": used}


def compare(fitness, seeds: list[int], pop_size: int = 12, iters: int = 30) -> dict:
    """Run all three methods at EQUAL evaluation budget across seeds."""
    budget = pop_size * (iters + 1)
    out = {"budget_evals_per_run": budget, "runs": []}
    for seed in seeds:
        row = {"seed": seed}
        for name, fn in (("goa", lambda: goa_optimize(fitness, pop_size, iters, seed)),
                         ("random", lambda: random_search(fitness, budget, seed)),
                         ("hillclimb", lambda: hill_climb(fitness, budget, seed))):
            res = fn()
            row[name] = {"best_score": round(res["best_score"], 4),
                         "best_config": res["best_config"]}
        out["runs"].append(row)

    def stats(method):
        vals = [r[method]["best_score"] for r in out["runs"]]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return {"mean": round(mean, 4), "min": round(min(vals), 4),
                "max": round(max(vals), 4), "stdev": round(math.sqrt(var), 4)}
    out["summary"] = {m: stats(m) for m in ("goa", "random", "hillclimb")}
    return out
