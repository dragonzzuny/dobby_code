"""Task router: minimum-sufficient-agency ladder + budgets.

Ladder (mission §2.6):
  1 deterministic script   2 direct single-model   3 structured single-agent
  4 planner-executor       5 planner-executor-evaluator
  6 specialist team        7 population workflow search
Routing is deterministic and explainable: features -> level, model tier,
skills, policies, budgets. Complexity above level 3 must be justified by a
recorded reason (anti Orchestrator-Overkill).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .policies import PolicyBook
from .skills import SkillRegistry
from .kg import KnowledgeGraph

MODEL_TIERS = ("small", "medium", "large")

DEFAULT_BUDGETS = {
    1: {"context_tokens": 2000, "tool_calls": 5, "minutes": 5},
    2: {"context_tokens": 4000, "tool_calls": 8, "minutes": 10},
    3: {"context_tokens": 8000, "tool_calls": 25, "minutes": 30},
    4: {"context_tokens": 12000, "tool_calls": 40, "minutes": 45},
    5: {"context_tokens": 16000, "tool_calls": 60, "minutes": 60},
    6: {"context_tokens": 24000, "tool_calls": 100, "minutes": 90},
    7: {"context_tokens": 32000, "tool_calls": 200, "minutes": 180},
}

PRODUCING_KW = ("merge", "convert", "create", "write", "produce", "package",
                "clean", "fix", "generate", "합치", "병합", "변환", "생성", "정리")
INVESTIGATE_KW = ("why", "how many", "count", "check", "verify", "inspect",
                  "왜", "몇", "확인", "검증", "조사")
MULTI_KW = (" and ", ";", "then", "&", "그리고", "하고", "한 뒤", "다음에")


@dataclass
class RoutePlan:
    task: str
    level: int
    model_tier: str
    policies: list = field(default_factory=list)      # policy ids
    skills: list = field(default_factory=list)        # skill names
    context_pack: dict = field(default_factory=dict)  # KG bundle
    budgets: dict = field(default_factory=dict)
    needs_plan: bool = False
    needs_independent_eval: bool = False
    escalations_expected: list = field(default_factory=list)
    justification: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Router:
    def __init__(self, policybook: PolicyBook, registry: SkillRegistry,
                 kg: KnowledgeGraph, config: dict | None = None):
        self.policies = policybook
        self.registry = registry
        self.kg = kg
        self.config = config or {}

    def route(self, task: str) -> RoutePlan:
        text = task.lower()
        fired = self.policies.match(task)
        fired_ids = [p["id"] for p in fired]
        # always_on policies fire for every task; only task-specific hits
        # (a keyword/regex match, even on an always_on policy) may raise the
        # agency level or the model tier — otherwise the ladder collapses to
        # its top rung for every producing task
        specific = [p for p in fired
                    if any(f != "always_on" for f in p.get("fired_on", []))]
        severities = {p["severity"] for p in specific}
        producing = any(k in text for k in PRODUCING_KW)
        investigative = any(k in text for k in INVESTIGATE_KW)
        multi_req = sum(text.count(k) for k in MULTI_KW) >= 1
        why = []

        # -- ladder level ----------------------------------------------------
        if not producing and investigative and len(specific) <= 2:
            level = 1 if self._script_covers(task) else 3
            why.append("investigative task -> deterministic script if one covers it")
        elif producing and ("critical" in severities or multi_req):
            level = 5
            why.append("producing task with critical policy or multiple requirements "
                       "-> planner-executor-evaluator")
        elif producing:
            level = 3
            why.append("producing single-requirement task -> structured single agent")
        else:
            level = 2
            why.append("simple response task")
        # levels 6-7 are never auto-selected; they require an explicit human
        # opt-in recorded in config (anti Orchestrator-Overkill)
        if level >= 6 and not self.config.get("allow_multi_agent"):
            level = 5
            why.append("multi-agent capped: no explicit opt-in")

        # -- model tier --------------------------------------------------------
        if level == 1:
            tier = "small"      # deterministic script does the work
        elif "critical" in severities or level >= 5:
            tier = "large"
        elif producing or level >= 3:
            tier = "medium"
        else:
            tier = "small"

        # -- skills (progressive disclosure: index match only) ---------------
        skills = []
        for entry in self.registry.index():
            sig = self.registry.signature(entry["name"])
            for cond in sig["applicable_when"]:
                if any(tok in text for tok in cond.lower().split("|")):
                    skills.append(entry["name"])
                    break

        # -- context pack, sized to the level's budget ------------------------
        budgets = dict(DEFAULT_BUDGETS[level])
        pack = self.kg.context_pack(task, weights=self.config.get("retrieval_weights"),
                                    k=self.config.get("context_k", 8),
                                    token_budget=budgets["context_tokens"] // 2)

        return RoutePlan(
            task=task, level=level, model_tier=tier,
            policies=fired_ids, skills=sorted(set(skills)),
            context_pack=pack, budgets=budgets,
            needs_plan=level >= 4,
            needs_independent_eval=(level >= 5 or "critical" in severities),
            escalations_expected=[p["id"] for p in fired
                                  if p.get("escalation_required")],
            justification=why,
        )

    def _script_covers(self, task: str) -> bool:
        hits = self.kg.retrieve(task, k=3, type_filter={"Tool"})
        return bool(hits and hits[0].score >= self.config.get(
            "script_cover_threshold", 1.0))


class BudgetMeter:
    """Hard budget accounting; callers must check charge() results."""

    def __init__(self, budgets: dict):
        self.budgets = dict(budgets)
        self.spent = {k: 0 for k in budgets}

    def charge(self, key: str, amount: int = 1) -> bool:
        """Returns False when the charge would exceed budget (caller must stop)."""
        if self.spent.get(key, 0) + amount > self.budgets.get(key, float("inf")):
            return False
        self.spent[key] = self.spent.get(key, 0) + amount
        return True

    def remaining(self, key: str) -> int:
        return max(0, self.budgets.get(key, 0) - self.spent.get(key, 0))
