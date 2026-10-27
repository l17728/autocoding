"""Transactional M1 core. Trusted in-process API, not an authenticated server."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from .migrations import upgrade


class DomainError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_ENCODER = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encoded(value: object) -> str:
    # JSONEncoder.encode keeps per-call traversal state local. Reuse immutable
    # configuration during large knowledge builds without changing fingerprints.
    return _ENCODER.encode(value)


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, description TEXT NOT NULL, acceptance TEXT NOT NULL,
 state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 attempt INTEGER NOT NULL DEFAULT 0, agent TEXT, lease_token TEXT,
 lease_until REAL, head_sha TEXT, verification_id TEXT,
 acceptance_hash TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS dependencies (
 task_id TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id, depends_on), CHECK(task_id <> depends_on)
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, agent TEXT NOT NULL, owner TEXT NOT NULL,
 epoch INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'ready'
);
CREATE TABLE IF NOT EXISTS commands (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 sender TEXT NOT NULL, epoch INTEGER NOT NULL, generation INTEGER NOT NULL,
 payload_hash TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
 timestamp REAL NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
 task_id TEXT, session_id TEXT, command_id TEXT, trace_id TEXT NOT NULL,
 payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
 actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
 request_hash TEXT NOT NULL, response TEXT NOT NULL,
 PRIMARY KEY(actor, operation, key)
);
"""


