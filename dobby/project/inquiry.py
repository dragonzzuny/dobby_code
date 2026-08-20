"""A topic is not a portfolio. This turns one into the other, deterministically.

WHY THIS EXISTS

`dobby project init --items` says it plainly: "Absent means an empty portfolio —
this command does not invent one." That refusal is correct and it is also the
reason a topic cannot be handed to this harness. `project run` on an empty
portfolio stops at PORTFOLIO_COMPLETE, which is true and useless. Somebody has to
write the work items, and for a research lifecycle they write the same eight
every time.

So this writes those eight. What it must not do — and does not do — is invent
what the topic REQUIRES. The distinction is the whole design:

    a LIFECYCLE SKELETON  is knowledge about how inquiry proceeds. It is the
                          same for every topic, so a template is honest.
    a REQUIREMENT         is knowledge about this topic. A template that emitted
                          those would be a model's guess wearing a portfolio's
                          clothes, and `project init` refuses it for that reason.

Every item below is a stage of inquiry with the topic substituted into its
outcome sentence. No item claims to know what the literature says, which datasets
exist, or what the answer might be. The stages that discover those things are the
items themselves.

WHY IT IS DETERMINISTIC

`project/select.py` explains why the selector calls no model: the same portfolio
in the same state must yield the same next item, or an interrupted session
reconsiders instead of continuing. A decomposer that called a model would move
that non-determinism one level up — the same topic would produce a different
portfolio on Tuesday. So this is a pure function of (topic, options), and two
runs are byte-identical.

WHERE IT DELIBERATELY STOPS

The implementation stage gets the project's OWN declared checks and nothing else.
If the caller supplies none, that item ships with no acceptance check, which
makes `needs_architect` True and halts the loop at NEEDS_ARCHITECT. That is the
correct outcome and not a gap: what to build for this topic is decided by the
elaboration stage's artifact, and inventing an acceptance check for work nobody
has specified yet is exactly the "make the item look gradeable" move that
`project/architecture.py` rejects.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import unicodedata

from . import evidence as E

#: One stage of inquiry. `kind` is the artifact gate from `project/evidence.py`,
#: or None for a stage graded by the project's own commands.
@dataclasses.dataclass(frozen=True)
class Stage:
    key: str
    title: str
    #: `{topic}` is substituted; nothing else is. A template with more slots
    #: would be a template that needs to know something about the topic.
    outcome: str
    kind: str | None
    #: Stage keys this one cannot start before. Encoded here rather than left to
    #: the caller, because the ordering IS the claim this module makes: no
    #: ideation before prior art (AGENTS.md, "Ideation is gated"), no evaluation
    #: before something was built.
    after: tuple[str, ...] = ()
    #: Below UNCERTAINTY_ESCALATION (3) for the stages whose artifact gate fully
    #: defines done. The implementation stage carries 3 on purpose: what to build
    #: is genuinely undecided until elaboration has produced its artifact, and
    #: understating that would send an unspecified item to a worker.
    uncertainty: int = 1
    impact: int = 1


#: The lifecycle, in the order inquiry actually proceeds. Background before
#: literature because the cheapest prior art is the project's own; data before
#: ideation because an idea that cannot be tested on any available data is not
#: yet an idea; debugging last because it is the stage that consumes an
#: evaluation that came out wrong.
STAGES: tuple[Stage, ...] = (
    Stage("background", "배경조사 / background",
          "Establish what THIS project and its knowledge graph already contain "
          "about: {topic}. Every finding anchored to a path that exists.",
          E.BACKGROUND, impact=2),
    Stage("literature", "문헌조사 / literature",
          "Retrieve external prior art on: {topic}. Include at least one "
          "refutation- or limitation-shaped query and record what it returned, "
          "including nothing.",
          E.LITERATURE, after=("background",), impact=3),
    Stage("dataset", "데이터수집 / data",
          "Identify and manifest the data that could test a claim about: "
          "{topic}. Origin, licence, row count, and split declared per source.",
          E.DATASET, after=("literature",), impact=2),
    Stage("ideation", "아이디어 기획 / ideation",
          "Propose approaches to: {topic}. Each anchored to an evidence id from "
          "the background or literature artifact, each with a test that could "
          "show it is wrong.",
          E.IDEATION, after=("literature", "dataset"), impact=3),
    Stage("elaboration", "아이디어 구체화 / elaboration",
          "Turn the surviving ideas for {topic} into buildable specifications: "
          "named targets and the acceptance checks each would be graded by.",
          E.ELABORATION, after=("ideation",), impact=3),
    Stage("implementation", "구현 / implementation",
          "Build the elaborated approach to: {topic}. Graded by this project's "
          "own declared checks, not by a description of the work.",
          None, after=("elaboration",), uncertainty=3, impact=3),
    Stage("evaluation", "평가 / evaluation",
          "Measure the implementation for {topic} against a baseline, with more "
          "than one run per arm and a stated trivial floor.",
          E.EVALUATION, after=("implementation",), impact=3),
    Stage("debug", "디버깅 / debug",
          "Record every defect the evaluation of {topic} exposed, each with a "
          "reproduction and the observed-versus-expected pair.",
          E.DEBUG, after=("evaluation",), impact=2),
)

STAGE_KEYS = tuple(s.key for s in STAGES)

#: Where a stage's artifact lands. Under `.dobby/` because these are operational
#: run artifacts, and in a per-topic directory so two inquiries in one repository
#: cannot overwrite each other's evidence.
ARTIFACT_ROOT = ".dobby/inquiry"


def slug(topic: str, *, limit: int = 28) -> str:
    """A directory-safe handle that stays stable and stays distinct.

    Korean, Japanese and Chinese topics reduce to almost nothing under an ASCII
    filter, so a pure slug would collide across unrelated topics. The digest
    suffix is what actually keeps them apart; the readable prefix exists so a
    person can tell the directories apart without opening them.
    """
    normalised = unicodedata.normalize("NFKC", topic).strip().lower()
    ascii_ish = re.sub(r"[^a-z0-9]+", "-", normalised).strip("-")[:limit]
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_ish}-{digest}" if ascii_ish else digest


def artifact_path(topic: str, stage_key: str, *, root: str = ARTIFACT_ROOT
                  ) -> str:
    return f"{root}/{slug(topic)}/{stage_key}.json"


def decompose(topic: str, *, stages: tuple[str, ...] | None = None,
              smoke_checks: tuple[str, ...] = (),
              artifact_root: str = ARTIFACT_ROOT,
              python: str = "python",
              minimums: dict[str, int] | None = None) -> list[dict]:
    """The topic as a dependency-ordered list of work item specs.

    Returns specs (plain dicts) rather than `WorkItem`s so the output is exactly
    what `--items` already accepts: a caller may write it to a file, read it,
    edit a stage out, and hand it to `project init` unchanged. A decomposer that
    returned objects would be a decomposer whose output a person cannot inspect
    before it becomes a portfolio.

    `smoke_checks` are the project's own commands, used ONLY by the
    implementation stage. Passing none leaves that item ungradeable on purpose;
    see the module docstring.
    """
    if not topic or not topic.strip():
        raise ValueError("decompose needs a topic: an empty string would "
                         "produce eight items about nothing")
    wanted = tuple(stages) if stages else STAGE_KEYS
    unknown = [k for k in wanted if k not in STAGE_KEYS]
    if unknown:
        raise ValueError(f"unknown stage(s) {unknown}; expected some of "
                         f"{STAGE_KEYS}")
    selected = [s for s in STAGES if s.key in wanted]
    floors = dict(minimums or {})

    # Ids are assigned here rather than by `items_from_specs`, because
    # `depends_on` has to name them and a spec whose dependency is a stage key
    # the store has never heard of is a dependency that silently never resolves.
    ids = {s.key: f"W{i:03d}" for i, s in enumerate(selected, start=1)}

    specs: list[dict] = []
    for stage in selected:
        if stage.kind is None:
            checks = list(smoke_checks)
        else:
            checks = [E.acceptance_command(
                stage.kind, artifact_path(topic, stage.key, root=artifact_root),
                min_rows=floors.get(stage.key), python=python)]
        specs.append({
            "work_item_id": ids[stage.key],
            "title": f"{stage.title}: {topic}"[:120],
            "outcome": stage.outcome.format(topic=topic),
            "acceptance_checks": checks,
            # Only dependencies that are actually in this portfolio. Dropping a
            # stage must not leave the next one waiting on an item that will
            # never exist — that is NOTHING_STARTABLE reported as a dependency
            # problem when it is really a configuration one.
            "depends_on": [ids[k] for k in stage.after if k in ids],
            # Earlier stages first, so `select.py`'s priority ordering and the
            # dependency graph agree instead of racing.
            "priority": len(selected) - selected.index(stage),
            "impact": stage.impact,
            "uncertainty": stage.uncertainty,
        })
    return specs


def plan(topic: str, **kwargs) -> dict:
    """`decompose` plus what the caller has to do next, stated rather than implied.

    The artifacts do not exist yet — that is the point, the stages create them —
    so a plan that only listed items would read as ready-to-run when the first
    acceptance check is guaranteed to fail until a stage has actually produced
    something. The `artifacts` block says which file each stage owes.
    """
    specs = decompose(topic, **kwargs)
    wanted = set(kwargs.get("stages") or STAGE_KEYS)
    root = kwargs.get("artifact_root", ARTIFACT_ROOT)
    # `decompose` emits in STAGES order over the same selection, so zipping the
    # two is a positional identity rather than a guess. Asserted below so a
    # future reordering fails loudly instead of mislabelling every artifact.
    selected = [s for s in STAGES if s.key in wanted]
    if len(selected) != len(specs):        # pragma: no cover - structural guard
        raise RuntimeError("stage selection and emitted specs disagree")
    return {
        "topic": topic,
        "slug": slug(topic),
        "items": specs,
        "artifacts": {
            spec["work_item_id"]: {
                "stage": stage.key,
                "kind": stage.kind,
                "path": (artifact_path(topic, stage.key, root=root)
                         if stage.kind else None),
            } for stage, spec in zip(selected, specs)},
        "ungradeable": [s["work_item_id"] for s in specs
                        if not s["acceptance_checks"]],
        "note": ("stage artifacts do not exist yet: every acceptance check "
                 "fails until the stage that owns it has produced its file. "
                 "That is the definition of done, not a defect"),
        "next": ("write this to a file and run: dobby project init --root . "
                 "--items <file>"),
    }
