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
import socket
import sqlite3
import threading
import time
import uuid

from ..core.platform import process_alive
from . import graph as G
from .contracts import Artifact, check_artifact_write

#: 2 added the `spans` table. Every table is `CREATE TABLE IF NOT EXISTS`, so an
#: existing store gains it on the next open without a migration step; runs
#: recorded before it simply have no spans, which is the truth about them.
#:
#: 3 added `lease_owner`/`lease_expires` to `nodes`. Columns, unlike tables, do
#: NOT appear on an existing database from a CREATE IF NOT EXISTS — so this one
#: needs the ALTER in `_migrate`. A store written by version 2 opens with both
#: columns empty, which reads as "no lease recorded" and recovers exactly as it
#: did before.
SCHEMA_VERSION = 3

#: How long a lease is honoured without renewal. A node cannot legitimately run
#: longer than its own timeout, so the runner sets this from the node's timeout
#: plus a margin rather than from a global heartbeat: a heartbeat thread would
#: be a second liveness mechanism to keep correct, and the node's own wall clock
#: already bounds the truth this needs.
DEFAULT_LEASE_TTL_S = 3600.0

#: The node states in which a worker is actively holding the node. The whole set
#: matters: an attempt stays open across all three, so a state missing from here
#: is a window in which recovery sees an open attempt with no holder.
LEASE_HOLDING_STATES = (G.LEASED, G.NODE_RUNNING, G.VERIFYING)

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

