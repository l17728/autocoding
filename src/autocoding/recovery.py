"""Owner-attested retirement of uncertain attempts; never replay old input."""
import json
from pathlib import Path

from .core import DomainError, digest, encoded
from .workspaces import git


class Recovery:
    def __init__(self, host):
        self.host, self.core = host, host.core

    @staticmethod
    def untracked_hash(root, paths):
        # Bound Windows argv size and avoid one Git process per untracked file.
        pairs, chunk, length = [], [], 0
        def flush():
            if chunk:
                hashes=git(root,'hash-object','--no-filters','--',*chunk).splitlines()
                if len(hashes)!=len(chunk):
                    raise DomainError('RECOVERY_WORKSPACE_CHANGED')
                pairs.extend(zip(chunk,hashes))
        for path in paths:
            if not path:
                continue
            if chunk and (length+len(path)>6000 or len(chunk)>=100):
                flush()
                chunk, length = [], 0
            chunk.append(path)
            length+=len(path)+3
        flush()
        return digest(pairs)

    def idle(self, task_id, sid):
        if task_id in self.host.workflow.active or task_id in self.host.coordinator.reserved:
            raise DomainError('RECOVERY_EXECUTION_ACTIVE')
        runtime = self.host.runtimes.get(sid)
        consumer = self.host.consumers.get(sid)
        if (runtime and (not runtime.closed or runtime._lock.locked())
                or consumer and not consumer.done() or self.host.questions.lock.locked()):
            raise DomainError('RECOVERY_SESSION_ACTIVE')
        if any(s == sid for s, _ in self.host.deliveries):
            raise DomainError('RECOVERY_DELIVERY_ACTIVE')

    def registration(self, run_id, fingerprint=None, stopped=False):
        run=next((r for r in self.host.workflow.list() if r['id']==run_id),None)
        if not run or not run.get('workspace_id'):
            raise DomainError('RECOVERY_RUN_NOT_FOUND')
        self.idle(run['task_id'],run.get('session_id'))
        if run['state'] not in {'reconciling','failed','reconciled'}:
            raise DomainError('RECOVERY_NOT_ELIGIBLE')
        if fingerprint is None:
            return self.host.workspaces.repair_evidence(self.host.root,run['workspace_id'],run['base_sha'])
        if stopped is not True:
            raise DomainError('RECOVERY_OWNER_ATTESTATION_REQUIRED')
        result=self.host.workspaces.repair(self.host.root,run['workspace_id'],run['base_sha'],fingerprint)
        with self.core.connection() as conn:
            self.core._event(conn,'WorkspaceRegistrationRepaired','human',task=run['task_id'],run_id=run_id,
                             workspace_id=run['workspace_id'],evidence_hash=fingerprint)
        return result

    def evidence(self, conn, run_id, filesystem=None):
        row = conn.execute('SELECT value FROM metadata WHERE key=?', ('run:'+run_id,)).fetchone()
        if not row:
            if not run_id.startswith('orphan:'):
                raise DomainError('RECOVERY_RUN_NOT_FOUND')
            task_id = run_id.removeprefix('orphan:')
            task = dict(self.core._get(conn, 'tasks', task_id))
            if task['state']!='blocked' or conn.execute("SELECT 1 FROM metadata WHERE key GLOB 'run:*' "
                    "AND json_extract(value,'$.task_id')=? LIMIT 1", (task_id,)).fetchone():
                raise DomainError('RECOVERY_NOT_ORPHAN')
            run = dict(id=run_id, task_id=task_id, agent_id=task['agent'], state='reconciling',
                       created_at=0, reason=task['reason'], orphan=True)
        else:
            run = json.loads(row['value'])
        task = dict(self.core._get(conn, 'tasks', run['task_id']))
        task.pop('lease_token', None)
        sid = run.get('session_id')
        session = dict(self.core._get(conn, 'sessions', sid)) if sid else None
        commands = [dict(r) for r in conn.execute('SELECT * FROM commands WHERE session_id=? ORDER BY id', (sid,))]
        approvals = [dict(r) for r in conn.execute('SELECT a.*,d.state AS delivery_state FROM approvals a '
            'LEFT JOIN approval_delivery d ON a.id=d.approval_id WHERE session_id=? ORDER BY a.id', (sid,))]
        questions = [json.loads(r['value']) for r in conn.execute("SELECT value FROM metadata WHERE key GLOB 'question:*' ORDER BY key")]
        questions = [q for q in questions if q['session_id'] == sid]
        late = conn.execute('SELECT value FROM metadata WHERE key=?', ('late_interaction:'+str(sid),)).fetchone()
        workspace = None
        if filesystem is not None and run != filesystem['run']:
            raise DomainError('RECOVERY_EVIDENCE_CHANGED')
        if filesystem is not None:
            workspace = filesystem['workspace']
        elif run.get('workspace_id'):
            try:
                workspace = self.host.workspaces.get(run['workspace_id'])
            except DomainError as exc:
                expected = self.host.workspaces.root / run['workspace_id']
                if exc.code!='WORKSPACE_UNAVAILABLE' or expected.exists() or expected.is_symlink():
                    raise
                workspace = dict(id=run['workspace_id'],path=str(expected),availability='missing',head=None,
                                 status='WORKSPACE_MISSING')
        if filesystem is None and workspace and workspace.get('availability')!='missing':
            root = Path(workspace['path'])
            untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
            if len(untracked) > 10000:
                raise DomainError('RECOVERY_WORKSPACE_TOO_LARGE')
            workspace = {**workspace, 'head':git(root, 'rev-parse', 'HEAD'),
                'status':git(root, 'status', '--porcelain=v1'),
                'diff_hash':digest(git(root, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD')),
                'untracked_hash':self.untracked_hash(root, untracked)}
        from .acceptance import Acceptance
        policy_hash = digest(Acceptance(self.host).read(conn))
        return dict(run=run, task=task, session=session, commands=commands, approvals=approvals, policy_hash=policy_hash,
                    questions=questions, late=json.loads(late['value']) if late else None,
                    workspace=workspace, source_head=filesystem['source_head'] if filesystem is not None else git(self.host.root, 'rev-parse', 'HEAD'))

    def inspect(self, run_id):
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            evidence = self.evidence(conn, run_id)
            stored = conn.execute('SELECT value FROM metadata WHERE key=?', ('recovery:'+run_id,)).fetchone()
            receipt = json.loads(stored['value']) if stored else None
        reason = None
        try:
            self.idle(evidence['task']['id'], evidence['run'].get('session_id'))
        except DomainError as exc:
            reason = exc.code
        if evidence['task']['state'] != 'blocked' or evidence['run']['state'] != 'reconciling':
            reason = 'RECOVERY_NOT_ELIGIBLE'
        # Raw question content and filesystem diffs never leave this endpoint.
        return self.host.redactor.value(dict(run=evidence['run'], task=evidence['task'],
            session=evidence['session'], commands=evidence['commands'], approvals=evidence['approvals'],
            workspace=evidence['workspace'], source_head=evidence['source_head'],
            questions=[{'id':q['id'],'state':q['state']} for q in evidence['questions']],
            missing_workspace=bool(evidence['workspace'] and evidence['workspace'].get('availability')=='missing'),
            orphan=bool(evidence['run'].get('orphan')),
            fingerprint=digest(evidence), unavailable_reason=reason,
            resolution={'note':receipt['note'],'at':receipt['at'],'fingerprint':receipt['fingerprint']} if receipt else None))

    def resolve(self, run_id, fingerprint, note, stopped, *, key, accept_missing_workspace=False):
        if stopped is not True or not isinstance(note, str) or not 3 <= len(note.strip()) <= 2000:
            raise DomainError('RECOVERY_OWNER_ATTESTATION_REQUIRED')
        payload = dict(run_id=run_id, fingerprint=fingerprint, note_hash=digest(note), stopped=stopped)
        # Preserve replay fingerprints for receipts written before loss acknowledgement existed.
        if accept_missing_workspace:
            payload['accept_missing_workspace'] = True
        # Expensive Git reads precede the write transaction. Re-read every ledger
        # field under the write lock; reject concurrent changes rather than merge.
        # Owner stop attestation remains required: SQLite cannot lock external Git.
        with self.core.connection() as conn:
            conn.execute('BEGIN')
            prior = conn.execute('SELECT 1 FROM requests WHERE actor=? AND operation=? AND key=?',
                                 ('human', 'reconcile_attempt', key)).fetchone()
            prepared = None if prior else self.evidence(conn, run_id)
        def action(conn):
            evidence = self.evidence(conn, run_id, filesystem=prepared)
            if digest(evidence) != fingerprint:
                raise DomainError('RECOVERY_EVIDENCE_CHANGED')
            run, task = evidence['run'], evidence['task']
            sid = run.get('session_id')
            self.idle(task['id'], sid)
            if run.get('orphan') and (any(not r.closed or r._lock.locked() for r in self.host.runtimes.values())
                                     or self.host.deliveries or self.host.workflow.active or self.host.coordinator.reserved):
                raise DomainError('RECOVERY_ORPHAN_EXECUTION_ACTIVE')
            missing = bool(evidence['workspace'] and evidence['workspace'].get('availability')=='missing')
            if missing and accept_missing_workspace is not True:
                raise DomainError('RECOVERY_MISSING_WORKSPACE_ACK_REQUIRED')
            if task['state'] != 'blocked' or task['agent'] != run['agent_id'] or run['state'] != 'reconciling':
                raise DomainError('RECOVERY_NOT_ELIGIBLE')
            # Persist the original unknown states as evidence, never call them delivered.
            receipt = dict(run_id=run_id, evidence=self.host.redactor.value(evidence), fingerprint=fingerprint,
                note=self.host.redactor.text(note), owner_attested_stopped=True,
                missing_workspace_acknowledged=missing, at=self.core.clock())
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('recovery:'+run_id, encoded(receipt)))
            if sid:
                conn.execute("UPDATE sessions SET state='closed',epoch=epoch+1,generation=generation+1 WHERE id=?", (sid,))
                conn.execute("UPDATE approvals SET state='reconciled',revision=revision+1 WHERE session_id=? AND state='pending'", (sid,))
                conn.execute("UPDATE approval_delivery SET state='reconciled',claim_token=NULL WHERE approval_id IN "
                    "(SELECT id FROM approvals WHERE session_id=?) AND state IN ('pending','sending','delivery_unknown')", (sid,))
                for q in evidence['questions']:
                    if q['state'] in {'pending_pm','awaiting_owner','sending','sent_unconfirmed','delivery_unknown'}:
                        q.update(state='reconciled', reason='OWNER_RETIRED_ATTEMPT')
                        conn.execute('UPDATE metadata SET value=? WHERE key=?', (encoded(q), 'question:'+q['id']))
                        self.host.interactions.question(conn, q)
                conn.execute('DELETE FROM metadata WHERE key=?', ('late_interaction:'+sid,))
            run.update(state='reconciled', recovery_action='new_attempt', recovered_at=receipt['at'])
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('run:'+run_id, encoded(run)))
            conn.execute("UPDATE tasks SET state='todo',revision=revision+1,agent=NULL,lease_token=NULL,"
                "lease_until=NULL,head_sha=NULL,verification_id=NULL,reason=NULL WHERE id=?", (task['id'],))
            self.core._event(conn, 'OwnerReconciledAttempt', 'human', task=task['id'], session=sid,
                run_id=run_id, evidence_hash=fingerprint, note_hash=digest(note), action='new_attempt')
            return dict(task_id=task['id'], state='todo', previous_attempt=task['attempt'], run_id=run_id,
                        execution_started=False, old_workspace_preserved=not missing,
                        missing_workspace=missing, orphan=bool(run.get('orphan')))
        return self.core._write('human', 'reconcile_attempt', key, payload, action)
