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

import re
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

#: Verbs that mean the task CHANGES something. The list previously stopped at
#: nine mild ones and contained not a single destructive or irreversible verb —
#: `deploy to production and notify the team` was classified as non-producing
#: and routed to the lowest agency rung, while AGENTS.md invariant 9 requires
#: escalation before exactly that action.
#:
#: Grouped by consequence so the omission is visible if it happens again.
PRODUCING_KW = (
    # authoring
    "merge", "convert", "create", "write", "produce", "package", "clean",
    "fix", "generate", "implement", "add", "build", "refactor", "rename",
    # Measured absent, and each fires on none of the read-only sentences tested:
    "draft", "modify", "improve", "translate", "summarize", "summarise",
    "author", "compose", "rewrite", "extend", "port",
    # destructive
    "delete", "remove", "drop", "purge", "truncate", "revert", "reset",
    # irreversible / outward-facing
    "deploy", "publish", "release", "upload", "push", "migrate", "install",
    "upgrade", "rollback",
    # Korean stems.
    #
    # The authoring half of this list was missing, and the omission was invisible
    # because the destructive half ("삭제", "배포") was present and fired. Measured
    # on twelve matched ko/en pairs: 7 diverged, every one of them an authoring
    # request. `논문 초안 작성` routed to level 2 / tier small / "simple response
    # task" while `write the paper draft` routed to level 3 / medium — so for the
    # single most common Korean verb for producing a document, the harness was off.
    #
    # `작성` (write/author), `만들` (make) and `제작` (produce) are the ones that
    # matter most; `수정`/`보완` (modify/amend), `개선` (improve), `설계` (design),
    # `기획` (plan out), `번역` (translate) and `요약` (summarise) all produce an
    # artifact and were absent too.
    "합치", "병합", "변환", "생성", "정리", "삭제", "제거", "배포", "설치",
    "구현", "추가", "이관", "롤백", "되돌",
    "작성", "만들", "제작", "번역", "고쳐", "고치",
    # Loanword verbs. Korean engineering speech transliterates rather than
    # translates, so the native-stem list above misses the vocabulary a developer
    # actually types. Measured on eight matched pairs before these were added:
    # 6 diverged, and NONE of 리팩터링/리팩토링/마이그레이션/머지/빌드/커밋/디버깅/
    # 업데이트 was present in any list.
    #
    # Only the ones whose ENGLISH counterpart already routes as producing are
    # added. `커밋` and `디버깅` are left out on purpose: `commit the changes` and
    # `debug this module` both route level 2 here, so adding the Korean forms
    # would break the parity they currently have rather than fix a gap.
    "리팩터링", "리팩토링", "마이그레이션", "머지", "빌드",
)

#: Producing verbs that are ALSO ordinary nouns, or appear in read-only phrasing.
#: These do NOT make a task producing on their own when an investigative marker
#: also fires — otherwise `check the design of this module` and `개선 사항 확인`
#: route a read-only question to a higher agency rung with a bigger model, which
#: is the same over-escalation the whole-word rule below was written to stop.
#:
#: Measured on sentences each must not fire alone (`make sure the tests pass`,
#: `why is the update failing`, `explain the design decisions`): `make`, `design`
#: and `update` each produced 1-2 false positives, while `draft`, `modify`,
#: `improve`, `translate`, `summarize`, `rewrite` produced none and sit in the
#: unambiguous list above's Latin half.
#:
#: Korean is worse here, not better, because it is matched as a substring: `설계`
#: is inside `설계 검토`, `수정` inside `수정된 파일 목록`, `개선` inside
#: `개선 사항 확인`. Adding them without this guard would have introduced the
#: defect rather than fixed one.
#:
#: NOT re-litigated: `정리` and `추가`, already in the list above, are ambiguous by
#: the same test (`정리된 목록 확인`). They are left where they were because
#: changing long-standing routing behaviour is a separate decision from closing
#: the gap this pass measured, and silently moving them would hide it.
AMBIGUOUS_PRODUCING_KW = (
    "make", "design", "update",
    "설계", "수정", "보완", "개선", "기획", "요약",
    # The loanword for `update`, which is ambiguous in exactly the same way:
    # `설정 업데이트해줘` produces, `업데이트 실패 원인 확인` inspects.
    "업데이트",
)

INVESTIGATE_KW = ("why", "how many", "count", "check", "verify", "inspect",
                  "explain", "compare", "list", "find", "search", "measure",
                  # `make sure X` is a verification, and it is the phrase that
                  # made bare `make` unusable as a producing verb.
                  "make sure",
                  "왜", "몇", "확인", "검증", "조사", "설명", "비교", "찾")

