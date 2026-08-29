"""Turning an accepted plan into work the runtime already knows how to run.

WHAT WAS MISSING

`PlanSpec.execution_steps` has been parsed, validated for type, and stored
durably since the architect boundary landed. Nothing read it. An applied plan
changed the item's acceptance checks and then the loop built the same generic
`plan -> execute -> verify -> report` graph it builds for an item nobody planned,
so the architect's answer to "how should this be done" was recorded and
discarded in the same transaction.

This compiles it. The output is an ordinary `TaskGraph`, so nothing downstream
changes: the same scheduler, the same lease, the same verifier gate, the same
artifact promotion. That is the point — a plan that needed its own execution path
would be a plan that escapes the gates the rest of the system is built on.

THE COMPILER IS A GATE, NOT A TRANSLATOR

`architecture.evaluate` refuses a plan that would weaken the definition of done.
The same reasoning applies one level down, because a step is a request for
capability: "run this command", "write these files". A translator would grant
whatever the plan asked for, and an architect that proposed three writing steps
across the same files would get them.

So every rule here is a refusal rather than a repair, and each names what it
refused:

    more than one writing order        v1 has no worktree isolation, so two
                                       writers in one tree is corruption with
                                       extra steps
    an unknown role                    the roles are an allow-list; a step
                                       asking for something outside it is not
                                       a step this compiler understands
    a raised side-effect class         above LOCAL_WRITE is the runtime's own
                                       approval path, not this one's
    a write path outside the root      a plan may not reach out of the project
    acceptance checks from the plan    they come from the ITEM, which is where
                                       `evaluate` already put the ones it was
                                       allowed to contribute
    no execution steps at all          nothing to compile; the caller keeps the
                                       generic graph rather than getting an
                                       empty one

WHY THE CRITIC CANNOT BLOCK

A model's opinion is not a measurement — `.dobby/ontology.json` says so and
`AdvisoryJudgeWorker` implements it, recording a FAIL verdict as data rather
than raising. The critic order compiles to that adapter for exactly that reason.
The order that decides is `verify`, which runs the project's own commands.
Letting a critic fail the run would be a semantic verdict outranking a
deterministic one, which is the inversion this repository keeps refusing.

WHAT IS DELIBERATELY NOT HERE

`budget_slice`. The review that prompted this asked for one, and `RunBudget`
(runtime/scheduler.py) is per RUN: attempts, deadline, cost, irreversible count.
Nothing in the runtime would enforce a per-node slice, so recording one would
produce a field that reads as a control and behaves as a comment. When per-node
budgets exist, this is where the field goes.
"""

from __future__ import annotations

import dataclasses
import os

from ..runtime import graph as G
from ..runtime.contracts import (LOCAL_WRITE, NONE, SCHEMAS, V_EXISTENCE,
                                 ArtifactContract)
from .models import ProjectError

# -- roles a plan may ask for -------------------------------------------------
#: Read-only investigation. Produces findings, touches nothing.
SCOUT = "scout"
#: The one order that may write. Exactly one per graph in v1.
IMPLEMENT = "implement"
#: A second opinion, recorded and never decisive.
CRITIC = "critic"

#: What a step's `role` may say. `verify` and `report` are absent on purpose:
#: those two are SYNTHESISED by this compiler from the item's own checks, and a
#: plan that could propose its own verify node could propose one that passes.
PROPOSABLE_ROLES = (SCOUT, IMPLEMENT, CRITIC)

# -- execution profiles ------------------------------------------------------
#: May not write. Carries NONE, and its write set must be empty.
READ_ONLY = "READ_ONLY"
#: May write inside the project root, and is the only order that may. Runs alone.
SERIAL_WRITE = "SERIAL_WRITE"
#: Runs commands, not models. The verify order.
DETERMINISTIC = "DETERMINISTIC"

PROFILES = (READ_ONLY, SERIAL_WRITE, DETERMINISTIC)

