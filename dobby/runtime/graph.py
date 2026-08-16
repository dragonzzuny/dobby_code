"""The task graph: what has to happen, in what order, and what it owes.

A run is a DAG of nodes, not a conversation. The difference that matters is not
structure for its own sake — it is that a DAG has a *frontier*. At any moment
the runtime can say exactly which nodes are runnable, which are blocked and on
what, and which are done. A conversation cannot answer any of those after a
crash, which is why resuming one means starting it again.

State machines, and the invariant each one protects:

    TaskRun   QUEUED -> RUNNING -> WAITING|RECOVERING -> SUCCEEDED|FAILED|CANCELLED
              Terminal states are terminal. A finished run cannot start a node.

    TaskNode  PENDING -> READY -> LEASED -> RUNNING -> VERIFYING
                                        -> SUCCEEDED|FAILED|SKIPPED
              A node cannot become READY until every dependency SUCCEEDED.

    Attempt   STARTED -> FINISHED|RETRYABLE_FAILURE|PERMANENT_FAILURE
              (run_id, node_id, attempt) is recorded exactly once — enforced by
              the store's primary key, not by care.

LEASED sits between READY and RUNNING on purpose. It is the moment a worker has
claimed a node but has not yet started work, and it is the only state that makes
"this node is being worked on by a process that has since died" a detectable
condition rather than a hang.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import ArtifactContract

# -- run states --------------------------------------------------------------
QUEUED = "QUEUED"
RUNNING = "RUNNING"
WAITING = "WAITING"
RECOVERING = "RECOVERING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

RUN_STATES = (QUEUED, RUNNING, WAITING, RECOVERING, SUCCEEDED, FAILED,
              CANCELLED)
RUN_TERMINAL = {SUCCEEDED, FAILED, CANCELLED}

_RUN_TRANSITIONS = {
    QUEUED: {RUNNING, CANCELLED},
    RUNNING: {WAITING, RECOVERING, SUCCEEDED, FAILED, CANCELLED},
    WAITING: {RUNNING, CANCELLED, FAILED},
    RECOVERING: {RUNNING, FAILED, CANCELLED},
    SUCCEEDED: set(),
    FAILED: set(),
    CANCELLED: set(),
}

# -- node states -------------------------------------------------------------
PENDING = "PENDING"
READY = "READY"
LEASED = "LEASED"
NODE_RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
NODE_SUCCEEDED = "SUCCEEDED"
NODE_FAILED = "FAILED"
SKIPPED = "SKIPPED"
BLOCKED_ON_APPROVAL = "BLOCKED_ON_APPROVAL"

NODE_STATES = (PENDING, READY, LEASED, NODE_RUNNING, VERIFYING, NODE_SUCCEEDED,
               NODE_FAILED, SKIPPED, BLOCKED_ON_APPROVAL)
NODE_TERMINAL = {NODE_SUCCEEDED, NODE_FAILED, SKIPPED}

_NODE_TRANSITIONS = {
    PENDING: {READY, SKIPPED, NODE_FAILED},
    READY: {LEASED, SKIPPED, NODE_FAILED, BLOCKED_ON_APPROVAL},
    LEASED: {NODE_RUNNING, READY, NODE_FAILED},        # back to READY: lease lost
    NODE_RUNNING: {VERIFYING, NODE_FAILED, READY},     # back to READY: retry
    VERIFYING: {NODE_SUCCEEDED, NODE_FAILED, READY},   # back to READY: repair
    BLOCKED_ON_APPROVAL: {READY, SKIPPED, NODE_FAILED},
    NODE_SUCCEEDED: set(),
    NODE_FAILED: set(),
    SKIPPED: set(),
}

# -- attempt outcomes --------------------------------------------------------
STARTED = "STARTED"
FINISHED = "FINISHED"
RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
PERMANENT_FAILURE = "PERMANENT_FAILURE"

ATTEMPT_OUTCOMES = (STARTED, FINISHED, RETRYABLE_FAILURE, PERMANENT_FAILURE)

#: The detail written when an attempt is closed because the process holding it
#: died. A CONSTANT rather than prose in two places: `metrics` counts recovered
#: runs by matching it, and the first version of that match looked for a word
#: the runner never wrote — so `recovery_success_rate` reported "nothing has
#: been interrupted" for a store that contained an interrupted run.
INTERRUPTED_DETAIL = ("the process holding this attempt was interrupted before "
                      "it finished; recovered on resume")


class GraphError(ValueError):
    """A graph that cannot be executed, or a transition that must not happen."""


def check_run_transition(current: str, to_state: str) -> None:
    if to_state not in _RUN_TRANSITIONS.get(current, set()):
        raise GraphError(
            f"illegal run transition {current} -> {to_state}"
            + (" (terminal states are final)" if current in RUN_TERMINAL else ""))


def check_node_transition(current: str, to_state: str) -> None:
    if to_state not in _NODE_TRANSITIONS.get(current, set()):
        raise GraphError(
            f"illegal node transition {current} -> {to_state}"
            + (" (terminal states are final)" if current in NODE_TERMINAL else ""))


@dataclass
class TaskNode:
    """One unit of work: a role, its inputs, its worker, and its contract."""

    node_id: str
    #: What kind of work this is — `plan`, `execute`, `verify`, `report`, or
    #: anything a caller defines. The scheduler treats it as an opaque label; the
    #: worker registry uses it to pick an adapter.
    kind: str
    #: Node ids that must have SUCCEEDED before this one may become READY.
    depends_on: list[str] = field(default_factory=list)
    #: The instruction handed to the worker. `{input}` is substituted with the
    #: promoted payloads of `depends_on`, so a node's prompt cannot silently
    #: reference an unverified artifact.
    instruction: str = ""
    contract: ArtifactContract = field(default_factory=ArtifactContract)
    #: Which worker adapter runs this. Resolved against the registry at run time
    #: so a graph stays serializable.
    worker: str = "command"
    #: Adapter-specific configuration — a command line, a provider id, a model.
    config: dict = field(default_factory=dict)
    state: str = PENDING
    attempts: int = 0
    last_failure: dict | None = None

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "kind": self.kind,
                "depends_on": list(self.depends_on),
                "instruction": self.instruction,
                "contract": self.contract.to_dict(), "worker": self.worker,
                "config": dict(self.config), "state": self.state,
                "attempts": self.attempts, "last_failure": self.last_failure}

    @classmethod
    def from_dict(cls, raw: dict) -> "TaskNode":
        data = dict(raw)
        data["contract"] = ArtifactContract.from_dict(data.get("contract") or {})
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})


class TaskGraph:
    """A validated DAG of `TaskNode`s.

    Validation happens once, at construction, and refuses three things that
    would otherwise surface as a hang: an unknown dependency, a cycle, and a
    duplicate node id. A run that cannot make progress is a bug the graph should
    have caught, not a timeout the operator should diagnose.
    """

    def __init__(self, nodes: list[TaskNode]):
        self.nodes: dict[str, TaskNode] = {}
        for node in nodes:
            if node.node_id in self.nodes:
                raise GraphError(f"duplicate node id {node.node_id!r}")
            self.nodes[node.node_id] = node
        self._validate()

    def _validate(self) -> None:
        for node in self.nodes.values():
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise GraphError(
                        f"node {node.node_id!r} depends on {dep!r}, which is not "
                        f"in the graph")
                if dep == node.node_id:
                    raise GraphError(f"node {node.node_id!r} depends on itself")
        self.topological_order()      # raises on a cycle

    def topological_order(self) -> list[str]:
        """Node ids in dependency order. Raises `GraphError` on a cycle."""
        indegree = {n: len(self.nodes[n].depends_on) for n in self.nodes}
        # Sorted, so the order is stable across runs and a report can be diffed.
        frontier = sorted([n for n, d in indegree.items() if d == 0])
        order: list[str] = []
        while frontier:
            current = frontier.pop(0)
            order.append(current)
            for other in sorted(self.nodes):
                if current in self.nodes[other].depends_on:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        frontier.append(other)
            frontier.sort()
        if len(order) != len(self.nodes):
            stuck = sorted(set(self.nodes) - set(order))
            raise GraphError(f"cycle among nodes: {stuck}")
        return order

    def ready_nodes(self) -> list[TaskNode]:
        """Nodes whose dependencies have all SUCCEEDED and which have not run.

        A dependency that FAILED or was SKIPPED does not make a dependent ready.
        It makes it unreachable, which `unreachable()` reports separately — a
        node quietly sitting in PENDING forever is the failure mode this
        distinction exists to prevent.
        """
        out = []
        for node_id in self.topological_order():
            node = self.nodes[node_id]
            if node.state not in (PENDING, READY):
                continue
            if all(self.nodes[d].state == NODE_SUCCEEDED
                   for d in node.depends_on):
                out.append(node)
        return out

    def unreachable(self) -> list[str]:
        """Pending nodes that can never become READY, and so must be SKIPPED."""
        out = []
        for node_id, node in sorted(self.nodes.items()):
            if node.state not in (PENDING, READY):
                continue
            if any(self.nodes[d].state in (NODE_FAILED, SKIPPED)
                   for d in node.depends_on):
                out.append(node_id)
        return out

    def blocking_reason(self, node_id: str) -> str:
        """Why `node_id` is not runnable, in one sentence."""
        node = self.nodes[node_id]
        pending = [d for d in node.depends_on
                   if self.nodes[d].state != NODE_SUCCEEDED]
        if not pending:
            return "nothing: this node is runnable"
        return "waiting on " + ", ".join(
            f"{d} ({self.nodes[d].state})" for d in pending)

    def done(self) -> bool:
        return all(n.state in NODE_TERMINAL for n in self.nodes.values())

    def succeeded(self) -> bool:
        return all(n.state == NODE_SUCCEEDED for n in self.nodes.values())

    def to_dict(self) -> dict:
        return {"nodes": [n.to_dict() for n in self.nodes.values()]}

    @classmethod
    def from_dict(cls, raw: dict) -> "TaskGraph":
        return cls([TaskNode.from_dict(n) for n in raw.get("nodes", [])])

    def summary(self) -> dict:
        by_state: dict[str, int] = {}
        for node in self.nodes.values():
            by_state[node.state] = by_state.get(node.state, 0) + 1
        return {"nodes": len(self.nodes), "by_state": by_state,
                "order": self.topological_order()}
