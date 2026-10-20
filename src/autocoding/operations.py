"""Persistent intents for effects that cannot share a SQLite transaction."""
import json
import uuid

from .core import DomainError, digest, encoded


def _checkpoint(core, intent, record, event):
    """Durably record a non-transactional operation stage.

    External providers cannot participate in SQLite's transaction.  The
    journal is therefore deliberately updated *before* an effect starts and
    after every known terminal outcome.  A process death between those two
    writes is conservatively reconciled instead of replayed.
    """
    with core.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE metadata SET value=? WHERE key=?",
                         (encoded(record), "operation:" + intent))
            core._event(conn, event, "owner", operation=record["operation"],
                        intent=intent, stage=record["stage"])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _claim_reserved(core, intent):
    """Claim a never-started intent exactly once, including after restart."""
    with core.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", ("operation:" + intent,)).fetchone()
            record = json.loads(row["value"])
            if record.get("state") != "reserved":
                conn.commit()
                return None
            record.update(state="running", stage="effect_started", updated_at=core.clock())
            conn.execute("UPDATE metadata SET value=? WHERE key=?", (encoded(record), "operation:" + intent))
            core._event(conn, "ExternalOperationStarted", "owner", operation=record["operation"],
                        intent=intent, stage=record["stage"])
            conn.commit()
            return record
        except BaseException:
            conn.rollback()
            raise


async def external_once(core, operation, key, payload, effect):
    fresh = False

    def reserve(conn):
        nonlocal fresh
        fresh = True
        token = uuid.uuid4().hex
        now = core.clock()
        conn.execute('INSERT INTO metadata VALUES (?,?)', ('operation:' + token, encoded({
            'state': 'reserved', 'stage': 'reserved', 'operation': operation,
            'payload_hash': digest(payload), 'created_at': now, 'updated_at': now,
        })))
        core._event(conn, 'ExternalOperationReserved', 'owner', operation=operation, intent=token)
        return {'intent': token}

    intent = core._write('owner', operation, key, payload, reserve)['intent']
    if not fresh:
        with core.connection() as conn:
            record = json.loads(conn.execute('SELECT value FROM metadata WHERE key=?',
                                ('operation:' + intent,)).fetchone()['value'])
        if record['state'] == 'completed':
            return record['result']
        if record['state'] != 'reserved':
            raise DomainError('OPERATION_RECONCILIATION_REQUIRED')

    # A reserved intent cannot have touched the provider, so it can safely be
    # claimed after a process restart. A second caller loses the claim and is
    # sent to reconciliation instead of executing the effect twice.
    record = _claim_reserved(core, intent)
    if record is None:
        raise DomainError('OPERATION_RECONCILIATION_REQUIRED')
    try:
        result = await effect()
    except BaseException as error:
        # The provider may have acted before it raised or the process may be
        # cancelled at this point. Persist only a safe error category, never
        # unredacted provider text, then require explicit reconciliation.
        record.update({
            'state': 'reconciliation_required', 'stage': 'effect_interrupted',
            'updated_at': core.clock(),
            'error_code': getattr(error, 'code', type(error).__name__),
        })
        _checkpoint(core, intent, record, 'ExternalOperationReconciliationRequired')
        raise

    record.update({
        'state': 'completed', 'stage': 'completed', 'updated_at': core.clock(), 'result': result,
    })
    _checkpoint(core, intent, record, 'ExternalOperationCompleted')
    return result
