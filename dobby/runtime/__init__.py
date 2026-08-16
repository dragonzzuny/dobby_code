"""dobby runtime — the durable execution kernel.

    from dobby.runtime import Runner, default_graph, RunBudget

    graph = default_graph("add rate limiting", execute_command="pytest -q")
    runner = Runner(repo=".")
    run_id = runner.start("add rate limiting", graph)
    result = runner.run(run_id)          # kill the process and call again:
                                         # finished nodes are not re-run

The parts, and the question each one owns:

    graph.py       what has to happen, in what order      (TaskGraph, TaskNode)
    store.py       what actually happened                 (RunStore, SQLite)
    contracts.py   what a node owes the next one          (ArtifactContract)
    verify.py      whether it delivered                   (Verifier, promotable)
    failures.py    what to do when it did not             (Failure, DEFAULT_POLICY)
    scheduler.py   whether to start anything else         (RunBudget, Scheduler)
    workers.py     who does the work                      (WorkerRegistry)
    runner.py      the loop that closes over all of them  (Runner)
"""

from .contracts import (Artifact, ArtifactContract, EXTERNAL_IRREVERSIBLE,
                        EXTERNAL_REVERSIBLE, LOCAL_WRITE, NONE, SCHEMAS,
                        idempotency_key, validate_schema)
from .failures import (DEFAULT_POLICY, FAILURE_CLASSES, Failure,
                       classify_provider_error, classify_verifier_failure)
from .graph import GraphError, TaskGraph, TaskNode
from .runner import Runner, RunResult, default_graph
from .scheduler import BudgetExceeded, RunBudget, Scheduler
from .store import RunStore, StoreError, new_run_id
from .verify import Verifier, VerifierResult, promotable
from .workers import (CommandWorker, ProviderWorker, StaticWorker,
                      WorkerRegistry, WorkerResult)

__all__ = [
    "Artifact", "ArtifactContract", "BudgetExceeded", "CommandWorker",
    "DEFAULT_POLICY", "EXTERNAL_IRREVERSIBLE", "EXTERNAL_REVERSIBLE",
    "FAILURE_CLASSES", "Failure", "GraphError", "LOCAL_WRITE", "NONE",
    "ProviderWorker", "RunBudget", "RunResult", "RunStore", "Runner",
    "SCHEMAS", "Scheduler", "StaticWorker", "StoreError", "TaskGraph",
    "TaskNode", "Verifier", "VerifierResult", "WorkerRegistry", "WorkerResult",
    "classify_provider_error", "classify_verifier_failure", "default_graph",
    "idempotency_key", "new_run_id", "promotable", "validate_schema",
]
