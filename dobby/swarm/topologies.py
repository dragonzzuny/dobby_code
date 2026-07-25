"""Team topologies: who talks to whom, and what that costs in diversity.

Topology is a diversity decision, not just a plumbing decision
-------------------------------------------------------------
The multi-agent literature's finding about *dense communication topologies
accelerating premature convergence* is a statement about wiring. Every edge in a
team graph is a channel through which one agent's framing reaches another before
that agent has committed to its own — and each such edge trades independence for
coordination.

Connectivity alone does not measure that cost
---------------------------------------------
The intuitive metric — edges ÷ possible edges — turns out to be the wrong one,
and measurably so. At six agents, a **pipeline** and a **fan-out-in** both have
five directed edges and therefore identical connectivity (0.167), while their
diversity properties are opposites: the pipeline chains a single framing through
five successive reinterpretations, and the fan-out keeps five of its six agents
reading the raw task.

The number that does track the cost is **framing depth**: how many hops separate
an agent from what was actually asked. An agent at depth 0 saw the task, and its
answer is independent evidence. An agent at depth 3 is optimising someone's
reading of someone's reading of the task, and its agreement with a sibling says
only that they share an ancestor. Both metrics are reported; `framing_depth` and
`independent_agents` are the ones to read.

The six shapes, at n = 6
------------------------

| topology | conn. | depth | independent | buys | costs |
|---|---|---|---|---|---|
| `independent` | 0.00 | 0 | 6 | maximum diversity | no coordination at all |
| `fan_out_in` | 0.17 | 1 | 5 | breadth with one merge point | the merger is a single point of judgment |
| `supervisor` | 0.24 | 2 | 1 | dynamic reallocation | its decomposition reaches every worker first |
| `hierarchical` | 0.15 | 3 | 1 | scale past one supervisor's span | leaves are three hops from the goal |
| `pipeline` | 0.17 | 5 | 1 | strict staging, specialised stages | errors propagate forward unchecked |
| `mesh` | 1.00 | 5 | 1 | fastest consensus | the collapse case; the honest baseline |

Note that `hierarchical` has *lower* connectivity than `pipeline` and *lower*
framing depth too, yet both are poor choices for exploration — which is exactly
why the recommendation function keys on what the work needs rather than on either
number in isolation.

`mesh` exists so a caller can see what it is choosing. Omitting it would not stop
anyone from wiring a mesh — it would only stop them from being told what it does.

Nothing here spawns an agent. These are *plans*: a topology says which prompts
depend on which results, and `providers/fanout.py` executes each independent
stage. Keeping planning separate from execution means a topology can be
inspected, tested, and costed before a single token is spent.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

INDEPENDENT = "independent"
PIPELINE = "pipeline"
FAN_OUT_IN = "fan_out_in"
SUPERVISOR = "supervisor"
HIERARCHICAL = "hierarchical"
MESH = "mesh"


@dataclasses.dataclass(frozen=True)
class Stage:
    """One executable step of a topology plan.

    `depends_on` lists the stage indices whose output this stage needs. Stages
    with no unmet dependency run concurrently — that is the whole scheduling
    contract, and it is deliberately the only one, so an executor cannot be
    required to understand the topology's semantics.
    """

    index: int
    role: str
    label: str
    depends_on: tuple[int, ...] = ()
    #: True when this stage may see the outputs of the stages it depends on.
    #: False marks an isolated stage: it receives the task only.
    sees_dependencies: bool = True
    instruction: str = ""


@dataclasses.dataclass
class TeamPlan:
    """A topology instantiated for a concrete agent count."""

    topology: str
    stages: list[Stage]
    connectivity: float
    diversity_note: str
    coordination_note: str
    single_points_of_failure: list[str]

    def framing_depth(self) -> int:
        """Longest chain of framing hops between the raw task and any agent.

        This — not `connectivity` — is the number that tracks diversity cost.
        Edge COUNT does not distinguish the two shapes that matter most: a
        6-agent pipeline and a 6-agent fan-out-in both have 5 directed edges, yet
        the pipeline chains one framing through five successive reinterpretations
        while the fan-out keeps every worker at depth 0, reading the task itself.

        An agent at depth d is optimising an interpretation of an interpretation,
        d levels removed from what was actually asked. Depth 0 means the agent
        saw the task; that is the only depth at which its answer is independent
        evidence.
        """
        depth: dict[int, int] = {}
        for stage in sorted(self.stages, key=lambda s: s.index):
            if not stage.depends_on or not stage.sees_dependencies:
                # An isolated stage reads the task directly, whatever it is
                # scheduled after.
                depth[stage.index] = 0
            else:
                depth[stage.index] = 1 + max(depth.get(d, 0)
                                             for d in stage.depends_on)
        return max(depth.values()) if depth else 0

    def independent_agents(self) -> int:
        """How many agents see the raw task rather than someone's reading of it.

        The honest denominator for any claim of corroboration: agreement among
        agents at depth > 0 is agreement about a shared framing, not about the
        task.
        """
        return sum(1 for s in self.stages
                   if not s.depends_on or not s.sees_dependencies)

    def waves(self) -> list[list[int]]:
        """Stage indices grouped into concurrent waves, dependencies respected.

        Each wave is one call to the executor. Wave count is the plan's critical
        path, and therefore its wall-clock cost in model round-trips — the number
        that actually matters when comparing topologies, since a plan with twice
        the agents but the same wave count costs no extra time.
        """
        remaining = {s.index: set(s.depends_on) for s in self.stages}
        done: set[int] = set()
        out: list[list[int]] = []
        while remaining:
            ready = sorted(i for i, deps in remaining.items()
                           if deps <= done)
            if not ready:
                # A cycle. Reported by raising rather than by looping forever,
                # because a cyclic plan cannot be executed and silently dropping
                # stages would produce a partial answer that looks complete.
                raise ValueError(
                    f"cyclic dependencies among stages {sorted(remaining)}")
            out.append(ready)
            done |= set(ready)
            for i in ready:
                remaining.pop(i)
        return out

    def cost(self) -> dict:
        waves = self.waves()
        return {
            "agents": len(self.stages),
            "waves": len(waves),
            "max_parallel": max(len(w) for w in waves) if waves else 0,
            "wave_plan": waves,
            "note": (f"{len(self.stages)} agent calls across {len(waves)} "
                     "sequential wave(s); wave count is the wall-clock cost, "
                     "agent count is the token cost"),
        }

    def to_dict(self) -> dict:
        return {
            "topology": self.topology,
            "connectivity": self.connectivity,
            "framing_depth": self.framing_depth(),
            "independent_agents": self.independent_agents(),
            "diversity_note": self.diversity_note,
            "coordination_note": self.coordination_note,
            "single_points_of_failure": self.single_points_of_failure,
            "stages": [dataclasses.asdict(s) for s in self.stages],
            "cost": self.cost(),
        }


def _connectivity(edges: int, n: int) -> float:
    """Directed edges ÷ possible directed edges. 0 for n < 2."""
    possible = n * (n - 1)
    return round(edges / possible, 4) if possible else 0.0


def build(topology: str, n: int, *, roles: Sequence[str] = (),
          task: str = "") -> TeamPlan:
    """Instantiate `topology` for `n` agents.

    `roles` names each worker's specialisation; missing entries fall back to a
    generic label. Roles matter most for `pipeline`, where each stage is supposed
    to do something the previous one did not.
    """
    if n < 1:
        raise ValueError("a team needs at least one agent")

    def role_at(i: int, default: str) -> str:
        return roles[i] if i < len(roles) else default

    if topology == INDEPENDENT:
        stages = [Stage(index=i, role=role_at(i, "draft"),
                        label=f"independent-{i}", sees_dependencies=False,
                        instruction="Answer alone. You will not see any other "
                                    "answer before submitting.")
                  for i in range(n)]
        return TeamPlan(
            topology, stages, _connectivity(0, n),
            "maximum diversity: no agent can be influenced by another, so "
            "agreement here is genuine corroboration",
            "no coordination — outputs must be merged by the caller",
            [])

    if topology == PIPELINE:
        stages = []
        for i in range(n):
            stages.append(Stage(
                index=i, role=role_at(i, f"stage{i}"),
                label=f"pipeline-{i}",
                depends_on=(i - 1,) if i else (),
                instruction=("Take the previous stage's output and perform ONLY "
                             "your stage's transformation."
                             if i else "Produce the initial artifact.")))
        return TeamPlan(
            topology, stages, _connectivity(max(0, n - 1), n),
            "low diversity by construction: each stage sees exactly one framing "
            "and is asked to extend it, not to question it",
            "strict staging — each stage can be specialised and validated "
            "independently",
            ["every stage: an error at stage k propagates through k+1..n with "
             "nothing behind it to catch the error"])

    if topology == FAN_OUT_IN:
        if n < 2:
            raise ValueError("fan_out_in needs at least 2 agents "
                             "(one worker and one merger)")
        workers = n - 1
        stages = [Stage(index=i, role=role_at(i, "draft"),
                        label=f"worker-{i}", sees_dependencies=False,
                        instruction="Answer alone under your assigned lens.")
                  for i in range(workers)]
        stages.append(Stage(
            index=workers, role="synthesize", label="merger",
            depends_on=tuple(range(workers)),
            instruction="Merge the independent answers. Name what each "
                        "contributed and what was discarded."))
        return TeamPlan(
            topology, stages, _connectivity(workers, n),
            "high diversity during the fan-out phase because workers are "
            "isolated; the merge is where it is spent",
            "breadth with exactly one merge point — the cheapest way to combine "
            "independent work",
            ["the merger: one agent's judgment decides what survives, and "
             "nothing checks it"])

    if topology == SUPERVISOR:
        if n < 2:
            raise ValueError("supervisor needs at least 2 agents")
        workers = n - 1
        stages = [Stage(index=0, role="adjudicate", label="supervisor",
                        instruction="Decompose the task and assign one "
                                    "sub-task per worker.")]
        stages += [Stage(index=i + 1, role=role_at(i, "implement"),
                         label=f"worker-{i}", depends_on=(0,),
                         instruction="Execute only your assigned sub-task.")
                   for i in range(workers)]
        stages.append(Stage(
            index=n, role="synthesize", label="supervisor-merge",
            depends_on=tuple(range(1, n)),
            instruction="Integrate the workers' results and decide whether the "
                        "task is complete."))
        return TeamPlan(
            topology, stages, _connectivity(2 * workers, n + 1),
            "LOW diversity: the supervisor's decomposition reaches every worker "
            "before any of them thinks, so all outputs inherit one framing. Use "
            "this for execution, not for exploration",
            "dynamic reallocation — the supervisor can re-plan after seeing "
            "results, which no static topology can",
            ["the supervisor: its decomposition is never questioned, and a "
             "wrong split makes every worker's correct output useless"])

    if topology == HIERARCHICAL:
        if n < 4:
            raise ValueError("hierarchical needs at least 4 agents; below that "
                             "a supervisor is strictly simpler")
        leads = max(2, int(round((n - 1) ** 0.5)))
        workers = n - 1 - leads
        if workers < leads:
            raise ValueError(
                f"n={n} gives {leads} lead(s) and {workers} worker(s): each "
                "lead needs at least one worker, so use `supervisor` instead")
        stages = [Stage(index=0, role="adjudicate", label="root",
                        instruction="Split the task into "
                                    f"{leads} independent areas.")]
        for lead in range(leads):
            stages.append(Stage(index=1 + lead, role="synthesize",
                                label=f"lead-{lead}", depends_on=(0,),
                                instruction="Plan your area and brief your "
                                            "workers."))
        base = 1 + leads
        assigned = []
        for w in range(workers):
            lead_index = 1 + (w % leads)
            stages.append(Stage(index=base + w,
                                role=role_at(w, "implement"),
                                label=f"worker-{w}",
                                depends_on=(lead_index,),
                                instruction="Execute your lead's assignment."))
            assigned.append(base + w)
        stages.append(Stage(index=base + workers, role="synthesize",
                            label="root-merge",
                            depends_on=tuple(assigned),
                            instruction="Integrate all areas."))
        return TeamPlan(
            topology, stages, _connectivity(leads + workers + workers, len(stages)),
            "LOWEST diversity of any shape here: leaf agents are two framing "
            "hops from the goal, so they optimise their lead's interpretation "
            "of the root's interpretation of the task",
            f"scale — {leads} leads extend coordination past one supervisor's "
            "span of attention",
            ["the root: a bad top-level split cannot be recovered downstream",
             "each lead: its area's workers all inherit its interpretation"])

    if topology == MESH:
        stages = [Stage(index=i, role=role_at(i, "draft"), label=f"mesh-{i}",
                        depends_on=tuple(j for j in range(n) if j < i),
                        instruction="You can see every earlier answer. Add or "
                                    "correct; do not restate agreement.")
                  for i in range(n)]
        return TeamPlan(
            topology, stages, _connectivity(n * (n - 1), n),
            "COLLAPSE RISK: every agent sees every earlier answer, which is the "
            "dense-topology configuration associated with premature convergence. "
            "Measure the result with swarm.diversity.analyze before treating "
            "agreement as evidence",
            "fastest convergence to a shared answer",
            ["the first agent: it anchors everyone after it"])

    raise ValueError(f"unknown topology {topology!r}; expected one of "
                     f"{[INDEPENDENT, PIPELINE, FAN_OUT_IN, SUPERVISOR, HIERARCHICAL, MESH]}")


def recommend(*, agents: int, needs_exploration: bool,
              needs_reallocation: bool, stages_are_distinct: bool) -> dict:
    """Pick a topology from what the work actually needs.

    The ordering encodes one rule: **exploration and coordination are traded
    against each other, and exploration is the one that cannot be recovered
    later.** A team wired for coordination can be re-run with more isolation; a
    team that already converged cannot be un-converged, because every member has
    seen the others' answers.
    """
    if agents < 2:
        return {"topology": INDEPENDENT, "agents": agents,
                "reason": "a single agent has no topology; this is one call"}

    if needs_exploration and not needs_reallocation:
        choice = FAN_OUT_IN if agents >= 3 else INDEPENDENT
        return {"topology": choice, "agents": agents,
                "reason": ("exploration is the priority and no mid-flight "
                           "re-planning is needed, so isolate the workers and "
                           "pay for exactly one merge")}
    if stages_are_distinct and not needs_exploration:
        return {"topology": PIPELINE, "agents": agents,
                "reason": ("the work is a sequence of distinct transformations; "
                           "diversity is not wanted between stages")}
    if needs_reallocation and agents >= 6:
        return {"topology": HIERARCHICAL, "agents": agents,
                "reason": ("re-planning is needed at a scale beyond one "
                           "supervisor's span"),
                "warning": ("leaf agents are two framing hops from the goal — "
                            "do not use this shape for exploration")}
    if needs_reallocation:
        return {"topology": SUPERVISOR, "agents": agents,
                "reason": "re-planning after seeing results is required",
                "warning": ("the supervisor's decomposition reaches every "
                            "worker before any of them thinks; all outputs "
                            "inherit one framing")}
    return {"topology": FAN_OUT_IN, "agents": agents,
            "reason": ("default: isolated work with one merge preserves the "
                       "diversity that a denser wiring would spend")}
