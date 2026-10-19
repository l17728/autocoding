"""Forward-only SQLite upgrades with a pre-upgrade SQLite backup."""
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


V2 = [
    """CREATE TABLE outbox (
        id INTEGER PRIMARY KEY, event_seq INTEGER NOT NULL REFERENCES events(seq),
        destination TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        attempt INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until REAL,
        UNIQUE(event_seq,destination))""",
    """CREATE TRIGGER event_outbox AFTER INSERT ON events BEGIN
        INSERT INTO outbox(event_seq,destination) VALUES (NEW.seq,'events'); END""",
    "INSERT INTO outbox(event_seq,destination) SELECT seq,'events' FROM events",
    """CREATE TABLE approvals (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
        generation INTEGER NOT NULL, provider_request_id TEXT NOT NULL,
        operation_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        revision INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL,
        decided_by TEXT, UNIQUE(session_id,generation,provider_request_id))""",
    """CREATE TABLE approval_delivery (
        approval_id TEXT PRIMARY KEY REFERENCES approvals(id),
        state TEXT NOT NULL DEFAULT 'pending', claim_token TEXT)""",
    "CREATE TABLE migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)",
]


def upgrade(database: Path, conn, error):
    version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
    if version == '2':
        return
    if version != '1':
        raise error('SCHEMA_VERSION_UNSUPPORTED')
    backup = database.with_name(database.name + '.v1-' + uuid.uuid4().hex + '.bak')
    with closing(sqlite3.connect(backup)) as target:
        conn.backup(target)
    conn.execute('BEGIN IMMEDIATE')
    try:
        # Another process may have upgraded while the backup was being written.
        version = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
        if version == '1':
            for statement in V2:
                conn.execute(statement)
            conn.execute('INSERT INTO migrations VALUES (2,?)', (time.time(),))
            conn.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        elif version != '2':
            raise error('SCHEMA_VERSION_UNSUPPORTED')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
