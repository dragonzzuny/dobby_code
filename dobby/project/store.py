"""Project state, in the same database as the runs it is made of.

Why the same file
-----------------
A work item's whole claim is "this was finished, and here is the run that
proves it". Splitting the two across databases makes that claim a join nobody
can do transactionally: the item could be marked DONE in one file while the run
it cites is rolled back in the other. Sharing `runs.sqlite3` keeps the
promotion — check the run, check its artifacts, mark the item — inside one
transaction.

Optimistic concurrency, not locking
-----------------------------------
Two sessions may hold the same portfolio. They are usually a human and an agent,
or two agents on different items, and locking the portfolio for the length of a
work item would block one of them for minutes. So every write carries the
version it read, the UPDATE matches on it, and a loser gets `StalePortfolio`
telling it to refresh. That is invariant PK-6, and it is enforced by the
database rather than by care.

Every portfolio change also appends a `project_event`. The events are what makes
"why is this item BLOCKED" answerable three sessions later, when the envelope
that recorded it has been superseded.
"""

from __future__ import annotations

import json
import os
import time
import uuid

from ..runtime.store import store_path, transaction
from .models import (Baseline, Portfolio, ProjectError, ProjectManifest,
                     SessionEnvelope, WorkItem)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id        TEXT PRIMARY KEY,
    root              TEXT NOT NULL,
    manifest          TEXT NOT NULL,
    manifest_digest   TEXT NOT NULL,
    portfolio_version INTEGER NOT NULL DEFAULT 1,
    baseline          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    project_id    TEXT NOT NULL,
    work_item_id  TEXT NOT NULL,
    spec          TEXT NOT NULL,
    state         TEXT NOT NULL,
    latest_run_id TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, work_item_id)
);
CREATE INDEX IF NOT EXISTS work_items_state ON work_items(project_id, state);

