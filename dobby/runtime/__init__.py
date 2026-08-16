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
    trace.py       one correlated record of all of it     (Tracer, Span)
    metrics.py     what the record says, and its gaps     (report, scorecard)
    placement.py   which provider, from what was measured (ProviderPlacement)
    workers.py     who does the work                      (WorkerRegistry)
    runner.py      the loop that closes over all of them  (Runner)
    flywheel.py    failures that recur become test cases  (harvest)
    bench.py       whether any of it helps, or refusing   (run_corpus, compare)
                   to say
"""

from .contracts import (Artifact, ArtifactContract, EXTERNAL_IRREVERSIBLE,
                        EXTERNAL_REVERSIBLE, LOCAL_WRITE, NONE, SCHEMAS,
                        idempotency_key, validate_schema)
from .failures import (DEFAULT_POLICY, FAILURE_CLASSES, Failure,
                       classify_provider_error, classify_verifier_failure)
from .flywheel import harvest, report as flywheel_report
from .graph import GraphError, TaskGraph, TaskNode
from .metrics import Measurement, percentile, report as metrics_report, scorecard
from .placement import (Breaker, ConcurrencyLimiter, Placement,
                        ProviderPlacement, Weights)
from .runner import Runner, RunResult, default_graph
from .scheduler import BudgetExceeded, RunBudget, Scheduler
from .store import RunStore, StoreError, new_run_id
from .trace import NullTracer, Span, Tracer, render_timeline, to_otlp
from .verify import Verifier, VerifierResult, promotable
from .workers import (CommandWorker, ProviderWorker, StaticWorker,
                      WorkerRegistry, WorkerResult)

__all__ = [
    "Artifact", "ArtifactContract", "Breaker", "BudgetExceeded",
    "CommandWorker", "ConcurrencyLimiter", "DEFAULT_POLICY",
    "EXTERNAL_IRREVERSIBLE", "EXTERNAL_REVERSIBLE", "FAILURE_CLASSES",
    "Failure", "GraphError", "LOCAL_WRITE", "Measurement", "NONE",
    "NullTracer", "Placement", "ProviderPlacement", "ProviderWorker",
    "RunBudget", "RunResult", "RunStore", "Runner", "SCHEMAS", "Scheduler",
    "Span", "StaticWorker", "StoreError", "TaskGraph", "TaskNode", "Tracer",
    "Verifier", "VerifierResult", "Weights", "WorkerRegistry", "WorkerResult",
    "classify_provider_error", "classify_verifier_failure", "default_graph",
    "flywheel_report", "harvest", "idempotency_key", "metrics_report",
    "new_run_id", "percentile", "promotable", "render_timeline", "scorecard",
    "to_otlp", "validate_schema",
]
