"""The run store: an append-only event log that is the source of truth.

Why a database and not another JSONL file
-----------------------------------------
This kit already records everything in append-only JSONL, and that is right for
a *record*. It is not enough for a *state machine* that has to survive a killed
process, because two things the runtime needs are not expressible in an append:

1. **Exactly-once.** `(run_id, node_id, attempt)` must be recordable once and
   only once, so a worker that dies after acting and before reporting cannot
   produce a second attempt with the same number on resume. That is a uniqueness
   constraint, and a file has none.

2. **Read-modify-write under a lock.** Leasing a node — check it is READY, then
   claim it — has to be atomic against a second `dobby` process working the same
   run. `core/jsonl.py` makes each *append* atomic, which is a different and
   weaker guarantee: two processes can both read READY and both append a lease.

`sqlite3` is in the standard library, so this costs no dependency, and the file
is a single artifact that can be copied, inspected, and deleted like the JSONL
ledgers next to it.

The event log is the truth; the tables are a projection
------------------------------------------------------
`events` is append-only and never updated. `runs`, `nodes`, `attempts`,
`artifacts` and `effects` are derived, and are written in the SAME transaction
as the event that causes them. So the projection can never disagree with the
log, and `rebuild()` can reconstruct it from the log alone when someone needs to
prove that.

The JSONL trajectory stays exactly where it is. It is the human-readable
projection — the thing a person opens to see what happened — and the two are
kept deliberately separate: a record you can read and a state you can resume are
different jobs, and making one file do both is what made resume unreliable.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid

from . import graph as G
from .contracts import Artifact

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Append-only. Nothing in this file ever UPDATEs or DELETEs from this table.
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    node_id  TEXT,
    attempt  INTEGER,
    kind     TEXT NOT NULL,
    payload  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS runs (
    run_id   TEXT PRIMARY KEY,
    task     TEXT NOT NULL,
    state    TEXT NOT NULL,
    created  TEXT NOT NULL,
    updated  TEXT NOT NULL,
    budget   TEXT NOT NULL DEFAULT '{}',
    route    TEXT NOT NULL DEFAULT '{}',
    repo     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nodes (
    run_id   TEXT NOT NULL,
    node_id  TEXT NOT NULL,
    spec     TEXT NOT NULL,
    state    TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated  TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id)
);

-- The uniqueness that makes resume safe. An INSERT that collides here is the
-- runtime discovering it already ran this attempt.
CREATE TABLE IF NOT EXISTS attempts (
    run_id        TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    started       TEXT NOT NULL,
    finished      TEXT,
    outcome       TEXT NOT NULL,
    failure_class TEXT,
    detail        TEXT NOT NULL DEFAULT '',
    worker        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, node_id, attempt)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    digest      TEXT NOT NULL,
    path        TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id, node_id);

-- One row per external effect, keyed by identity rather than by content, so a
-- reworded retry of the same effect collides instead of duplicating.
CREATE TABLE IF NOT EXISTS effects (
    idempotency_key TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    effect_version  TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    result_digest   TEXT NOT NULL DEFAULT ''
);
"""


class StoreError(RuntimeError):
    """The store refused an operation that would corrupt a run's history."""


class AttemptAlreadyRecorded(StoreError):
    """This (run, node, attempt) is already in the log.

    Raised rather than silently ignored: a caller that hits this has lost track
    of what it already did, and the safe response is to reload state, not to
    guess a higher attempt number.
    """


def new_run_id() -> str:
    """Sortable and unique. The timestamp prefix makes `ls` useful."""
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def store_path(data_dir: str) -> str:
    return os.path.join(data_dir, "state", "runtime", "runs.sqlite3")


