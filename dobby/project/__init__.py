"""dobby project — the unit above a run.

    dobby project init --smoke "pytest -q"
    dobby session open
    dobby project next
    dobby project attach-run W001 <run_id>
    dobby session close <session_id>
    dobby project run --until empty

The runtime made one RUN durable. A project outlives many, and the bottleneck
after durability is not execution — it is that the next session opens a
repository it has never seen and re-derives what "done" means.

    models.py    the five objects       (ProjectManifest, WorkItem, Portfolio,
                                         Baseline, SessionEnvelope)
    store.py     project state, in the same database as the runs it cites
    init.py      first contact: scan, discover, baseline
    select.py    which item is next, decided without a model
    session.py   open a shift, judge the item by its run, hand over
    loop.py      one item at a time, and the boundary it stopped at
    architecture.py  the one place a model may change the plan
    inquiry.py   a research topic as a portfolio, deterministically
    evidence.py  acceptance for stages a command could not grade before
    refine.py    the idea cycle: generate, assess, repair, generate again
                 (imported as a MODULE, never re-exported: a function named
                  `refine` in this namespace would shadow its own module)
    reattempt.py the item cycle: retry only after a derived repair
    readonly.py  running a provider in a role that may not write
    workorder.py an accepted plan as a graph the existing runtime runs
    execution_policy.py  which SHAPE of execution an item gets
    fastpath.py  one gated call, and a report nobody paid for
    workspace.py an isolated tree for one item, and the gate its changes
                 must pass to enter the project
    replan.py    a failure carried back to the one thing allowed to change
                 the approach

Six invariants, each enforced in one place:

    PK-1  a failing baseline yields no work item                    select.py
    PK-2  DONE requires a SUCCEEDED run with a PROMOTED artifact    session.py
    PK-3  DONE is not selectable again without an explicit reopen   select.py
    PK-4  a stale manifest digest or git sha refuses to start       session.py
    PK-5  recovery outranks new work                                select.py
    PK-6  a portfolio change bumps the version and appends an event store.py
"""

