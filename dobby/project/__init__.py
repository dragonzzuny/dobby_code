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

Six invariants, each enforced in one place:

    PK-1  a failing baseline yields no work item                    select.py
    PK-2  DONE requires a SUCCEEDED run with a PROMOTED artifact    session.py
    PK-3  DONE is not selectable again without an explicit reopen   select.py
    PK-4  a stale manifest digest or git sha refuses to start       session.py
    PK-5  recovery outranks new work                                select.py
    PK-6  a portfolio change bumps the version and appends an event store.py
"""

from .init import (build_manifest, capability_inventory, detect_stack,
                   discover_smoke_checks, git_sha, initialise, items_from_specs,
                   repo_digest, run_smoke, take_baseline)
from .architecture import (APPLIED, ArchitectureRequest, NEEDS_DISCOVERY,
                           NEEDS_HUMAN_APPROVAL, OUTCOMES, PlanDecision,
                           PlanRejected, PlanSpec, REJECTED,
                           request_architecture)
from .loop import STOP_REASONS, advance
from .evidence import (ArtifactError, KINDS, acceptance_command,
                       check_file, ideas_and_corpus)
from .inquiry import STAGE_KEYS, decompose, plan
from .reattempt import derive_repair, parse_artifact_check, persevere
from .readonly import ReadOnlyViolation, run_read_only
from .workorder import (PlanNotCompilable, WorkOrder, choose_graph,
                        compile_graph, compile_orders)
from .models import (BLOCKED, CANCELLED, DONE, IN_PROGRESS, NEEDS_REPLAN, OPEN,
                     READY, SELECTABLE, VERIFYING, WORK_ITEM_STATES, Baseline,
                     Portfolio, ProjectError, ProjectManifest, SessionEnvelope,
                     WorkItem, digest_of)
from .select import Selection, dependencies_met, rank_key, select_next
from .session import (attach_run, close_session, open_session,
                      promote_from_run)
from .store import (ProjectStore, StalePortfolio, new_project_id,
                    new_session_id, new_work_item_id)

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
    "run_read_only",
]