#: The highest side-effect class this compiler will emit without a human. Matches
#: `architecture.SAFE_SIDE_EFFECTS`; anything above it is the runtime's approval
#: path and reaching it from here would route around that.
MAX_SIDE_EFFECT = LOCAL_WRITE


class PlanNotCompilable(ProjectError):
    """The plan cannot become a graph, and the reason is a refusal not a bug."""


@dataclasses.dataclass(frozen=True)
class WorkOrder:
    """One compiled unit of work: what it is for, what it may touch, how it is graded.

    Frozen because it is the record of a decision. A caller that could edit an
    order after compilation could raise its side-effect class after the checks
    that decide whether that was allowed.
    """

    work_id: str
    plan_id: str
    role: str
    objective: str
    depends_on: tuple = ()
    #: Paths the order expects to read. Advisory in v1 — recorded for the
    #: conflict detection a worktree fan-out will need, and honestly not
    #: enforced, since nothing isolates a worker's reads yet.
    read_set: tuple = ()
    #: Paths the order may write. Empty for every role but `implement`, and
    #: checked to stay inside the project root.
    write_set: tuple = ()
    #: From the ITEM. Never from the plan — see the module docstring.
    acceptance_checks: tuple = ()
    #: A key of `runtime.contracts.SCHEMAS`, or "" for a free-form artifact.
    output_schema: str = ""
    side_effect_class: str = NONE
    execution_profile: str = READ_ONLY
    #: Node ids whose promoted payloads this order is given. The runner
    #: substitutes them into the instruction, so an order cannot reference an
    #: artifact that was never verified.
    input_artifact_ids: tuple = ()

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in dataclasses.asdict(self).items()}


def _relative(path: str, root: str) -> str:
    """A project-relative POSIX path, or a refusal if it leaves the project."""
    text = str(path).strip().replace("\\", "/")
    if not text:
        raise PlanNotCompilable("a step declared an empty path")
    absolute = os.path.abspath(os.path.join(root, text))
    inside = os.path.abspath(root)
    if os.path.commonpath([absolute, inside]) != inside:
        raise PlanNotCompilable(
            f"a step wants to write {text!r}, which resolves outside the "
            f"project root {root!r}. A plan may describe the work; it may not "
            f"widen where the work happens")
    return os.path.relpath(absolute, inside).replace("\\", "/")


def _paths(step: dict, key: str, root: str) -> tuple:
    raw = step.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise PlanNotCompilable(
            f"a step's {key!r} is {type(raw).__name__}, not a list of paths")
    return tuple(_relative(p, root) for p in raw)