# Lazily, and the public names are unchanged.
#
# This package imported all fourteen submodules to present one flat API,
# which cost 0.47s before anything ran. `dobby/cli.py` reaches in for a
# single tuple of stage names while BUILDING ITS ARGUMENT PARSER, so every
# `dobby` command -- `doctor`, `style`, `fleet` -- paid the whole project
# and runtime stack to render one help string. Measured cold, that chain
# was 4.81s.
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
    "ANNOTATION_PREFIX": ("models", "ANNOTATION_PREFIX"),
    "APPLIED": ("architecture", "APPLIED"),
    "ArchitectureRequest": ("architecture", "ArchitectureRequest"),
    "ArtifactError": ("evidence", "ArtifactError"),
    "BLOCKED": ("models", "BLOCKED"),
    "Baseline": ("models", "Baseline"),
    "CANCELLED": ("models", "CANCELLED"),
    "ChangeManifest": ("workspace", "ChangeManifest"),
    "DONE": ("models", "DONE"),
    "ExecutionClass": ("execution_policy", "ExecutionClass"),
    "IN_PROGRESS": ("models", "IN_PROGRESS"),
    "KINDS": ("evidence", "KINDS"),
    "MergeRefused": ("workspace", "MergeRefused"),
    "NEEDS_DISCOVERY": ("architecture", "NEEDS_DISCOVERY"),
    "NEEDS_HUMAN_APPROVAL": ("architecture", "NEEDS_HUMAN_APPROVAL"),
    "NEEDS_REPLAN": ("models", "NEEDS_REPLAN"),
    "OPEN": ("models", "OPEN"),
    "OUTCOMES": ("architecture", "OUTCOMES"),
    "PlanDecision": ("architecture", "PlanDecision"),
    "PlanNotCompilable": ("workorder", "PlanNotCompilable"),
    "PlanRejected": ("architecture", "PlanRejected"),
    "PlanSpec": ("architecture", "PlanSpec"),
    "Portfolio": ("models", "Portfolio"),
    "ProjectError": ("models", "ProjectError"),
    "ProjectManifest": ("models", "ProjectManifest"),
    "ProjectStore": ("store", "ProjectStore"),
    "READY": ("models", "READY"),
    "REJECTED": ("architecture", "REJECTED"),
    "ReadOnlyViolation": ("readonly", "ReadOnlyViolation"),
    "SELECTABLE": ("models", "SELECTABLE"),
    "STAGE_KEYS": ("inquiry", "STAGE_KEYS"),
    "STOP_REASONS": ("loop", "STOP_REASONS"),
    "Selection": ("select", "Selection"),
    "SessionEnvelope": ("models", "SessionEnvelope"),
    "StalePortfolio": ("store", "StalePortfolio"),
    "TaskProfile": ("execution_policy", "TaskProfile"),
    "VERIFYING": ("models", "VERIFYING"),
    "WORK_ITEM_STATES": ("models", "WORK_ITEM_STATES"),
    "WorkItem": ("models", "WorkItem"),
    "WorkOrder": ("workorder", "WorkOrder"),
    "acceptance_command": ("evidence", "acceptance_command"),
    "advance": ("loop", "advance"),
    "attach_run": ("session", "attach_run"),
    "blocked_needs_replan": ("replan", "blocked_needs_replan"),
    "build_manifest": ("init", "build_manifest"),
    "capability_inventory": ("init", "capability_inventory"),
    "changed_paths": ("workspace", "changed_paths"),
    "check_file": ("evidence", "check_file"),
    "choose_execution": ("execution_policy", "choose_execution"),
    "choose_graph": ("workorder", "choose_graph"),
    "close_session": ("session", "close_session"),
    "compile_graph": ("workorder", "compile_graph"),
    "compile_orders": ("workorder", "compile_orders"),
    "declared_write_set": ("workspace", "declared_write_set"),
    "decompose": ("inquiry", "decompose"),
    "dependencies_met": ("select", "dependencies_met"),
    "derive_repair": ("reattempt", "derive_repair"),
    "detect_stack": ("init", "detect_stack"),
    "deterministic_report": ("fastpath", "deterministic_report"),
    "digest_of": ("models", "digest_of"),
    "direct_gated_graph": ("fastpath", "direct_gated_graph"),
    "discover_smoke_checks": ("init", "discover_smoke_checks"),
    "failure_context": ("replan", "failure_context"),
    "git_sha": ("init", "git_sha"),
    "ideas_and_corpus": ("evidence", "ideas_and_corpus"),
    "initialise": ("init", "initialise"),
    "isolated": ("workspace", "isolated"),
    "items_from_specs": ("init", "items_from_specs"),
    "merge": ("workspace", "merge"),
    "new_project_id": ("store", "new_project_id"),
    "new_session_id": ("store", "new_session_id"),
    "new_work_item_id": ("store", "new_work_item_id"),
    "open_session": ("session", "open_session"),
    "parse_artifact_check": ("reattempt", "parse_artifact_check"),
    "persevere": ("reattempt", "persevere"),
    "plan": ("inquiry", "plan"),
    "profile_item": ("execution_policy", "profile_item"),
    "promote_from_run": ("session", "promote_from_run"),
    "rank_key": ("select", "rank_key"),
    "repo_digest": ("init", "repo_digest"),
    "request_architecture": ("architecture", "request_architecture"),
    "request_replan": ("replan", "request_replan"),
    "run_read_only": ("readonly", "run_read_only"),
    "run_smoke": ("init", "run_smoke"),
    "select_next": ("select", "select_next"),
    "take_baseline": ("init", "take_baseline"),
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
    "BLOCKED", "Baseline", "CANCELLED", "DONE", "IN_PROGRESS", "NEEDS_REPLAN",
    "APPLIED", "ArchitectureRequest", "NEEDS_DISCOVERY",
    "NEEDS_HUMAN_APPROVAL", "OPEN", "OUTCOMES", "PlanDecision",
    "PlanRejected", "PlanSpec", "Portfolio", "REJECTED", "STOP_REASONS", "ProjectError", "ProjectManifest", "ProjectStore",
    "READY", "SELECTABLE", "Selection", "SessionEnvelope", "StalePortfolio",
    "VERIFYING", "WORK_ITEM_STATES", "WorkItem", "advance", "attach_run", "request_architecture",
    "build_manifest", "capability_inventory", "close_session",
    "dependencies_met", "detect_stack", "digest_of", "discover_smoke_checks",
    "git_sha", "initialise", "items_from_specs", "new_project_id",
    "new_session_id", "new_work_item_id", "open_session", "promote_from_run",
    "rank_key", "repo_digest", "run_smoke", "select_next", "take_baseline",
    "ArtifactError", "KINDS", "STAGE_KEYS", "acceptance_command",
    "check_file", "decompose", "derive_repair", "ideas_and_corpus",
    "parse_artifact_check",
    "persevere", "plan", "PlanNotCompilable", "ReadOnlyViolation",
    "WorkOrder", "choose_graph", "compile_graph", "compile_orders",
    "run_read_only", "blocked_needs_replan", "failure_context",
    "request_replan", "ANNOTATION_PREFIX", "ChangeManifest",
    "MergeRefused", "changed_paths", "declared_write_set",
    "isolated", "merge", "ExecutionClass", "TaskProfile",
    "choose_execution", "profile_item", "deterministic_report",
    "direct_gated_graph",
]
