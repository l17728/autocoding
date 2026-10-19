"""Persistent intents for effects that cannot share a SQLite transaction."""
import json
import uuid

from .core import DomainError, encoded


async def external_once(core, operation, key, payload, effect):
    fresh = False

    def reserve(conn):
        nonlocal fresh
        fresh = True
        token = uuid.uuid4().hex
        conn.execute('INSERT INTO metadata VALUES (?,?)',
                     ('operation:' + token, encoded({'state': 'pending'})))
        core._event(conn, 'ExternalOperationReserved', 'owner', operation=operation, intent=token)
        return {'intent': token}

    intent = core._write('owner', operation, key, payload, reserve)['intent']
    if not fresh:
        with core.connection() as conn:
            record = json.loads(conn.execute('SELECT value FROM metadata WHERE key=?',
                                ('operation:' + intent,)).fetchone()['value'])
        if record['state'] == 'completed':
            return record['result']
        raise DomainError('OPERATION_RECONCILIATION_REQUIRED')

    # Any exception/cancellation/crash leaves the intent pending. Retrying must
    # not repeat an effect that might already have happened.
    result = await effect()
    with core.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE metadata SET value=? WHERE key=?',
                     (encoded({'state': 'completed', 'result': result}), 'operation:' + intent))
        core._event(conn, 'ExternalOperationCompleted', 'owner', operation=operation, intent=intent)
        conn.commit()
    return result