MULTI_KW = (" and ", ";", "then", "&", "그리고", "하고", "한 뒤", "다음에")

#: `하고` is the Korean conjunctive ending — and also the first half of the
#: PROGRESSIVE `-하고 있다`. Measured: `이 코드 사용하고 있는지 봐` and
#: `테스트가 동작하고 있어?` both fired multi-requirement on a single question,
#: which raises the agency level and the model tier for a one-line ask. The
#: conjunction is real and stays; only the progressive is excluded, because
#: `사용하고 있는지` is one verb, not two requirements.
_KO_PROGRESSIVE = ("하고 있", "하고있")


#: `applicable_when` entries that describe the ROUTER'S OWN VERDICT rather than
#: words in the task. Each was previously substring-matched against the task text,
#: which no sentence can satisfy — `>1 requirement` is a condition, not a phrase —
#: so the skills gated behind them were unreachable.
#:
#: STILL DEAD, and left that way on purpose: `first run in a new repository`.
#: The router has no signal for repository freshness, and inventing one from the
#: knowledge graph's size would be a guess dressed as a measurement. Recorded here
#: so the next reader sees a known gap instead of an oversight.
STRUCTURAL_CONDITIONS = {
    ">1 requirement": lambda state: state["multi_req"],
    ">~10 expected tool calls": lambda state: state["level"] >= 5,
}


def multi_requirement_hits(text: str) -> int:
    """How many conjunctions in `text` actually join two requirements."""
    total = sum(text.count(k) for k in MULTI_KW)
    for progressive in _KO_PROGRESSIVE:
        total -= text.count(progressive)
    return max(0, total)

#: Latin keyword matching is WHOLE-WORD. Substring matching classified
#: `the prefix is wrong` and `inspect the fixture files` as producing (via
#: "fix"), `read the underwriter report` as producing (via "write"), and
#: `how many packages` — an investigative question — as producing. Each of those
#: routes a read-only task to a higher agency rung and a larger model.
#:
#: Korean is matched as a SUBSTRING, and must be: it is agglutinative, so `삭제`
#: appears inside `삭제하라` and `삭제하고`, and a word boundary would never fire.
_LATIN_KW_CACHE: dict[tuple, "re.Pattern"] = {}


def _mentions(text: str, keywords: tuple) -> list[str]:
    """Keywords present in `text`, whole-word for Latin, substring for CJK."""
    hits = []
    latin = tuple(k for k in keywords if k.isascii())
    cjk = [k for k in keywords if not k.isascii()]
    if latin:
        pattern = _LATIN_KW_CACHE.get(latin)
        if pattern is None:
            # Longest first so `how many` is preferred over `many`.
            alts = "|".join(re.escape(k) for k in
                            sorted(latin, key=len, reverse=True))
            pattern = re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])",
                                 re.IGNORECASE)
            _LATIN_KW_CACHE[latin] = pattern
        hits.extend(m.group(0).lower() for m in pattern.finditer(text))
    hits.extend(k for k in cjk if k in text)
    return hits


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
        investigative = bool(_mentions(text, INVESTIGATE_KW))
        # An ambiguous verb is a producing signal only when nothing in the
        # sentence asks a question. `설계 문서 작성` produces; `설계 검토해줘`
        # inspects, and both contain `설계`.
        producing = bool(_mentions(text, PRODUCING_KW)) or (
            bool(_mentions(text, AMBIGUOUS_PRODUCING_KW)) and not investigative)
        multi_req = multi_requirement_hits(text) >= 1
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
        #
        # `applicable_when` holds two kinds of entry and only one of them is text.
        # `>1 requirement` and `>~10 expected tool calls` describe ROUTER STATE,
        # and substring-matching them against the task meant they could never fire
        # — measured: `설계 문서 작성하고 리뷰 요청 그리고 배포`, a four-requirement
        # request, surfaced no skill at all, while the router had already computed
        # `multi_req` one screen earlier and put the task on level 5.
        #
        # So structural conditions are EVALUATED against what was just computed,
        # and only the rest are matched as text.
        state = {"multi_req": multi_req, "level": level,
                 "producing": producing, "investigative": investigative}
        skills = []
        for entry in self.registry.index():
            sig = self.registry.signature(entry["name"])
            for cond in sig["applicable_when"]:
                lowered = cond.lower()
                predicate = STRUCTURAL_CONDITIONS.get(lowered)
                if predicate is not None:
                    if predicate(state):
                        skills.append(entry["name"])
                        break
                    continue
                if any(tok in text for tok in lowered.split("|")):
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
