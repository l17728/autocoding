"""Transactional event delivery and operation-bound approval decisions.

Events may be redelivered after a lease expires. Permission responses may not:
once dispatch starts, a crash or transport failure requires reconciliation.
"""
import json
import math
from .core import DomainError, digest, identifier


class Outbox:
    def __init__(self, core):
        self.core = core

    def claim(self, *, lease_seconds=30):
        if not 0 < lease_seconds <= 300:
            raise DomainError('INVALID_LEASE')
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT * FROM outbox WHERE state='pending' OR "
                "(state='sending' AND lease_until<=?) ORDER BY id LIMIT 1", (self.core.clock(),)).fetchone()
            if row is None:
                conn.commit()
                return None
            token = identifier('delivery')
            conn.execute("UPDATE outbox SET state='sending',attempt=attempt+1,claim_token=?,lease_until=? WHERE id=?",
                         (token, self.core.clock() + lease_seconds, row['id']))
            event = dict(conn.execute('SELECT * FROM events WHERE seq=?', (row['event_seq'],)).fetchone())
            event['payload'] = json.loads(event['payload'])
            conn.commit()
            return dict(id=row['id'], token=token, event=event, destination=row['destination'])

    def acknowledge(self, delivery):
        with self.core.connection() as conn:
            changed = conn.execute("UPDATE outbox SET state='delivered',claim_token=NULL,lease_until=NULL "
                "WHERE id=? AND state='sending' AND claim_token=? AND lease_until>?",
                (delivery['id'], delivery['token'], self.core.clock())).rowcount
            if not changed:
                raise DomainError('STALE_DELIVERY')


class Approvals:
    def __init__(self, core):
        self.core = core

    def register(self, session_id, generation, provider_request_id, operation, *, ttl=300, key):
        if (not isinstance(provider_request_id, str) or not 0 < len(provider_request_id) <= 200
                or not isinstance(operation, dict) or not operation
                or type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 3600):
            raise DomainError('INVALID_APPROVAL')
        operation_hash = digest(operation)
        def action(conn):
            session = self.core._get(conn, 'sessions', session_id)
            if session['generation'] != generation or session['state'] != 'ready':
                raise DomainError('STALE_APPROVAL')
            prior = conn.execute('SELECT * FROM approvals WHERE session_id=? AND generation=? AND provider_request_id=?',
                                 (session_id, generation, provider_request_id)).fetchone()
            if prior:
                if prior['operation_hash'] != operation_hash:
                    raise DomainError('APPROVAL_OPERATION_CHANGED')
                return dict(prior)
            aid = identifier('approval')
            conn.execute('INSERT INTO approvals(id,session_id,generation,provider_request_id,operation_hash,expires_at) '
                         'VALUES (?,?,?,?,?,?)', (aid, session_id, generation, provider_request_id, operation_hash,
                                                self.core.clock() + ttl))
            self.core._event(conn, 'ApprovalRequested', 'runner', session=session_id,
                             approval_id=aid, operation_hash=operation_hash, generation=generation)
            return dict(conn.execute('SELECT * FROM approvals WHERE id=?', (aid,)).fetchone())
        return self.core._write('runner', 'approval_register', key,
            dict(session=session_id, generation=generation, request=provider_request_id,
                 operation_hash=operation_hash, ttl=ttl), action)

    def decide(self, approval_id, decision, operation_hash, expected_revision, *, key, actor='owner'):
        if actor not in {'owner','pm'}:
            raise DomainError('INVALID_APPROVAL_ACTOR')
        if decision not in {'approved', 'denied'}:
            raise DomainError('INVALID_DECISION')
        def action(conn):
            row = conn.execute('SELECT * FROM approvals WHERE id=?', (approval_id,)).fetchone()
            if row is None:
                raise DomainError('NOT_FOUND')
            session = self.core._get(conn, 'sessions', row['session_id'])
            if (row['state'] != 'pending' or row['revision'] != expected_revision
                    or row['operation_hash'] != operation_hash or row['expires_at'] <= self.core.clock()
                    or session['generation'] != row['generation'] or session['state'] != 'ready'):
                raise DomainError('STALE_APPROVAL')
            conn.execute("UPDATE approvals SET state=?,revision=revision+1,decided_by=? WHERE id=?",
                         (decision, actor, approval_id))
            conn.execute('INSERT INTO approval_delivery(approval_id) VALUES (?)', (approval_id,))
            self.core._event(conn, 'ApprovalDecided', actor, session=row['session_id'],
                             approval_id=approval_id, decision=decision, operation_hash=operation_hash)
            return dict(approval_id=approval_id, state=decision, revision=expected_revision + 1)
        return self.core._write(actor, 'approval_decide', key,
            dict(id=approval_id, decision=decision, operation_hash=operation_hash, revision=expected_revision), action)

    def begin_delivery(self, approval_id):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT a.*, d.state AS delivery_state FROM approvals a JOIN approval_delivery d '
                               'ON a.id=d.approval_id WHERE a.id=?', (approval_id,)).fetchone()
            if row is None or row['delivery_state'] != 'pending':
                raise DomainError('APPROVAL_DELIVERY_NOT_ALLOWED')
            session = self.core._get(conn, 'sessions', row['session_id'])
            if (session['generation'] != row['generation'] or session['state'] != 'ready'
                    or row['expires_at'] <= self.core.clock()):
                raise DomainError('STALE_APPROVAL')
            token = identifier('reply')
            conn.execute("UPDATE approval_delivery SET state='sending',claim_token=? WHERE approval_id=?", (token, approval_id))
            self.core._event(conn, 'ApprovalDispatching', 'runner', session=row['session_id'], approval_id=approval_id)
            conn.commit()
            return {**dict(row), 'token': token}

    def finish_delivery(self, approval_id, token, *, confirmed):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            state = 'delivered' if confirmed else 'delivery_unknown'
            changed = conn.execute("UPDATE approval_delivery SET state=? WHERE approval_id=? AND "
                                   "state='sending' AND claim_token=?", (state, approval_id, token)).rowcount
            if not changed:
                raise DomainError('STALE_DELIVERY')
            row = conn.execute('SELECT session_id FROM approvals WHERE id=?', (approval_id,)).fetchone()
            if not confirmed:
                conn.execute("UPDATE sessions SET state='reconciling' WHERE id=?", (row['session_id'],))
            self.core._event(conn, 'ApprovalDeliveryRecorded', 'runner', session=row['session_id'],
                             approval_id=approval_id, state=state)
            conn.commit()

    def recover(self):
        with self.core.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT a.id,a.session_id FROM approvals a JOIN approval_delivery d "
                                "ON a.id=d.approval_id WHERE d.state='sending'").fetchall()
            for row in rows:
                conn.execute("UPDATE approval_delivery SET state='delivery_unknown' WHERE approval_id=?", (row['id'],))
                conn.execute("UPDATE sessions SET state='reconciling' WHERE id=?", (row['session_id'],))
                self.core._event(conn, 'ApprovalDeliveryRecorded', 'recovery', session=row['session_id'],
                                 approval_id=row['id'], state='delivery_unknown')
            conn.commit()
            return len(rows)

    def list(self):
        with self.core.connection() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM approvals ORDER BY expires_at')]