def compile_orders(plan, *, item, manifest) -> list[WorkOrder]:
    """The plan's steps as typed orders, or a refusal naming what it asked for.

    Returns orders for the proposable roles only. `verify` and `report` are added
    by `compile_graph`, which is where they belong: they are this harness's
    orders, not the architect's, and building them here would let a future caller
    take the list and drop them.
    """
    steps = list(plan.execution_steps or ())
    if not steps:
        raise PlanNotCompilable(
            f"plan {plan.plan_id} proposes no execution steps; there is nothing "
            f"to compile and the caller should keep the generic graph rather "
            f"than run an empty one")

    if plan.side_effect_class not in ("NONE", LOCAL_WRITE):
        raise PlanNotCompilable(
            f"plan {plan.plan_id} declares side_effect_class "
            f"{plan.side_effect_class!r}; this compiler emits at most "
            f"{MAX_SIDE_EFFECT} and anything beyond it is the runtime's "
            f"approval path")

    root = manifest.root
    orders: list[WorkOrder] = []
    scouts: list[str] = []
    writer: str | None = None

    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise PlanNotCompilable(
                f"execution step {index} is {type(step).__name__}, not an object")
        role = str(step.get("role", "")).strip().lower()
        if role not in PROPOSABLE_ROLES:
            raise PlanNotCompilable(
                f"execution step {index} asks for role {role or '(none)'!r}; "
                f"a step may propose one of {PROPOSABLE_ROLES}. `verify` and "
                f"`report` are this harness's, not the plan's")
        objective = str(step.get("objective") or step.get("what") or "").strip()
        if not objective:
            raise PlanNotCompilable(
                f"execution step {index} ({role}) states no objective; a worker "
                f"cannot be sent an unnamed task")

        write_set = _paths(step, "write_set", root)
        read_set = _paths(step, "read_set", root)

        if role == IMPLEMENT:
            if writer is not None:
                raise PlanNotCompilable(
                    f"plan {plan.plan_id} proposes more than one writing step "
                    f"({writer} and step {index}). Nothing here isolates two "
                    f"workers in one tree, so a second writer is corruption "
                    f"with extra steps. Split it into two work items, which is "
                    f"a decision with an owner")
            if not write_set:
                raise PlanNotCompilable(
                    f"execution step {index} implements without naming a "
                    f"write_set; an order that may write anything cannot be "
                    f"checked against anything")
            work_id = f"implement-{index}"
            writer = work_id
            orders.append(WorkOrder(
                work_id=work_id, plan_id=plan.plan_id, role=IMPLEMENT,
                objective=objective, depends_on=tuple(scouts),
                read_set=read_set, write_set=write_set,
                output_schema="patchset",
                side_effect_class=LOCAL_WRITE,
                execution_profile=SERIAL_WRITE,
                input_artifact_ids=tuple(scouts)))
            continue

        if write_set:
            raise PlanNotCompilable(
                f"execution step {index} is a {role} and declares a write_set "
                f"{list(write_set)}; only an `implement` step may write")

        if role == SCOUT:
            work_id = f"scout-{index}"
            scouts.append(work_id)
            orders.append(WorkOrder(
                work_id=work_id, plan_id=plan.plan_id, role=SCOUT,
                objective=objective, read_set=read_set,
                output_schema="research_claims",
                execution_profile=READ_ONLY))
            continue

        # CRITIC — depends on the writer if there is one, so it judges an
        # artifact rather than an intention.
        if writer is None:
            raise PlanNotCompilable(
                f"execution step {index} is a critic with nothing to criticise: "
                f"no implementing step precedes it")
        orders.append(WorkOrder(
            work_id=f"critic-{index}", plan_id=plan.plan_id, role=CRITIC,
            objective=objective, depends_on=(writer,), read_set=read_set,
            execution_profile=READ_ONLY, input_artifact_ids=(writer,)))

    if writer is None:
        raise PlanNotCompilable(
            f"plan {plan.plan_id} proposes {len(orders)} step(s) and none of "
            f"them implements anything. A graph that investigates and reports "
            f"without changing the tree cannot satisfy an item whose acceptance "
            f"checks run against the tree")
    return orders