-- Append-only, like `events`. Nothing here is ever updated.
CREATE TABLE IF NOT EXISTS project_events (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    project_id        TEXT NOT NULL,
    work_item_id      TEXT,
    kind              TEXT NOT NULL,
    portfolio_version INTEGER,
    payload           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS project_events_project
    ON project_events(project_id, seq);

-- One row per call to the architect, opened BEFORE the provider runs. A row
-- with no decision is the visible state of "we asked and never heard back",
-- which is what a crash mid-call leaves and what resume has to recognise.
CREATE TABLE IF NOT EXISTS architecture_requests (
    digest       TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    request      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS architecture_requests_project
    ON architecture_requests(project_id, created_at);

-- The plan and what was done about it, written in the SAME transaction as the
-- portfolio change it justifies. Splitting them would allow a plan recorded as
-- APPLIED beside an item that never changed.
CREATE TABLE IF NOT EXISTS plan_revisions (
    digest            TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    work_item_id      TEXT NOT NULL,
    plan_id           TEXT,
    plan              TEXT,
    outcome           TEXT NOT NULL,
    reason            TEXT NOT NULL,
    portfolio_version INTEGER,
    decision          TEXT NOT NULL,
    decided_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_revisions_project
    ON plan_revisions(project_id, decided_at);

CREATE TABLE IF NOT EXISTS session_envelopes (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    envelope   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at  TEXT
);
CREATE INDEX IF NOT EXISTS session_envelopes_project
    ON session_envelopes(project_id, created_at);
"""


class StalePortfolio(ProjectError):
    """Somebody else changed the portfolio since this caller read it.

    Raised rather than merged. A merge would have to guess which of two
    intentions wins, and the losing session can re-read and re-decide in
    milliseconds — which is the correct resolution and the one a human would
    make.
    """


def new_project_id(root: str) -> str:
    """Stable-ish and readable: the folder name plus a short random tail."""
    base = os.path.basename(os.path.abspath(root)) or "project"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base)[:24]
    return f"{safe}-{uuid.uuid4().hex[:6]}"


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def new_work_item_id(index: int) -> str:
    return f"W{index:03d}"


class ProjectStore:
    """Durable projects, portfolios, and session envelopes."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = store_path(data_dir)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with transaction(self.path) as conn:
            conn.executescript(_SCHEMA)

    # -- events ------------------------------------------------------------
    def _event(self, conn, project_id: str, kind: str, payload: dict, *,
               work_item_id: str | None = None,
               portfolio_version: int | None = None) -> None:
        conn.execute(
            "INSERT INTO project_events(ts, project_id, work_item_id, kind,"
            " portfolio_version, payload) VALUES(?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), project_id, work_item_id, kind,
             portfolio_version,
             json.dumps(payload, ensure_ascii=False, default=str)))

    def events(self, project_id: str, *, since: int = 0) -> list[dict]:
        with transaction(self.path) as conn:
            rows = conn.execute(
                "SELECT seq, ts, work_item_id, kind, portfolio_version, payload"
                " FROM project_events WHERE project_id=? AND seq>? ORDER BY seq",
                (project_id, since)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"],
                 "work_item_id": r["work_item_id"], "kind": r["kind"],
                 "portfolio_version": r["portfolio_version"],
                 "payload": json.loads(r["payload"])} for r in rows]

    # -- projects ----------------------------------------------------------
    def create_project(self, manifest: ProjectManifest,
                       items: list[WorkItem]) -> str:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with transaction(self.path) as conn:
            existing = conn.execute(
                "SELECT project_id FROM projects WHERE project_id=?",
                (manifest.project_id,)).fetchone()
            if existing:
                raise ProjectError(
                    f"project {manifest.project_id} already exists")
            conn.execute(
                "INSERT INTO projects(project_id, root, manifest,"
                " manifest_digest, portfolio_version, baseline, created_at,"
                " updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (manifest.project_id, manifest.root,
                 json.dumps(manifest.to_dict(), ensure_ascii=False),
                 manifest.manifest_digest, 1, None, now, now))
            for item in items:
                self._insert_item(conn, item, now)
            self._event(conn, manifest.project_id, "project_created",
                        {"root": manifest.root, "stack": list(manifest.stack),
                         "items": [i.work_item_id for i in items],
                         "manifest_digest": manifest.manifest_digest},
                        portfolio_version=1)
        return manifest.project_id

    @staticmethod
    def _insert_item(conn, item: WorkItem, now: str) -> None:
        conn.execute(
            "INSERT INTO work_items(project_id, work_item_id, spec, state,"
            " latest_run_id, version, updated_at) VALUES(?,?,?,?,?,?,?)",
            (item.project_id, item.work_item_id,
             json.dumps(item.to_dict(), ensure_ascii=False),
             item.state, item.latest_run_id, item.version, now))

    def list_projects(self) -> list[dict]:
        with transaction(self.path) as conn:
            rows = conn.execute(
                "SELECT project_id, root, manifest_digest, portfolio_version,"
                " created_at, updated_at FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def load_project(self, project_id: str | None = None) -> dict:
        """A project by id, or the only one, or a refusal that says which."""
        with transaction(self.path) as conn:
            if project_id:
                row = conn.execute("SELECT * FROM projects WHERE project_id=?",
                                   (project_id,)).fetchone()
                if row is None:
                    raise ProjectError(f"no project {project_id!r}")
            else:
                rows = conn.execute(
                    "SELECT * FROM projects ORDER BY created_at DESC").fetchall()
                if not rows:
                    raise ProjectError(
                        "no project here yet — run `dobby project init`")
                if len(rows) > 1:
                    raise ProjectError(
                        "more than one project in this store; name one with "
                        "--project: "
                        + ", ".join(r["project_id"] for r in rows))
                row = rows[0]
            items = conn.execute(
                "SELECT spec, state, latest_run_id, version FROM work_items"
                " WHERE project_id=? ORDER BY work_item_id",
                (row["project_id"],)).fetchall()

        specs = []
        for record in items:
            spec = json.loads(record["spec"])
            # The columns are authoritative for the mutable fields. Reading them
            # from the blob would resurrect whatever it held when the item was
            # written, which is the same resume defect the run store avoids.
            spec["state"] = record["state"]
            spec["latest_run_id"] = record["latest_run_id"]
            spec["version"] = record["version"]
            specs.append(WorkItem.from_dict(spec))

        baseline = (Baseline.from_dict(json.loads(row["baseline"]))
                    if row["baseline"] else None)
        return {
            "project_id": row["project_id"], "root": row["root"],
            "manifest": ProjectManifest.from_dict(json.loads(row["manifest"])),
            "manifest_digest": row["manifest_digest"],
            "baseline": baseline,
            "portfolio": Portfolio(project_id=row["project_id"],
                                   version=row["portfolio_version"],
                                   items=specs),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # -- baseline ----------------------------------------------------------
    def set_baseline(self, project_id: str, baseline: Baseline) -> None:
        with transaction(self.path) as conn:
            cur = conn.execute(
                "UPDATE projects SET baseline=?, updated_at=? WHERE project_id=?",
                (json.dumps(baseline.to_dict(), ensure_ascii=False),
                 time.strftime("%Y-%m-%dT%H:%M:%S"), project_id))
            if cur.rowcount != 1:
                raise ProjectError(f"no project {project_id!r}")
            self._event(conn, project_id, "baseline_recorded",
                        {"git_sha": baseline.git_sha,
                         "passed": baseline.passed,
                         "manifest_digest": baseline.manifest_digest,
                         "note": baseline.note})

    # -- work items --------------------------------------------------------
    def add_items(self, project_id: str, items: list[WorkItem], *,
                  expected_version: int) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with transaction(self.path) as conn:
            version = self._bump(conn, project_id, expected_version, now)
            for item in items:
                self._insert_item(conn, item, now)
            self._event(conn, project_id, "items_added",
                        {"items": [i.work_item_id for i in items]},
                        portfolio_version=version)
        return version

    def update_item(self, item: WorkItem, *, expected_version: int,
                    reason: str = "") -> int:
        """Write one item and bump the portfolio. Raises `StalePortfolio`.

        The item's own `version` is bumped too, so an event log reader can tell
        two edits of one item apart without consulting timestamps.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with transaction(self.path) as conn:
            version = self._bump(conn, item.project_id, expected_version, now)
            item.version += 1
            cur = conn.execute(
                "UPDATE work_items SET spec=?, state=?, latest_run_id=?,"
                " version=?, updated_at=? WHERE project_id=? AND work_item_id=?",
                (json.dumps(item.to_dict(), ensure_ascii=False), item.state,
                 item.latest_run_id, item.version, now, item.project_id,
                 item.work_item_id))
            if cur.rowcount != 1:
                raise ProjectError(
                    f"no work item {item.work_item_id!r} in "
                    f"{item.project_id!r}")
            self._event(conn, item.project_id, "item_updated",
                        {"state": item.state, "reason": reason,
                         "latest_run_id": item.latest_run_id,
                         "evidence_refs": list(item.evidence_refs),
                         "blocked_reason": item.blocked_reason},
                        work_item_id=item.work_item_id,
                        portfolio_version=version)
        return version

    @staticmethod
    def _bump(conn, project_id: str, expected_version: int, now: str) -> int:
        cur = conn.execute(
            "UPDATE projects SET portfolio_version=portfolio_version+1,"
            " updated_at=? WHERE project_id=? AND portfolio_version=?",
            (now, project_id, expected_version))
        if cur.rowcount != 1:
            row = conn.execute(
                "SELECT portfolio_version FROM projects WHERE project_id=?",
                (project_id,)).fetchone()
            if row is None:
                raise ProjectError(f"no project {project_id!r}")
            raise StalePortfolio(
                f"portfolio is at version {row['portfolio_version']} and this "
                f"write carried {expected_version}; re-read the portfolio and "
                f"decide again — a merge here would have to guess which of two "
                f"intentions wins")
        return expected_version + 1

    # -- session envelopes -------------------------------------------------
    def put_envelope(self, envelope: SessionEnvelope) -> None:
        with transaction(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_envelopes(session_id,"
                " project_id, envelope, created_at, closed_at)"
                " VALUES(?,?,?,?,?)",
                (envelope.session_id, envelope.project_id,
                 json.dumps(envelope.to_dict(), ensure_ascii=False),
                 envelope.created_at, envelope.closed_at))
            self._event(conn, envelope.project_id,
                        "session_closed" if envelope.closed_at
                        else "session_opened",
                        {"session_id": envelope.session_id,
                         "active_work_item_id": envelope.active_work_item_id,
                         "needs_rebaseline": envelope.needs_rebaseline,
                         "next_action": envelope.next_action},
                        work_item_id=envelope.active_work_item_id,
                        portfolio_version=envelope.portfolio_version)

    def get_envelope(self, session_id: str) -> SessionEnvelope:
        with transaction(self.path) as conn:
            row = conn.execute(
                "SELECT envelope FROM session_envelopes WHERE session_id=?",
                (session_id,)).fetchone()
        if row is None:
            raise ProjectError(f"no session {session_id!r}")
        return SessionEnvelope.from_dict(json.loads(row["envelope"]))

    def latest_envelope(self, project_id: str, *,
                        open_only: bool = False) -> SessionEnvelope | None:
        query = ("SELECT envelope FROM session_envelopes WHERE project_id=?"
                 + (" AND closed_at IS NULL" if open_only else "")
                 + " ORDER BY created_at DESC, session_id DESC LIMIT 1")
        with transaction(self.path) as conn:
            row = conn.execute(query, (project_id,)).fetchone()
        if row is None:
            return None
        return SessionEnvelope.from_dict(json.loads(row["envelope"]))

    # -- architecture ------------------------------------------------------
    def open_architecture_request(self, request) -> None:
        """Record the question before anybody answers it.

        `INSERT OR IGNORE`: asking twice about an identical world is one
        question, and the digest says so. The caller checks `decision_for`
        first; this is the belt to that brace.
        """
        with transaction(self.path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO architecture_requests(digest,"
                " project_id, work_item_id, trigger, request, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (request.digest, request.project_id, request.work_item_id,
                 request.trigger,
                 json.dumps(request.to_dict(), ensure_ascii=False),
                 request.created_at))
            self._event(conn, request.project_id, "architecture_requested",
                        {"digest": request.digest, "trigger": request.trigger},
                        work_item_id=request.work_item_id)

    def decision_for(self, project_id: str, digest: str) -> dict | None:
        """A prior decision for this exact question, or None."""
        with transaction(self.path) as conn:
            row = conn.execute(
                "SELECT decision FROM plan_revisions WHERE digest=?"
                " AND project_id=?", (digest, project_id)).fetchone()
        return json.loads(row["decision"]) if row else None

    def open_requests(self, project_id: str) -> list[dict]:
        """Requests with no decision — asked, never settled."""
        with transaction(self.path) as conn:
            rows = conn.execute(
                "SELECT r.request FROM architecture_requests r"
                " LEFT JOIN plan_revisions p ON p.digest = r.digest"
                " WHERE r.project_id=? AND p.digest IS NULL"
                " ORDER BY r.created_at", (project_id,)).fetchall()
        return [json.loads(r["request"]) for r in rows]

    def plans(self, project_id: str, *, work_item_id: str | None = None
              ) -> list[dict]:
        query = ("SELECT plan, decision FROM plan_revisions WHERE project_id=?")
        params = [project_id]
        if work_item_id:
            query += " AND work_item_id=?"
            params.append(work_item_id)
        with transaction(self.path) as conn:
            rows = conn.execute(query + " ORDER BY decided_at",
                                params).fetchall()
        return [{"plan": json.loads(r["plan"]) if r["plan"] else None,
                 "decision": json.loads(r["decision"])} for r in rows]

    def settle_architecture(self, request, plan, decision, *, item=None,
                            expected_version: int | None = None) -> int | None:
        """Write the plan, the decision and the portfolio change as ONE unit.

        The item is optional because most outcomes change nothing: a rejected
        plan must leave the portfolio at the version it was read at, and an
        assertion that it did is the only way to know a refusal was really a
        refusal.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        version = None
        with transaction(self.path) as conn:
            if item is not None:
                if expected_version is None:
                    raise ProjectError(
                        "applying a plan needs the portfolio version it was "
                        "decided against")
                version = self._bump(conn, item.project_id, expected_version,
                                     now)
                item.version += 1
                cur = conn.execute(
                    "UPDATE work_items SET spec=?, state=?, latest_run_id=?,"
                    " version=?, updated_at=? WHERE project_id=?"
                    " AND work_item_id=?",
                    (json.dumps(item.to_dict(), ensure_ascii=False),
                     item.state, item.latest_run_id, item.version, now,
                     item.project_id, item.work_item_id))
                if cur.rowcount != 1:
                    raise ProjectError(
                        f"no work item {item.work_item_id!r} to apply a plan to")
            conn.execute(
                "INSERT OR REPLACE INTO plan_revisions(digest, project_id,"
                " work_item_id, plan_id, plan, outcome, reason,"
                " portfolio_version, decision, decided_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (request.digest, request.project_id, request.work_item_id,
                 decision.plan_id,
                 json.dumps(plan.to_dict(), ensure_ascii=False) if plan
                 else None,
                 decision.outcome, decision.reason, version,
                 json.dumps({**decision.to_dict(),
                             "portfolio_version": version},
                            ensure_ascii=False),
                 decision.decided_at))
            self._event(conn, request.project_id, "plan_decided",
                        {"digest": request.digest, "plan_id": decision.plan_id,
                         "outcome": decision.outcome,
                         "reason": decision.reason,
                         "applied_checks": list(decision.applied_checks)},
                        work_item_id=request.work_item_id,
                        portfolio_version=version)
        return version

    def sessions(self, project_id: str, *, limit: int = 25) -> list[dict]:
        with transaction(self.path) as conn:
            rows = conn.execute(
                "SELECT session_id, created_at, closed_at FROM"
                " session_envelopes WHERE project_id=? ORDER BY created_at DESC"
                " LIMIT ?", (project_id, limit)).fetchall()
        return [dict(r) for r in rows]