class RunStore:
    """Durable state for every run in one project.

    Opened per operation rather than held open: a long-lived connection across a
    run that can last an hour is a lock somebody else waits on, and the cost of
    reopening a local SQLite file is not measurable next to a provider call.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = store_path(data_dir)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._tx() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),))

    @contextlib.contextmanager
    def _tx(self):
        """One transaction, on a connection that is CLOSED afterwards.

        `with sqlite3.connect(...) as conn` is a transaction context manager and
        not a closing one — it commits, and leaves the handle open. On POSIX
        that is a leak nobody notices; on Windows an open handle makes
        `shutil.rmtree` fail with PermissionError, so every temp-directory
        cleanup in the test suite raised. Same class of defect as the one
        `cli._read_json` was written for, in a place that holds a lock as well
        as a handle.
        """
        conn = sqlite3.connect(self.path, timeout=30.0,
                               isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        try:
            # WAL so a reader (a status command) never blocks the running
            # worker. Set outside the transaction: sqlite refuses a journal-mode
            # change from inside one.
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # -- events ------------------------------------------------------------
    def _append_event(self, conn: sqlite3.Connection, run_id: str, kind: str,
                      payload: dict, *, node_id: str | None = None,
                      attempt: int | None = None) -> None:
        conn.execute(
            "INSERT INTO events(ts, run_id, node_id, attempt, kind, payload) "
            "VALUES(?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id, attempt, kind,
             json.dumps(payload, ensure_ascii=False, default=str)))

    def events(self, run_id: str, *, since: int = 0) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT seq, ts, node_id, attempt, kind, payload FROM events "
                "WHERE run_id=? AND seq>? ORDER BY seq", (run_id, since)
            ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "node_id": r["node_id"],
                 "attempt": r["attempt"], "kind": r["kind"],
                 "payload": json.loads(r["payload"])} for r in rows]

    # -- runs --------------------------------------------------------------
    def create_run(self, task: str, task_graph: "G.TaskGraph", *,
                   run_id: str | None = None, budget: dict | None = None,
                   route: dict | None = None, repo: str = "") -> str:
        run_id = run_id or new_run_id()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._tx() as conn:
            existing = conn.execute("SELECT run_id FROM runs WHERE run_id=?",
                                    (run_id,)).fetchone()
            if existing:
                raise StoreError(f"run {run_id} already exists")
            conn.execute(
                "INSERT INTO runs(run_id, task, state, created, updated, budget,"
                " route, repo) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, task, G.QUEUED, now, now,
                 json.dumps(budget or {}, ensure_ascii=False),
                 json.dumps(route or {}, ensure_ascii=False, default=str), repo))
            for node in task_graph.nodes.values():
                conn.execute(
                    "INSERT INTO nodes(run_id, node_id, spec, state, attempts,"
                    " updated) VALUES(?,?,?,?,?,?)",
                    (run_id, node.node_id,
                     json.dumps(node.to_dict(), ensure_ascii=False,
                                default=str),
                     node.state, node.attempts, now))
            self._append_event(conn, run_id, "run_created",
                               {"task": task, "budget": budget or {},
                                "nodes": task_graph.topological_order()})
        return run_id

    def load_run(self, run_id: str) -> dict:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"no run {run_id!r} in {self.path}")
            nodes = conn.execute(
                "SELECT node_id, spec, state, attempts FROM nodes WHERE run_id=?"
                " ORDER BY node_id", (run_id,)).fetchall()
        specs = []
        for node in nodes:
            spec = json.loads(node["spec"])
            # The projection columns are authoritative for mutable fields; the
            # spec blob keeps the immutable definition. Reading state from the
            # blob would resurrect whatever it held when the node was created,
            # which is exactly the resume bug this store exists to prevent.
            spec["state"] = node["state"]
            spec["attempts"] = node["attempts"]
            specs.append(spec)
        return {"run_id": row["run_id"], "task": row["task"],
                "state": row["state"], "created": row["created"],
                "updated": row["updated"],
                "budget": json.loads(row["budget"]),
                "route": json.loads(row["route"]), "repo": row["repo"],
                "graph": G.TaskGraph.from_dict({"nodes": specs})}

    def set_run_state(self, run_id: str, to_state: str, *,
                      reason: str = "") -> None:
        if to_state not in G.RUN_STATES:
            raise StoreError(f"unknown run state {to_state!r}")
        with self._tx() as conn:
            row = conn.execute("SELECT state FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"no run {run_id!r}")
            G.check_run_transition(row["state"], to_state)
            conn.execute("UPDATE runs SET state=?, updated=? WHERE run_id=?",
                         (to_state, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id))
            self._append_event(conn, run_id, "run_state",
                               {"from": row["state"], "to": to_state,
                                "reason": reason})

    def list_runs(self, *, limit: int = 50) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT run_id, task, state, created, updated FROM runs "
                "ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- nodes -------------------------------------------------------------
    def set_node_state(self, run_id: str, node_id: str, to_state: str, *,
                       reason: str = "", enforce: bool = True) -> None:
        if to_state not in G.NODE_STATES:
            raise StoreError(f"unknown node state {to_state!r}")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id)).fetchone()
            if row is None:
                raise StoreError(f"no node {node_id!r} in run {run_id!r}")
            if enforce:
                G.check_node_transition(row["state"], to_state)
            conn.execute(
                "UPDATE nodes SET state=?, updated=? WHERE run_id=? AND node_id=?",
                (to_state, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id))
            self._append_event(conn, run_id, "node_state",
                               {"from": row["state"], "to": to_state,
                                "reason": reason}, node_id=node_id)

    def lease_node(self, run_id: str, node_id: str, *, holder: str) -> bool:
        """Claim a READY node atomically. False means somebody else has it.

        The check and the claim are one UPDATE with the expected state in the
        WHERE clause, so two processes racing for the same node produce one
        winner and one `False` — rather than two workers running the same node
        and two sets of side effects.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE nodes SET state=?, updated=? "
                "WHERE run_id=? AND node_id=? AND state=?",
                (G.LEASED, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id,
                 G.READY))
            if cur.rowcount != 1:
                return False
            self._append_event(conn, run_id, "node_leased", {"holder": holder},
                               node_id=node_id)
            return True

    # -- attempts ----------------------------------------------------------
    def next_attempt_number(self, run_id: str, node_id: str) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT MAX(attempt) AS n FROM attempts "
                "WHERE run_id=? AND node_id=?", (run_id, node_id)).fetchone()
        return (row["n"] or 0) + 1

    def start_attempt(self, run_id: str, node_id: str, attempt: int, *,
                      worker: str = "") -> None:
        with self._tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO attempts(run_id, node_id, attempt, started,"
                    " outcome, worker) VALUES(?,?,?,?,?,?)",
                    (run_id, node_id, attempt,
                     time.strftime("%Y-%m-%dT%H:%M:%S"), G.STARTED, worker))
            except sqlite3.IntegrityError as exc:
                raise AttemptAlreadyRecorded(
                    f"attempt {attempt} of {node_id} in run {run_id} is already "
                    f"recorded — reload the run rather than guessing the next "
                    f"number") from exc
            conn.execute(
                "UPDATE nodes SET attempts=?, updated=? "
                "WHERE run_id=? AND node_id=?",
                (attempt, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id))
            self._append_event(conn, run_id, "attempt_started",
                               {"worker": worker}, node_id=node_id,
                               attempt=attempt)

    def finish_attempt(self, run_id: str, node_id: str, attempt: int, *,
                       outcome: str, failure_class: str | None = None,
                       detail: str = "") -> None:
        if outcome not in G.ATTEMPT_OUTCOMES:
            raise StoreError(f"unknown attempt outcome {outcome!r}")
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE attempts SET finished=?, outcome=?, failure_class=?,"
                " detail=? WHERE run_id=? AND node_id=? AND attempt=? "
                "AND outcome=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), outcome, failure_class,
                 detail[:2000], run_id, node_id, attempt, G.STARTED))
            if cur.rowcount != 1:
                raise StoreError(
                    f"attempt {attempt} of {node_id} is not open (already "
                    f"finished, or never started)")
            self._append_event(conn, run_id, "attempt_finished",
                               {"outcome": outcome,
                                "failure_class": failure_class,
                                "detail": detail[:2000]},
                               node_id=node_id, attempt=attempt)

    def attempts(self, run_id: str, node_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM attempts WHERE run_id=?"
        params: list = [run_id]
        if node_id:
            query += " AND node_id=?"
            params.append(node_id)
        query += " ORDER BY node_id, attempt"
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def open_attempts(self, run_id: str) -> list[dict]:
        """Attempts that started and never finished — the crash signature.

        A process killed mid-node leaves exactly this: a STARTED row with no
        finish. `resume` reads it to know which node was in flight, instead of
        inferring it from a node state that was written before the work began.
        """
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE run_id=? AND outcome=? "
                "ORDER BY node_id, attempt", (run_id, G.STARTED)).fetchall()
        return [dict(r) for r in rows]

    # -- artifacts ---------------------------------------------------------
    def put_artifact(self, artifact: Artifact, *, path: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifacts(artifact_id, run_id, node_id,"
                " kind, state, digest, path, created) VALUES(?,?,?,?,?,?,?,?)",
                (artifact.artifact_id, artifact.run_id, artifact.node_id,
                 artifact.kind, artifact.state, artifact.digest_, path,
                 artifact.created))
            self._append_event(conn, artifact.run_id, "artifact",
                               {"artifact_id": artifact.artifact_id,
                                "kind": artifact.kind, "state": artifact.state,
                                "digest": artifact.digest_, "path": path},
                               node_id=artifact.node_id)

    def artifacts(self, run_id: str, *, node_id: str | None = None,
                  state: str | None = None) -> list[dict]:
        query = "SELECT * FROM artifacts WHERE run_id=?"
        params: list = [run_id]
        if node_id:
            query += " AND node_id=?"
            params.append(node_id)
        if state:
            query += " AND state=?"
            params.append(state)
        query += " ORDER BY created, artifact_id"
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    # -- external effects --------------------------------------------------
    def claim_effect(self, key: str, run_id: str, node_id: str,
                     effect_version: str) -> bool:
        """Reserve the right to perform an external effect. False = already done.

        Claimed BEFORE the effect, not after. The window between acting and
        recording is where duplicates are born, and the only way to close it
        without a distributed transaction is to record the intent first and
        accept that a crash in that window leaves a claimed-but-unperformed
        effect. That failure is visible (`unconfirmed_effects`) and safe; the
        other order's failure is an invisible duplicate.
        """
        with self._tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO effects(idempotency_key, run_id, node_id,"
                    " effect_version, applied_at) VALUES(?,?,?,?,?)",
                    (key, run_id, node_id, effect_version,
                     time.strftime("%Y-%m-%dT%H:%M:%S")))
            except sqlite3.IntegrityError:
                return False
            self._append_event(conn, run_id, "effect_claimed",
                               {"key": key, "effect_version": effect_version},
                               node_id=node_id)
            return True

    def confirm_effect(self, key: str, result_digest: str) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT run_id, node_id FROM effects "
                               "WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                raise StoreError(f"no claimed effect {key!r} to confirm")
            conn.execute("UPDATE effects SET result_digest=? "
                         "WHERE idempotency_key=?", (result_digest, key))
            self._append_event(conn, row["run_id"], "effect_confirmed",
                               {"key": key, "result_digest": result_digest},
                               node_id=row["node_id"])

    def unconfirmed_effects(self, run_id: str) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM effects WHERE run_id=? AND result_digest=''",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def effects(self, run_id: str) -> list[dict]:
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM effects WHERE run_id=? ORDER BY applied_at",
                (run_id,)).fetchall()]

    # -- integrity ---------------------------------------------------------
    def rebuild(self, run_id: str) -> dict:
        """Recompute node states from the event log and report disagreements.

        Not called on the happy path. It exists so the claim "the log is the
        truth" is testable rather than asserted: if the projection and the log
        ever diverge, this says where.
        """
        states: dict[str, str] = {}
        for event in self.events(run_id):
            if event["kind"] == "node_state" and event["node_id"]:
                states[event["node_id"]] = event["payload"]["to"]
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT node_id, state FROM nodes WHERE run_id=?",
                (run_id,)).fetchall()
        projected = {r["node_id"]: r["state"] for r in rows}
        mismatches = {
            node: {"log": states.get(node, G.PENDING), "table": state}
            for node, state in projected.items()
            if states.get(node, G.PENDING) != state
        }
        return {"run_id": run_id, "nodes": len(projected),
                "from_log": states, "mismatches": mismatches,
                "consistent": not mismatches}