def compile_graph(plan, *, item, manifest, provider: str | None = None,
                  execute_command: str | None = None,
                  static: bool = False) -> G.TaskGraph:
    """An accepted plan as a runnable graph, ending in the same gate as any other.

    The tail — `verify` then `report` — is fixed and not proposable. `verify`
    carries the ITEM's acceptance checks and runs no command of its own, exactly
    as `runner.default_graph` builds it, so a compiled run and a generic one are
    graded by the identical mechanism. That equivalence is the argument for
    letting a model shape the middle at all.
    """
    orders = compile_orders(plan, item=item, manifest=manifest)
    if not (static or provider or execute_command):
        raise PlanNotCompilable(
            "a compiled graph needs somebody to do the work: pass provider= "
            "for an agent CLI, execute_command= for a deterministic step, or "
            "static=True for a dry run that exercises the gates only")

    worker = "provider" if provider else "static"
    nodes: list[G.TaskNode] = []

    for order in orders:
        if order.role == CRITIC:
            nodes.append(G.TaskNode(
                node_id=order.work_id, kind="critic",
                depends_on=list(order.depends_on),
                worker="judge" if provider else "static",
                instruction=order.objective,
                # `advisory` because this node's output is a MODEL'S
                # OPINION, which is what the flag is for, and the worker
                # already reports it in `meta` — the CONTRACT is what travels
                # to a consumer through `_promoted_inputs`, so saying it in one
                # place and not the other left the label behind.
                #
                # The schema because a contract declaring nothing is refused at
                # the gate. `ungraded=True` would be the wrong word here: the
                # verdict is not graded, and that it is a verdict at all is
                # checkable.
                contract=ArtifactContract(
                    output_schema=SCHEMAS["judge"],
                    side_effect_class=NONE, advisory=True),
                config=({"criterion": {"id": order.work_id,
                                       "description": order.objective},
                         "judge_of": order.input_artifact_ids[0],
                         "provider_role": "critic",
                         "exclude": [provider] if provider else []}
                        if provider else
                        {"payload": {"verdict_token": "NOT_JUDGED",
                                     "evidence": "no provider was "
                                                 "available to judge"}})))
            continue

        # Refused HERE and not at the gate. A non-writing order with no schema
        # compiles to a contract that declares nothing, and the runtime refuses
        # that — but only after a provider call has been paid for. This module's
        # rule is that every check is a refusal that names what it refused, and
        # the cheapest place to refuse a node that could never be graded is
        # before it runs.
        if (order.role != IMPLEMENT and not order.output_schema
                and order.side_effect_class == NONE):
            raise PlanNotCompilable(
                f"step {order.work_id!r} ({order.role}) declares no output "
                f"schema and no side effect, so nothing it produced could be "
                f"graded. Give it an output_schema from "
                f"{sorted(SCHEMAS)}, or a side_effect_class whose effect can "
                f"be observed")

        is_writer = order.role == IMPLEMENT
        node_worker = ("command" if (is_writer and execute_command) else worker)
        config = ({"command": execute_command} if node_worker == "command"
                  else _config(node_worker, provider, order))
        if node_worker == "provider":
            # scout runs somewhere the project is not; implement writes the
            # original tree. The roles differ and so must their candidate sets.
            # A scout READS; it does not need a worktree and must not be
            # routed to a role that requires one. Mapping it to
            # `isolated_delegate` made the first step of every compiled plan
            # unplaceable, and the run failed before any work happened.
            config["provider_role"] = ("scout" if order.role == SCOUT
                                       else "implement")
            # Carry only the tools the node's declared side effect needs.
            # `workers.tools_for` reads "auto" and derives the set from that
            # same field, so the tool list and the write permission cannot
            # drift apart. Priced 2026-08-24 against a claude call with every
            # tool disabled (22,565 tokens): the full built-in set adds 7,603,
            # `Read,Grep,Glob` adds about 509. A scout handed an editing tool's
            # schema pays roughly 7,100 tokens per call for something it is not
            # allowed to use, and a compiled plan makes several such calls.
            config["tools"] = "auto"
        nodes.append(G.TaskNode(
            node_id=order.work_id, kind=order.role,
            depends_on=list(order.depends_on), worker=node_worker,
            instruction=_instruction(order),
            contract=ArtifactContract(
                output_schema=(SCHEMAS.get("test_report", {})
                               if node_worker == "command"
                               else SCHEMAS.get(order.output_schema, {})),
                side_effect_class=order.side_effect_class,
                # THE PLAN'S WRITE SET REACHES THE EFFECT CHECK.
                #
                # It did not, and on a real repository the consequence was a
                # solved instance reported as a failure. `effects.snapshot`
                # falls back to walking the whole tree when this is empty, and
                # that walk stops at `MAX_WALKED = 3000` files. Measured
                # 2026-08-24 on django__django-11532: the clone holds 6,138
                # files, both snapshots truncated at the same 3,000, the three
                # files the implementer actually edited were not among them, and
                # `observed()` concluded nothing had changed. Both implement
                # attempts failed EFFECT_NOT_OBSERVED while the tree was
                # correct — FAIL_TO_PASS 1/1, zero regressions.
                #
                # With the write set here the snapshot is a three-entry path map
                # instead of a truncated tree walk: exact, and cheaper.
                expected_paths=list(order.write_set)),
            config=config))

    tail = [n.node_id for n in nodes]
    verify = G.TaskNode(
        node_id="verify", kind="verify", depends_on=tail, worker="static",
        instruction="The project's own checks, run by the gate.",
        contract=ArtifactContract(
            output_schema=SCHEMAS["test_report"],
            acceptance_checks=list(item.acceptance_checks),
            # The item's own statement of what its checks reach. Carried here
            # rather than defaulted, because this verify node is what grades
            # the implement orders upstream of it: the rung it declares is the
            # rung those writes are verified at, and leaving it at the floor
            # made every project item look like the weakest possible one.
            checks_at=getattr(item, "checks_at", V_EXISTENCE)),
        config={"payload": {
            "command": "; ".join(item.acceptance_checks) or "(none declared)",
            "exit_code": 0}})
    report = G.TaskNode(
        node_id="report", kind="report", depends_on=["verify"], worker=worker,
        instruction=("Report what was done against the plan's objective, with "
                     "the evidence, and state plainly what remains "
                     "unestablished."),
        contract=ArtifactContract(output_schema=SCHEMAS["report"]),
        config=(_config(worker, provider, None) if worker == "provider"
                else {"payload": {"summary": f"completed: {plan.objective}",
                                  "not_established": []}}))

    return G.TaskGraph(nodes + [verify, report])


