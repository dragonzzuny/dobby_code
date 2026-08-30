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

# Lazily, and the public names are unchanged. Same reasoning as
# `dobby/project/__init__.py`: twelve submodules imported to present one
# flat API, 0.55s before anything ran, paid by every command that touched
# the package even to read one name.
#
# PEP 562: `from dobby.x import Name` still works, `from dobby.x import
# submodule` still works (the import system falls back to importing the
# submodule when `__getattr__` raises), star imports still work because
# `__all__` is declared, and nothing is read from disk until a name is asked
# for. Checked first: no module under either package does anything at import
# time, so there is no registration to lose by deferring.
#
# The ALIAS is why the value is a pair. `from .metrics import report as
# metrics_report` binds `metrics_report` here and `report` there, and a first
# version of this map stored only the alias -- so the lookup went looking for
# `metrics_report` inside `metrics` and raised ImportError on a name that had
# worked for a year.
_LAZY = {
    "Artifact": ("contracts", "Artifact"),
    "ArtifactContract": ("contracts", "ArtifactContract"),
    "Breaker": ("placement", "Breaker"),
    "BudgetExceeded": ("scheduler", "BudgetExceeded"),
    "CommandWorker": ("workers", "CommandWorker"),
    "ConcurrencyLimiter": ("placement", "ConcurrencyLimiter"),
    "DEFAULT_POLICY": ("failures", "DEFAULT_POLICY"),
    "EXTERNAL_IRREVERSIBLE": ("contracts", "EXTERNAL_IRREVERSIBLE"),
    "EXTERNAL_REVERSIBLE": ("contracts", "EXTERNAL_REVERSIBLE"),
    "FAILURE_CLASSES": ("failures", "FAILURE_CLASSES"),
    "Failure": ("failures", "Failure"),
    "GraphError": ("graph", "GraphError"),
    "LOCAL_WRITE": ("contracts", "LOCAL_WRITE"),
    "Measurement": ("metrics", "Measurement"),
    "NONE": ("contracts", "NONE"),
    "NullTracer": ("trace", "NullTracer"),
    "Placement": ("placement", "Placement"),
    "ProviderPlacement": ("placement", "ProviderPlacement"),
    "ProviderWorker": ("workers", "ProviderWorker"),
    "RunBudget": ("scheduler", "RunBudget"),
    "RunResult": ("runner", "RunResult"),
    "RunStore": ("store", "RunStore"),
    "Runner": ("runner", "Runner"),
    "SCHEMAS": ("contracts", "SCHEMAS"),
    "Scheduler": ("scheduler", "Scheduler"),
    "Span": ("trace", "Span"),
    "StaticWorker": ("workers", "StaticWorker"),
    "StoreError": ("store", "StoreError"),
    "TaskGraph": ("graph", "TaskGraph"),
    "TaskNode": ("graph", "TaskNode"),
    "Tracer": ("trace", "Tracer"),
    "Verifier": ("verify", "Verifier"),
    "VerifierResult": ("verify", "VerifierResult"),
    "Weights": ("placement", "Weights"),
    "WorkerRegistry": ("workers", "WorkerRegistry"),
    "WorkerResult": ("workers", "WorkerResult"),
    "classify_provider_error": ("failures", "classify_provider_error"),
    "classify_verifier_failure": ("failures", "classify_verifier_failure"),
    "default_graph": ("runner", "default_graph"),
    "flywheel_report": ("flywheel", "report"),
    "harvest": ("flywheel", "harvest"),
    "idempotency_key": ("contracts", "idempotency_key"),
    "metrics_report": ("metrics", "report"),
    "new_run_id": ("store", "new_run_id"),
    "percentile": ("metrics", "percentile"),
    "promotable": ("verify", "promotable"),
    "render_timeline": ("trace", "render_timeline"),
    "scorecard": ("metrics", "scorecard"),
    "to_otlp": ("trace", "to_otlp"),
    "validate_schema": ("contracts", "validate_schema"),
}


def __getattr__(name):
    where = _LAZY.get(name)
    if where is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, original = where
    value = getattr(importlib.import_module(f".{module}", __name__),
                    original)
    globals()[name] = value          # second lookup skips this entirely
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))

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