-- `lease_owner` and `lease_expires` are what make a lease auditable rather than
-- merely atomic. The claim was always a single compare-and-swap, so two workers
-- never both won it; without a recorded owner, though, nothing could tell an
-- abandoned lease from one a live worker is still holding, and crash recovery
-- had to assume every open attempt was abandoned.
CREATE TABLE IF NOT EXISTS nodes (
    run_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    spec         TEXT NOT NULL,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated      TEXT NOT NULL,
    lease_owner  TEXT NOT NULL DEFAULT '',
    lease_expires REAL NOT NULL DEFAULT 0,
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

-- The observation model. Separate from `events` on purpose: an event is a fact
-- about state ("this node became READY"), a span is an INTERVAL with a parent
-- ("this generation took 12s inside this node inside this run"). Collapsing
-- them loses the tree, and the tree is what answers "where did the time go".
CREATE TABLE IF NOT EXISTS spans (
    span_id        TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL,
    parent_span_id TEXT,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    node_id        TEXT,
    attempt        INTEGER,
    started_ms     REAL NOT NULL,
    ended_ms       REAL,
    duration_ms    REAL,
    status         TEXT NOT NULL,
    attributes     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id, started_ms);
CREATE INDEX IF NOT EXISTS spans_kind ON spans(kind, started_ms);

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


#: The three answers `effect_status` can give. Constants rather than literals
#: because the runner branches on them and a typo'd branch would silently take
#: the "never claimed" path, which is the one that repeats the effect.
EFFECT_CLAIMED = "CLAIMED"
EFFECT_CONFIRMED = "CONFIRMED"


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


@contextlib.contextmanager
def transaction(path: str):
    """One transaction, on a connection that is CLOSED afterwards.

    `with sqlite3.connect(...) as conn` is a transaction context manager and
    not a closing one — it commits, and leaves the handle open. On POSIX that is
    a leak nobody notices; on Windows an open handle makes `shutil.rmtree` fail
    with PermissionError, so every temp-directory cleanup in the test suite
    raised. Same class of defect as the one `cli._read_json` was written for, in
    a place that holds a lock as well as a handle.

    Module-level so a second store over the same file — `project/store.py` keeps
    its tables in this database, which is what makes a work item joinable to the
    run that satisfied it — does not have to reach into a private method to get
    the same guarantees.
    """
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


#: Durability of a commit, from `DOBBY_SQLITE_SYNCHRONOUS`. Default FULL.
#:
#: Under WAL, `NORMAL` still gives a consistent database and still survives the
#: process being killed -- an application crash loses nothing, because the WAL
#: file already has the frames. What it gives up is the OS crash and the power
#: cut: the most recent commits can be lost. Measured on this machine, 300
#: transactions on a held connection: 4.7 ms/tx at FULL against 0.30 ms/tx at
#: NORMAL.
#:
#: FULL is the default and stays the default. This store is what a resume
#: reads, so trading its durability is a deployment decision somebody makes on
#: purpose for a specific machine -- a CI runner rebuilding from scratch, a
#: throughput benchmark -- and not something a version bump does to them.
_SYNCHRONOUS_MODES = ("FULL", "NORMAL", "EXTRA", "OFF")


def _synchronous() -> str:
    raw = (os.environ.get("DOBBY_SQLITE_SYNCHRONOUS") or "FULL").strip().upper()
    if raw not in _SYNCHRONOUS_MODES:
        raise StoreError(
            f"DOBBY_SQLITE_SYNCHRONOUS={raw!r} is not a sqlite synchronous "
            f"mode; expected one of {_SYNCHRONOUS_MODES}. Refused rather than "
            f"defaulted: a typo silently falling back to FULL would make a "
            f"machine somebody tuned behave like one they did not.")
    return raw


def connect(path: str) -> sqlite3.Connection:
    """One configured connection. The caller closes it."""
    conn = sqlite3.connect(path, timeout=30.0, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    # WAL so a reader (a status command) never blocks the running worker.
    # Set outside the transaction: sqlite refuses a journal-mode change from
    # inside one. Measured at 4% of a 300-transaction loop, so repeating it per
    # connection is not worth the memo it would take to skip.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={_synchronous()}")
    return conn


def worker_identity() -> str:
    """Who this process is, as a lease owner: `host/pid`.

    The host half is not decoration. Liveness can only be checked for a PID on
    this machine, so an owner has to say where it lives — otherwise a second
    host reading the same store would test its own process table against a
    stranger's PID and get a confident wrong answer.
    """
    return f"{socket.gethostname()}/{os.getpid()}"


def lease_is_held(owner: str, expires: float, *, now: float | None = None
                  ) -> bool:
    """True only when a LIVE worker demonstrably still holds this lease.

    The default is False, and that asymmetry is the whole design: this answer
    decides whether crash recovery may take a node back, so every case where
    the evidence is absent or unreadable must fall to "not held" — a run that
    stalls because nobody dared reclaim an abandoned node is a worse failure
    than one that reclaims a node whose owner cannot be identified.

    True requires an owner string this runtime wrote and a lease that has not
    expired, and then either: the owner names THIS host and that PID is running,
    or it names another host, whose process table cannot be read from here and
    whose unexpired lease is therefore the only evidence available.
    """
    if not owner:
        return False
    if (now or time.time()) >= (expires or 0):
        # Expired beats liveness on purpose. It is the bound on PID reuse and on
        # a process that reports alive for a reason this code cannot see.
        return False
    host, _, pid_text = owner.rpartition("/")
    if not host or not pid_text.isdigit():
        return False        # not a string this runtime wrote
    if host != socket.gethostname():
        # Another machine's PID cannot be probed from here, so the unexpired
        # lease is the only evidence there is — and it is evidence FOR the
        # holder. Recovery waits out the TTL rather than guessing.
        return True
    return process_alive(int(pid_text)) is True


def _strict_spec(node) -> str:
    """Serialize a node spec, refusing anything that cannot round-trip.

    Deliberately WITHOUT `default=str`, which every other write here uses. The
    difference is that those are records — a report of what happened, where a
    stringified object is ugly and harmless — and this is EXECUTABLE DATA. The
    runner runs the graph it loaded, not the one it was handed, so a value that
    goes in as an object and comes back as its `str()` changes what the code
    does and changes nothing visible.

    Measured while writing the injection tests: a `Failure` in `config` came
    back as a string, the fault never fired, and four tests reported a healthy
    system. Refusing at `start()` puts the error where the fix is one line.
    """
    try:
        return json.dumps(node.to_dict(), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise StoreError(
            f"node {node.node_id!r} has a spec that is not JSON: {exc}. The "
            f"runner executes the graph it LOADS from this store, so a config "
            f"value that cannot round-trip would silently become its str() and "
            f"change what the node does") from exc


class RunStore:
    """Durable state for every run in one project.

    Opened per operation by default, and held for the length of a `session()`.

    The default was chosen because an open handle on Windows makes
    `shutil.rmtree` fail with PermissionError, which broke every temp-directory
    cleanup in the suite. That reason still holds. The reason given alongside it
    did not: "the cost of reopening a local SQLite file is not measurable next
    to a provider call" is true of one provider call and false of the harness.
    Measured on this machine, 300 transactions:

        connection per transaction   27.3 ms/tx
        one connection reused         4.7 ms/tx

    A 16-node graph runs about 19 transactions per node, so the difference is
    roughly 430 ms of pure harness time per node -- invisible behind a 300 s
    agent call, and most of the wall clock for a graph of deterministic ones.

    `session()` is the narrow fix: hold one connection for the duration of a
    run, close it in a `finally`, and leave every call outside a run exactly as
    it was. Holding the connection is also not the same as holding a LOCK --
    under WAL a lock is taken per transaction and released at its commit, so a
    reader is never waiting on the handle itself.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = store_path(data_dir)
        #: The connection held by `session()`, per thread. Set before any call
        #: that might reach `_tx`, including the schema creation below.
        self._local = threading.local()
        #: Spans that could not be written. Reported rather than raised — see
        #: `record_span` — so a metrics table can say it is short instead of
        #: presenting a partial sum as a whole.
        self.span_write_failures: list[str] = []
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._tx() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),))
            conn.execute("UPDATE meta SET value=? WHERE key='schema'",
                         (str(SCHEMA_VERSION),))

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns an older store predates. Idempotent, and column-driven.

        Driven by what the table actually has rather than by the recorded
        version number, because the version is a claim about the schema and the
        schema is the schema — a store hand-copied between machines, or written
        by a branch that bumped the number without the column, has to open
        rather than fail.
        """
        have = {row["name"] for row in
                conn.execute("PRAGMA table_info(nodes)").fetchall()}
        for column, ddl in (("lease_owner", "TEXT NOT NULL DEFAULT ''"),
                            ("lease_expires", "REAL NOT NULL DEFAULT 0")):
            if column not in have:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {column} {ddl}")

    @contextlib.contextmanager
    def session(self):
        """Hold one connection for this thread until the block exits.

        Thread-local, because `sqlite3` connections refuse use from a thread
        other than the one that made them and `max_parallel > 1` runs nodes on
        several. A worker thread with no session of its own simply falls back to
        a connection per transaction, which is the behaviour every caller had
        before this existed.

        Re-entrant: a nested `session()` is a no-op rather than a second
        connection, so a caller does not have to know whether its caller
        already opened one.
        """
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        conn = connect(self.path)
        self._local.conn = conn
        try:
            yield
        finally:
            self._local.conn = None
            conn.close()

    @contextlib.contextmanager
    def _tx(self):
        held = getattr(self._local, "conn", None)
        if held is None:
            with transaction(self.path) as conn:
                yield conn
            return
        # `with conn` commits on success and rolls back on an exception, and
        # leaves the handle open. That is the whole saving.
        with held:
            yield held

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
                    (run_id, node.node_id, _strict_spec(node),
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
            if row["state"] == to_state:
                # Reporting the same fact twice is not a transition. With one
                # process this never happened; with the several the lease design
                # supports it happens constantly, and it used to raise
                # `illegal run transition WAITING -> WAITING` -- two workers
                # agreeing that the run is parked, and one of them dying for it.
                #
                # No event either. An event log is what a resume replays, and a
                # row saying WAITING -> WAITING records a change that did not
                # occur.
                return
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
                       reason: str = "", enforce: bool = True,
                       expect: str | None = None) -> bool:
        """Move a node. Returns whether this caller is the one that moved it.

        `expect` makes it a compare-and-set, and it exists because of a measured
        lease theft. `_execute_node` promoted a node from PENDING to READY out
        of its OWN copy of the graph, and `LEASED -> READY` is legal because
        that is how a lost lease is recovered. Two processes, three nodes:

            PENDING -> READY   "dependencies satisfied"   (worker A)
            node_leased        A
            attempt_started
            LEASED  -> READY   "dependencies satisfied"   (worker B, stale)
            node_leased        B
            attempt_started

        B's in-memory node still said PENDING, so B wrote READY over a lease a
        LIVE worker was holding, took it, and both ran the node. Two promoted
        artifacts for one node, which `_promoted_inputs` then refused as a
        broken invariant -- correctly, and one layer too late.

        With `expect`, the write happens only if the stored state is still what
        the caller thought, in the same transaction as the read. A caller that
        loses says so by getting `False` instead of overwriting somebody.
        """
        if to_state not in G.NODE_STATES:
            raise StoreError(f"unknown node state {to_state!r}")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id)).fetchone()
            if row is not None and expect is not None                     and row["state"] != expect:
                return False
            if row is None:
                raise StoreError(f"no node {node_id!r} in run {run_id!r}")
            if row["state"] == to_state:
                # The same fact reported twice, exactly as at the run level.
                # Two workers each holding their own copy of the graph both see
                # a node PENDING and both move it to READY; the second used to
                # die with `illegal node transition READY -> READY`. Measured:
                # three processes on one run, two dead, four times in six.
                #
                # No event, because an event log is what a resume replays and a
                # row saying READY -> READY records a change that did not
                # happen. The lease, not this, is what stops two workers doing
                # the WORK -- and it still does: `lease_node` is one UPDATE
                # with the expected state in its WHERE clause.
                return True
            if enforce:
                G.check_node_transition(row["state"], to_state)
            elif to_state not in G.RECOVERY_DESTINATIONS:
                # `enforce=False` is for recovery, which the forward table
                # cannot describe. It was a blanket override, and a gate with an
                # unbounded override is the override. The destinations are now
                # an allow-list, and SUCCEEDED is not on it: a node passes its
                # gate to get there or it does not get there.
                raise StoreError(
                    f"recovery may not move {node_id!r} to {to_state!r}; "
                    f"allowed: {sorted(G.RECOVERY_DESTINATIONS)}. Reaching "
                    f"{G.NODE_SUCCEEDED} requires the verifier")
            # A node outside the working states is not being worked on, so it
            # must not still name an owner. Left behind, a stale owner is worse
            # than no owner at all: it makes `lease_is_held` answer for a lease
            # nobody holds, and recovery believes it.
            #
            # VERIFYING is a working state and dropping it here was a real bug
            # for the length of one edit: the attempt is still open while the
            # acceptance checks run, so a lease released at VERIFYING left a
            # window in which another worker saw an open attempt with no holder
            # and recovered a node that was mid-verification.
            holds = to_state in LEASE_HOLDING_STATES
            conn.execute(
                "UPDATE nodes SET state=?, updated=?" +
                ("" if holds else ", lease_owner='', lease_expires=0") +
                " WHERE run_id=? AND node_id=?",
                (to_state, time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id))
            self._append_event(conn, run_id, "node_state",
                               {"from": row["state"], "to": to_state,
                                "reason": reason}, node_id=node_id)
            return True

    def lease_node(self, run_id: str, node_id: str, *, holder: str,
                   ttl_s: float = DEFAULT_LEASE_TTL_S) -> bool:
        """Claim a READY node atomically. False means somebody else has it.

        The check and the claim are one UPDATE with the expected state in the
        WHERE clause, so two processes racing for the same node produce one
        winner and one `False` — rather than two workers running the same node
        and two sets of side effects.

        The owner and the expiry are written in the SAME statement as the state.
        Recording them afterwards would leave a window in which a node is LEASED
        by nobody, and that window is precisely when a crash makes the record
        matter.
        """
        expires = time.time() + max(1.0, float(ttl_s))
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE nodes SET state=?, updated=?, lease_owner=?, "
                "lease_expires=? WHERE run_id=? AND node_id=? AND state=?",
                (G.LEASED, time.strftime("%Y-%m-%dT%H:%M:%S"), holder, expires,
                 run_id, node_id, G.READY))
            if cur.rowcount != 1:
                return False
            self._append_event(conn, run_id, "node_leased",
                               {"holder": holder, "expires": expires},
                               node_id=node_id)
            return True

    def renew_lease(self, run_id: str, node_id: str, *, holder: str,
                    ttl_s: float = DEFAULT_LEASE_TTL_S) -> bool:
        """Extend a lease this holder already owns. False if it does not own it.

        Not called on the happy path — a node's own timeout already bounds how
        long it may legitimately hold one. It exists for a worker that knows it
        will exceed that (a long build behind a raised `timeout_s`) and can say
        so instead of having the node taken from it.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE nodes SET lease_expires=?, updated=? WHERE run_id=? "
                "AND node_id=? AND lease_owner=? AND state IN "
                "(" + ",".join("?" * len(LEASE_HOLDING_STATES)) + ")",
                (time.time() + max(1.0, float(ttl_s)),
                 time.strftime("%Y-%m-%dT%H:%M:%S"), run_id, node_id, holder,
                 *LEASE_HOLDING_STATES))
            return cur.rowcount == 1

    def node_lease(self, run_id: str, node_id: str) -> dict:
        """The lease record for one node: state, owner, expiry, and `held`."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, lease_owner, lease_expires FROM nodes "
                "WHERE run_id=? AND node_id=?", (run_id, node_id)).fetchone()
        if row is None:
            raise StoreError(f"no node {node_id!r} in run {run_id!r}")
        return {"state": row["state"], "owner": row["lease_owner"],
                "expires": row["lease_expires"],
                "held": lease_is_held(row["lease_owner"], row["lease_expires"])}

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

    # -- spans -------------------------------------------------------------
    def record_span(self, span) -> None:
        """Write one span. `INSERT OR REPLACE` so a re-ended span updates.

        Never raises into the caller's control flow: an observation that breaks
        the thing it observes is worse than a missing observation. A store error
        here is swallowed and counted, and `span_write_failures` reports it —
        silence would make the metrics quietly wrong instead of visibly short.
        """
        row = span.to_dict()
        try:
            with self._tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO spans(span_id, trace_id,"
                    " parent_span_id, kind, name, run_id, node_id, attempt,"
                    " started_ms, ended_ms, duration_ms, status, attributes)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["span_id"], row["trace_id"], row["parent_span_id"],
                     row["kind"], row["name"], row["run_id"], row["node_id"],
                     row["attempt"], row["started_ms"], row["ended_ms"],
                     row["duration_ms"], row["status"],
                     json.dumps(row["attributes"], ensure_ascii=False,
                                default=str)))
        except sqlite3.Error as exc:      # pragma: no cover - defensive
            self.span_write_failures.append(f"{row['span_id']}: {exc}")

    def spans(self, run_id: str | None = None, *, kind: str | None = None,
              limit: int = 5000) -> list[dict]:
        query = "SELECT * FROM spans"
        clauses, params = [], []
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_ms LIMIT ?"
        params.append(limit)
        with self._tx() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["attributes"] = json.loads(record["attributes"])
            out.append(record)
        return out

    # -- artifacts ---------------------------------------------------------
    def put_artifact(self, artifact: Artifact, *, path: str = "") -> None:
        """Record an artifact, refusing a state it could not have reached.

        The transition table used to be enforced on the in-memory object only,
        and this is the other door into the same state. `_promoted_inputs`
        reads THIS table to decide what a later node may consume, so the rule
        that decides what becomes an input was being checked in the one place
        that does not decide it. Demonstrated in an audit: two `put_artifact`
        calls moved one artifact PROMOTED -> REJECTED while the table declares
        PROMOTED terminal.

        No `force` parameter, deliberately. A gate with an override is the
        override.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT state, digest FROM artifacts WHERE artifact_id=?"
                " AND run_id=?",
                (artifact.artifact_id, artifact.run_id)).fetchone()
            check_artifact_write(
                row["state"] if row else None, artifact.state,
                previous_digest=row["digest"] if row else "",
                digest_=artifact.digest_)
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

    def effect_status(self, key: str) -> str | None:
        """None (never claimed), CLAIMED, or CONFIRMED.

        The distinction the runtime turns on. CONFIRMED means the effect
        provably happened and repeating it would be a duplicate. CLAIMED means
        the intent was recorded and the process did not survive to say what came
        of it — the effect may have happened, or may not. Collapsing CLAIMED
        into CONFIRMED reports a success nobody observed; collapsing it into
        None repeats an effect that may already be out in the world. It is its
        own state because it is its own situation.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT result_digest FROM effects WHERE idempotency_key=?",
                (key,)).fetchone()
        if row is None:
            return None
        return EFFECT_CONFIRMED if row["result_digest"] else EFFECT_CLAIMED

    def release_effect(self, key: str, *, reason: str) -> bool:
        """Drop a CLAIMED effect so the node may perform it after all.

        The operator half of reconciliation: for when the outside world has been
        checked and the effect did NOT happen. Refuses to touch a CONFIRMED
        effect — releasing one of those is how the same mail gets sent twice,
        and no `reason` string makes that a different outcome.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT run_id, node_id, result_digest FROM effects "
                "WHERE idempotency_key=?", (key,)).fetchone()
            if row is None:
                return False
            if row["result_digest"]:
                raise StoreError(
                    f"effect {key!r} is CONFIRMED; releasing it would permit a "
                    f"second one. Only a CLAIMED effect can be released.")
            conn.execute("DELETE FROM effects WHERE idempotency_key=?", (key,))
            self._append_event(conn, row["run_id"], "effect_released",
                               {"key": key, "reason": reason},
                               node_id=row["node_id"])
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