def _instruction(order: WorkOrder) -> str:
    """The order, written so the worker is told its limits and not only its task."""
    lines = [order.objective, ""]
    if order.read_set:
        lines.append(f"You may read: {', '.join(order.read_set)}")
    if order.write_set:
        lines.append(f"You may write ONLY: {', '.join(order.write_set)}")
        lines.append("Changing anything else is out of scope for this order; "
                     "report it as a finding instead.")
    else:
        lines.append("This order writes NOTHING. Report what you found.")
    return "\n".join(lines)


def _config(worker: str, provider: str | None, order: WorkOrder | None) -> dict:
    if worker == "provider":
        config: dict = {"provider": provider}
        if order is None:
            config["schema"] = "report"
        elif order.output_schema:
            config["schema"] = order.output_schema
        return config
    if order is None:
        return {"payload": {"summary": "reported", "not_established": []}}
    # A static payload has to satisfy the same schema a real one does, or the
    # gate rejects the node and a dry run fails for a reason that has nothing to
    # do with the plan. `base_commit` says "static" rather than a fake sha: a
    # placeholder that looks like real provenance is worse than one that admits
    # what it is.
    if order.output_schema == "patchset":
        return {"payload": {"base_commit": "static", "diff": "",
                            "changed_files": list(order.write_set)}}
    if order.output_schema == "research_claims":
        return {"payload": {"claims": [{"claim": order.objective,
                                        "evidence": []}]}}
    return {"payload": {"summary": order.objective}}


#: What shape of work a run used. Reported rather than inferred: a compiled run
#: and a generic one produce the same kind of result, and a caller that could not
#: tell them apart could not tell whether the architect's plan was executed or
#: quietly ignored — which is the defect this module exists to fix, and it would
#: be invisible again.
GENERIC = "generic"


def plan_for(store, project_id: str, item):
    """The applied plan this item was shaped by, or None.

    `planned_by` records WHICH plan cleared the item's uncertainty. Matching on
    it rather than taking the newest plan for the item matters: an item can carry
    several plans, most of them refused, and the newest is as likely to be a
    rejection as the one that was applied.
    """
    if not getattr(item, "planned_by", None):
        return None
    for row in store.plans(project_id, work_item_id=item.work_item_id):
        plan = row.get("plan") or {}
        if plan.get("plan_id") == item.planned_by:
            return plan
    return None