class Core:
    SQLITE_BUSY_TIMEOUT_MS = 15000

    """One database per project in M1. Callers are trusted local code.

    Runner, HTTP auth, actual verification execution and outbox delivery are
    later milestones. This class never launches processes or sends messages.
    """
    def __init__(self, database: str | Path, clock: Callable[[], float] = time.time):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            upgrade(self.database, conn, DomainError)

    @contextmanager
    def connection(self):
        # Agent workers and the API share the durable event ledger. Under disk
        # pressure, let short competing writes finish before failing a live run.
        conn = sqlite3.connect(self.database, isolation_level=None,
                               timeout=self.SQLITE_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.SQLITE_BUSY_TIMEOUT_MS}")
        try:
            yield conn
        finally:
            conn.close()

    def _write(self, actor, operation, key, payload, action):
        if not isinstance(actor, str) or not actor.strip() or not isinstance(key, str) or not key.strip():
            raise DomainError("IDENTITY_OR_IDEMPOTENCY_KEY_REQUIRED")
        fingerprint = digest(payload)
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    "SELECT request_hash,response FROM requests WHERE actor=? AND operation=? AND key=?",
                    (actor, operation, key),
                ).fetchone()
                if prior:
                    if prior["request_hash"] != fingerprint:
                        raise DomainError("IDEMPOTENCY_CONFLICT")
                    result = json.loads(prior["response"])
                else:
                    result = action(conn)
                    conn.execute("INSERT INTO requests VALUES (?,?,?,?,?)",
                                 (actor, operation, key, fingerprint, encoded(result)))
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    def _event(self, conn, kind, actor, *, task=None, session=None, command=None, **payload):
        # Only explicitly selected metadata enters the audit log, never input text or lease token.
        conn.execute(
            "INSERT INTO events(event_id,timestamp,type,actor,task_id,session_id,command_id,trace_id,payload) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (identifier("ev"), self.clock(), kind, actor, task, session, command,
             command or task or session or identifier("trace"), encoded(payload)),
        )

    @staticmethod
    def _get(conn, table, item_id):
        if table not in {"tasks", "sessions", "commands"}:
            raise ValueError("Unsupported table")
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND")
        return row

    def create_task(self, task_id, description, acceptance, dependencies=(), *, key):
        payload = dict(id=task_id, description=description, acceptance=acceptance, dependencies=list(dependencies))
        def action(conn):
            if not re.fullmatch(r"task_[A-Za-z0-9_-]+", task_id) or not description.strip():
                raise DomainError("INVALID_TASK")
            if not isinstance(acceptance, list) or not acceptance or any(
                not isinstance(x, str) or not x.strip() for x in acceptance
            ) or len(set(acceptance)) != len(acceptance):
                raise DomainError("ACCEPTANCE_REQUIRED")
            if len(set(dependencies)) != len(dependencies):
                raise DomainError("DUPLICATE_DEPENDENCY")
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise DomainError("TASK_EXISTS")
            for dependency in dependencies:
                if dependency == task_id:
                    raise DomainError("DEPENDENCY_CYCLE")
                self._get(conn, "tasks", dependency)
            conn.execute(
                "INSERT INTO tasks(id,description,acceptance,state,acceptance_hash) VALUES (?,?,?,'todo',?)",
                (task_id, description, encoded(acceptance), digest(acceptance)),
            )
            conn.executemany("INSERT INTO dependencies VALUES (?,?)", [(task_id, d) for d in dependencies])
            self._event(conn, "TaskCreated", "owner", task=task_id,
                        acceptance_hash=digest(acceptance), dependencies=list(dependencies))
            return {"task_id": task_id, "state": "todo"}
        return self._write("owner", "create_task", key, payload, action)

    def set_dependencies(self, task_id, dependencies, *, expected_revision, key):
        payload = dict(task=task_id, dependencies=list(dependencies), revision=expected_revision)
        def action(conn):
            task = self._get(conn, "tasks", task_id)
            if task["state"] != "todo" or task["attempt"] != 0 or task["revision"] != expected_revision:
                raise DomainError("TASK_PLAN_IMMUTABLE")
            if len(set(dependencies)) != len(dependencies):
                raise DomainError("DUPLICATE_DEPENDENCY")
            for dep in dependencies:
                self._get(conn, "tasks", dep)
                reachable = conn.execute(
                    "WITH RECURSIVE chain(id) AS (SELECT ? UNION SELECT d.depends_on "
                    "FROM dependencies d JOIN chain c ON d.task_id=c.id) SELECT 1 FROM chain WHERE id=?",
                    (dep, task_id),
                ).fetchone()
                if reachable:
                    raise DomainError("DEPENDENCY_CYCLE")
            conn.execute("DELETE FROM dependencies WHERE task_id=?", (task_id,))
            conn.executemany("INSERT INTO dependencies VALUES (?,?)", [(task_id, d) for d in dependencies])
            conn.execute("UPDATE tasks SET revision=revision+1 WHERE id=?", (task_id,))
            self._event(conn, "TaskDependenciesChanged", "owner", task=task_id, dependencies=list(dependencies))
            return {"task_id": task_id, "revision": expected_revision + 1}
        return self._write("owner", "dependencies", key, payload, action)

    def claim(self, task_id, agent, *, lease_seconds=60, key):
        payload = dict(task=task_id, lease_seconds=lease_seconds)
        def action(conn):
            task = self._get(conn, "tasks", task_id)
            if task["state"] != "todo":
                raise DomainError("TASK_NOT_READY")
            if not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds <= 3600:
                raise DomainError("INVALID_LEASE")
            waiting = conn.execute(
                "SELECT 1 FROM dependencies d JOIN tasks t ON d.depends_on=t.id "
                "WHERE d.task_id=? AND t.state!='done' LIMIT 1", (task_id,),
            ).fetchone()
            if waiting:
                raise DomainError("DEPENDENCY_NOT_READY")
            token, expiry = identifier("lease"), self.clock() + lease_seconds
            conn.execute(
                "UPDATE tasks SET state='leased',revision=revision+1,attempt=attempt+1,"
                "agent=?,lease_token=?,lease_until=?,reason=NULL WHERE id=?",
                (agent, token, expiry, task_id),
            )
            self._event(conn, "TaskLeased", agent, task=task_id, attempt=task["attempt"] + 1,
                        lease_until=expiry, lease_fingerprint=digest(token)[:12])
            return dict(task_id=task_id, state="leased", attempt=task["attempt"] + 1,
                        lease_token=token, lease_until=expiry)
        return self._write(agent, "claim", key, payload, action)

    def _active_lease(self, task, agent, token, attempt):
        if (task["state"] != "leased" or task["agent"] != agent or task["lease_token"] != token
                or task["attempt"] != attempt or task["lease_until"] <= self.clock()):
            raise DomainError("STALE_LEASE")

    def heartbeat(self, task_id, agent, token, attempt, *, key, lease_seconds=60):
        payload = dict(task=task_id, token_hash=digest(token), attempt=attempt,
                       lease_seconds=lease_seconds)
        def action(conn):
            if not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds <= 3600:
                raise DomainError("INVALID_LEASE")
            task = self._get(conn, "tasks", task_id)
            self._active_lease(task, agent, token, attempt)
            expiry = self.clock() + lease_seconds
            conn.execute("UPDATE tasks SET lease_until=? WHERE id=?", (expiry, task_id))
            self._event(conn, "LeaseRenewed", agent, task=task_id, lease_until=expiry)
            return {"lease_until": expiry}
        return self._write(agent, "heartbeat", key, payload, action)

    def report(self, task_id, agent, token, attempt, head_sha, artifact_refs, *, key):
        payload = dict(task=task_id, token_hash=digest(token), attempt=attempt,
                       head_sha=head_sha, artifact_refs=artifact_refs)
        def action(conn):
            task = self._get(conn, "tasks", task_id)
            self._active_lease(task, agent, token, attempt)
            if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", head_sha):
                raise DomainError("INVALID_COMMIT")
            if not isinstance(artifact_refs, list) or not artifact_refs or any(
                not isinstance(x, str) or not x.strip() for x in artifact_refs
            ):
                raise DomainError("EVIDENCE_REQUIRED")
            verification_id = identifier("verify")
            revision = task["revision"] + 1
            conn.execute(
                "UPDATE tasks SET state='verifying',revision=?,head_sha=?,verification_id=? WHERE id=?",
                (revision, head_sha, verification_id, task_id),
            )
            self._event(conn, "CompletionReported", agent, task=task_id, attempt=attempt,
                        head_sha=head_sha, artifact_refs=artifact_refs, verification_id=verification_id)
            return dict(task_id=task_id, state="verifying", verification_id=verification_id,
                        task_revision=revision, attempt=attempt, head_sha=head_sha,
                        acceptance_hash=task["acceptance_hash"])
        return self._write(agent, "report", key, payload, action)

    def verify(self, job, verifier, checks, *, key):
        """Trusted verifier port: caller must have actually executed the checks.

        M1 has no external authentication nor real test runner; demo/tests use
        synthetic evidence and are explicitly labelled as such.
        """
        payload = dict(job=job, checks=checks)
        def action(conn):
            task = self._get(conn, "tasks", job["task_id"])
            fields = {"verification_id": "verification_id", "task_revision": "revision",
                      "attempt": "attempt", "head_sha": "head_sha", "acceptance_hash": "acceptance_hash"}
            if task["state"] != "verifying" or any(job.get(k) != task[v] for k, v in fields.items()):
                raise DomainError("STALE_VERIFICATION")
            if not verifier.startswith("verifier:") or verifier == task["agent"]:
                raise DomainError("INDEPENDENT_VERIFIER_REQUIRED")
            criteria = json.loads(task["acceptance"])
            if not isinstance(checks, dict) or set(checks) != set(criteria) or any(
                not isinstance(result, dict) or type(result.get("passed")) is not bool
                or not isinstance(result.get("artifact_ref"), str) or not result["artifact_ref"].strip()
                for result in checks.values()
            ):
                raise DomainError("INCOMPLETE_VERIFICATION")
            passed = all(result["passed"] for result in checks.values())
            state = "done" if passed else "todo"
            conn.execute(
                "UPDATE tasks SET state=?,revision=revision+1,lease_token=NULL,lease_until=NULL,"
                "reason=?,verification_id=NULL WHERE id=?",
                (state, None if passed else "VERIFICATION_FAILED", task["id"]),
            )
            self._event(conn, "VerificationFinished", verifier, task=task["id"],
                        verification_id=job["verification_id"], passed=passed, checks=checks,
                        attempt=job["attempt"], head_sha=job["head_sha"])
            self._event(conn, "TaskDone" if passed else "TaskRequeued", verifier,
                        task=task["id"], attempt=job["attempt"])
            return {"task_id": task["id"], "state": state}
        return self._write(verifier, "verify", key, payload, action)

    def cancel_task(self, task_id, *, expected_revision, key):
        def action(conn):
            task = self._get(conn, "tasks", task_id)
            if task["revision"] != expected_revision or task["state"] in {"done", "cancelled"}:
                raise DomainError("STATE_CONFLICT")
            # Leased work requires actual Runner reconciliation, which M1 cannot prove.
            if task["state"] == "leased":
                raise DomainError("RECONCILIATION_REQUIRED")
            conn.execute("UPDATE tasks SET state='cancelled',revision=revision+1 WHERE id=?", (task_id,))
            self._event(conn, "TaskCancelled", "owner", task=task_id)
            return {"task_id": task_id, "state": "cancelled"}
        return self._write("owner", "cancel", key, dict(task=task_id, revision=expected_revision), action)

    def create_session(self, session_id, agent, *, owner="pm", simulated=True, key):
        def action(conn):
            if owner not in {"pm", "human"} or not session_id or not agent:
                raise DomainError("INVALID_SESSION")
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise DomainError("SESSION_EXISTS")
            conn.execute("INSERT INTO sessions(id,agent,owner) VALUES (?,?,?)", (session_id, agent, owner))
            self._event(conn, "SessionRegistered", "owner", session=session_id, agent=agent, simulated=simulated)
            return {"session_id": session_id, "epoch": 0, "generation": 1, "owner": owner}
        return self._write("owner", "create_session", key, dict(id=session_id, agent=agent, owner=owner, simulated=simulated), action)

    def enqueue(self, session_id, sender, epoch, text, *, key):
        payload = dict(session=session_id, epoch=epoch, text_hash=digest(text))
        def action(conn):
            session = self._get(conn, "sessions", session_id)
            if session["owner"] != sender or session["epoch"] != epoch:
                raise DomainError("STALE_INPUT_EPOCH")
            if session["state"] != "ready":
                raise DomainError("RECONCILIATION_REQUIRED")
            if not isinstance(text, str) or not text.strip():
                raise DomainError("EMPTY_INPUT")
            if len(text.encode("utf-8")) > 65536:
                raise DomainError("INPUT_TOO_LARGE")
            if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text) or "\x7f" in text:
                raise DomainError("CONTROL_SEQUENCE_REJECTED")
            queued = conn.execute("SELECT count(*) FROM commands WHERE session_id=? AND state='queued'",
                                  (session_id,)).fetchone()[0]
            if queued >= 128:
                raise DomainError('INPUT_QUEUE_FULL')
            command = identifier("cmd")
            conn.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                         (command, session_id, sender, epoch, session["generation"],
                          digest(text), "queued"))
            self._event(conn, "CommandQueued", sender, session=session_id, command=command,
                        payload_hash=digest(text), utf8_bytes=len(text.encode("utf-8")),
                        epoch=epoch, generation=session["generation"])
            return {"command_id": command, "state": "queued"}
        return self._write(sender, "enqueue", key, payload, action)

    def takeover(self, session_id, owner, expected_epoch, *, key):
        def action(conn):
            session = self._get(conn, "sessions", session_id)
            if session['state'] == 'closed':
                raise DomainError('SESSION_CLOSED')
            if owner not in {"pm", "human"}:
                raise DomainError("INVALID_INPUT_OWNER")
            if session["epoch"] != expected_epoch:
                raise DomainError("STALE_INPUT_EPOCH")
            pending = conn.execute("SELECT id,state FROM commands WHERE session_id=? "
                                   "AND state IN ('queued','dispatching','sent','sent_unconfirmed','acknowledged')",
                                   (session_id,)).fetchall()
            uncertain = any(row["state"] != "queued" for row in pending)
            for row in pending:
                state = "cancelled" if row["state"] == "queued" else "delivery_unknown"
                conn.execute("UPDATE commands SET state=? WHERE id=?", (state, row["id"]))
                self._event(conn, "CommandCancelled" if state == "cancelled" else "CommandDeliveryUnknown",
                            "owner", session=session_id, command=row["id"], reason="INPUT_TAKEOVER")
            next_state = "reconciling" if uncertain or session["state"] == "reconciling" else "ready"
            conn.execute("UPDATE sessions SET owner=?,epoch=epoch+1,state=? WHERE id=?",
                         (owner, next_state, session_id))
            self._event(conn, "InputTakenOver", "owner", session=session_id,
                        owner=owner, epoch=expected_epoch + 1, state=next_state)
            return {"session_id": session_id, "epoch": expected_epoch + 1, "owner": owner, "state": next_state}
        return self._write("owner", "takeover", key,
                           dict(session=session_id, owner=owner, epoch=expected_epoch), action)

    def begin_dispatch(self, command_id, *, key):
        """Record intent only. Does not write to an Agent or hold a process lock."""
        def action(conn):
            command = self._get(conn, "commands", command_id)
            session = self._get(conn, "sessions", command["session_id"])
            if (command["state"] != "queued" or session["state"] != "ready"
                    or command["epoch"] != session["epoch"] or command["sender"] != session["owner"]
                    or command["generation"] != session["generation"]):
                raise DomainError("DISPATCH_NOT_ALLOWED")
            other = conn.execute(
                "SELECT 1 FROM commands WHERE session_id=? AND state IN "
                "('dispatching','sent','sent_unconfirmed','acknowledged','delivery_unknown') LIMIT 1",
                (session["id"],),
            ).fetchone()
            if other:
                raise DomainError("SESSION_INPUT_BUSY")
            conn.execute("UPDATE commands SET state='dispatching' WHERE id=?", (command_id,))
            self._event(conn, "CommandDispatching", "runner", session=session["id"], command=command_id)
            return {"command_id": command_id, "state": "dispatching"}
        return self._write("runner", "begin_dispatch", key, dict(command=command_id), action)

    def recover(self, *, key):
        """Conservative M1 recovery: never reassign uncertain external work."""
        def action(conn):
            tasks = conn.execute("SELECT id FROM tasks WHERE state='leased' AND lease_until<=?",
                                 (self.clock(),)).fetchall()
            for task in tasks:
                conn.execute("UPDATE tasks SET state='blocked',revision=revision+1,"
                             "reason='RECONCILIATION_REQUIRED' WHERE id=?", (task["id"],))
                self._event(conn, "TaskBlocked", "recovery", task=task["id"], reason="RECONCILIATION_REQUIRED")
            commands = conn.execute(
                "SELECT id,session_id FROM commands WHERE state IN ('dispatching','sent','sent_unconfirmed','acknowledged')"
            ).fetchall()
            for command in commands:
                conn.execute("UPDATE commands SET state='delivery_unknown' WHERE id=?", (command["id"],))
                conn.execute("UPDATE sessions SET state='reconciling' WHERE id=? AND state!='closed'", (command["session_id"],))
                self._event(conn, "CommandDeliveryUnknown", "recovery",
                            session=command["session_id"], command=command["id"], reason="RESTART")
            self._event(conn, "RecoveryFinished", "recovery", tasks_blocked=len(tasks), commands_unknown=len(commands))
            return {"tasks_blocked": len(tasks), "commands_unknown": len(commands)}
        return self._write("recovery", "recover", key, {}, action)


    def command(self, command_id):
        """Return command metadata, never original input."""
        with self.connection() as conn:
            return dict(self._get(conn, "commands", command_id))

    def record_command(self, command_id, state, *, provider_turn_id=None, reason=None, key):
        """Trusted Runner port; records delivery, never marks a task done."""
        transitions = {
            "sent_unconfirmed": {"dispatching"},
            "acknowledged": {"dispatching", "sent"},
            "completed": {"acknowledged"},
            "failed": {"acknowledged"},
            "cancelled": {"acknowledged"},
            "delivery_unknown": {"dispatching", "sent", "sent_unconfirmed", "acknowledged"},
        }
        payload = dict(command_id=command_id, state=state, provider_turn_id=provider_turn_id, reason=reason)
        def action(conn):
            command = self._get(conn, "commands", command_id)
            session = self._get(conn, "sessions", command["session_id"])
            if state not in transitions or command["state"] not in transitions[state]:
                raise DomainError("COMMAND_STATE_CONFLICT")
            if state != "delivery_unknown" and (
                session["state"] != "ready" or session["epoch"] != command["epoch"]
                or session["generation"] != command["generation"] or session["owner"] != command["sender"]
            ):
                raise DomainError("STALE_INPUT_EPOCH")
            if state == "acknowledged" and (not isinstance(provider_turn_id, str) or not provider_turn_id):
                raise DomainError("PROVIDER_TURN_REQUIRED")
            if reason is not None and not re.fullmatch(r"[A-Z_]{1,80}", reason):
                raise DomainError("INVALID_REASON_CODE")
            conn.execute("UPDATE commands SET state=? WHERE id=?", (state, command_id))
            if state == "delivery_unknown":
                conn.execute("UPDATE sessions SET state='reconciling' WHERE id=? AND state!='closed'", (session["id"],))
            event = {"sent_unconfirmed": "CommandSentUnconfirmed", "acknowledged": "CommandAcknowledged", "completed": "CommandCompleted",
                     "failed": "CommandFailed", "cancelled": "CommandCancelled",
                     "delivery_unknown": "CommandDeliveryUnknown"}[state]
            self._event(conn, event, "runner", session=session["id"], command=command_id,
                        generation=command["generation"], epoch=command["epoch"],
                        provider_turn_id=provider_turn_id, reason=reason)
            return {"command_id": command_id, "state": state}
        return self._write("runner", "record_command", key, payload, action)


    def snapshot(self):
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                tasks = [dict(row) for row in conn.execute(
                    "SELECT id,description,state,revision,attempt,agent,head_sha,reason FROM tasks ORDER BY id")]
                sessions = [dict(row) for row in conn.execute("SELECT * FROM sessions ORDER BY id")]
                commands = [dict(row) for row in conn.execute(
                    "SELECT id,session_id,sender,epoch,generation,payload_hash,state FROM commands ORDER BY id")]
                seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
                conn.commit()
                return dict(tasks=tasks, sessions=sessions, commands=commands, latest_seq=seq)
            except BaseException:
                conn.rollback()
                raise

    def events(self, after=0, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 5000):
            raise DomainError('INVALID_EVENT_LIMIT')
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE seq>? ORDER BY seq" + (' LIMIT ?' if limit is not None else ''),
                                (after, limit) if limit is not None else (after,)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def latest_event_sequence(self):
        with self.connection() as conn:
            return conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]


