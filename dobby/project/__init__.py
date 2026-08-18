"""dobby project — the unit above a run.

    dobby project init --smoke "pytest -q"
    dobby session open
    dobby project next
    dobby project attach-run W001 <run_id>
    dobby session close <session_id>

The runtime made one RUN durable. A project outlives many, and the bottleneck
after durability is not execution — it is that the next session opens a
repository it has never seen and re-derives what "done" means.

    models.py    the five objects       (ProjectManifest, WorkItem, Portfolio,
                                         Baseline, SessionEnvelope)
    store.py     project state, in the same database as the runs it cites
    init.py      first contact: scan, discover, baseline
    select.py    which item is next, decided without a model
    session.py   open a shift, judge the item by its run, hand over

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
    "OPEN", "Portfolio", "ProjectError", "ProjectManifest", "ProjectStore",
    "READY", "SELECTABLE", "Selection", "SessionEnvelope", "StalePortfolio",
    "VERIFYING", "WORK_ITEM_STATES", "WorkItem", "attach_run",
    "build_manifest", "capability_inventory", "close_session",
    "dependencies_met", "detect_stack", "digest_of", "discover_smoke_checks",
    "git_sha", "initialise", "items_from_specs", "new_project_id",
    "new_session_id", "new_work_item_id", "open_session", "promote_from_run",
    "rank_key", "repo_digest", "run_smoke", "select_next", "take_baseline",
]