def choose_graph(store, project_id: str, item, *, manifest, make_graph,
                 provider=None, execute_command=None, static=False,
                 compile_plans: bool = False, policy: str = "",
                 worktree_available: bool = False):
    """`(graph, shape)` — the compiled plan when there is one, else the generic graph.

    Falling back is not a failure and is not silent: `shape` names what ran, and
    a plan that could not be compiled comes back with the refusal attached rather
    than as a generic run that looks like nobody planned anything.
    """
    from .architecture import PlanSpec

    if policy == "adaptive":
        # THE MEASURED DEFAULT. `evals/ab/RESULTS_pilot.md`: the generic graph
        # cost 3.00x the calls and 2.94x the money for 3/3 verified in every
        # arm. So the shape is chosen from the item rather than assumed, and a
        # scoped gradeable item gets one gated call instead of three.
        from .execution_policy import (ExecutionClass, choose_execution,
                                       explain, profile_item)
        from .fastpath import direct_gated_graph

        profile = profile_item(item, store=store, project_id=project_id,
                               worktree_available=worktree_available)
        chosen = choose_execution(profile)
        why = explain(profile, chosen)

        if chosen in (ExecutionClass.DIRECT_GATED,
                      ExecutionClass.CODEX_FOCUSED_IMPLEMENT):
            return direct_gated_graph(
                item, profile, provider=provider,
                execute_command=execute_command, static=static),                 f"{chosen.value}: {why}"
        if chosen in (ExecutionClass.ARCHITECT_REPLAN,
                      ExecutionClass.HUMAN_BOUNDARY):
            # Not this function's decision to make. The loop already has stops
            # for both, reached through the architect and the boundary checks;
            # the generic graph runs only if the caller sends it here anyway.
            return make_graph(item.outcome or item.title, provider=provider,
                              execute_command=execute_command,
                              acceptance_checks=list(item.acceptance_checks),
                              static=static), f"{chosen.value}: {why}"
        # COMPILED_SERIAL and AGY_ISOLATED_DELEGATE both want the plan compiled,
        # so the flag stops being a flag: the class turns it on. This is PR 6 —
        # compilation as a selective escalation rather than a default tax.
        compile_plans = True

    if not compile_plans:
        return make_graph(item.outcome or item.title, provider=provider,
                          execute_command=execute_command,
                          acceptance_checks=list(item.acceptance_checks),
                          static=static), GENERIC

    raw = plan_for(store, project_id, item)
    if raw is None:
        return make_graph(item.outcome or item.title, provider=provider,
                          execute_command=execute_command,
                          acceptance_checks=list(item.acceptance_checks),
                          static=static), GENERIC

    plan = PlanSpec(**{k: (tuple(v) if isinstance(v, list) else v)
                       for k, v in raw.items()
                       if k in PlanSpec.__dataclass_fields__})

    # A plan that widened the item's acceptance and proposed no steps is the
    # ORDINARY applied plan, not a failed compile. Reporting it with a refusal
    # attached would put a reason on almost every planned item and leave the one
    # that genuinely could not compile indistinguishable from the rest — which
    # is the same "silent difference" problem `shape` exists to prevent, arrived
    # at from the other side.
    if not plan.execution_steps:
        return make_graph(item.outcome or item.title, provider=provider,
                          execute_command=execute_command,
                          acceptance_checks=list(item.acceptance_checks),
                          static=static), GENERIC
    try:
        graph = compile_graph(plan, item=item, manifest=manifest,
                              provider=provider,
                              execute_command=execute_command, static=static)
    except PlanNotCompilable as exc:
        return make_graph(item.outcome or item.title, provider=provider,
                          execute_command=execute_command,
                          acceptance_checks=list(item.acceptance_checks),
                          static=static), f"{GENERIC}: {exc}"
    return graph, f"plan:{plan.plan_id}"
