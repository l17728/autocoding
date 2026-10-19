import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from autocoding.core import DomainError, encoded
from autocoding.host import Host
from autocoding.recovery import Recovery
from autocoding.workspaces import git


class RecoveryTests(unittest.TestCase):
    def test_repair_corrupt_registration_preserves_dirty_files_and_checks_version(self):
        base=git(self.root,'rev-parse','HEAD')
        wid=self.workspace['id']
        registration=self.host.workspaces.root/(wid+'.json')
        registration.write_text('broken',encoding='utf-8')
        dirty=Path(self.workspace['path'])/'code.txt'
        dirty.write_text('keep user edits')
        evidence=self.host.workspaces.repair_evidence(self.root,wid,base)
        registration.write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(DomainError,'WORKSPACE_REPAIR_EVIDENCE_CHANGED'):
            self.host.workspaces.repair(self.root,wid,base,evidence['fingerprint'])
        evidence=self.host.workspaces.repair_evidence(self.root,wid,base)
        repaired=self.host.workspaces.repair(self.root,wid,base,evidence['fingerprint'])
        self.assertEqual(self.workspace['path'],repaired['path'])
        self.assertEqual('keep user edits',dirty.read_text())
        self.assertEqual(1,len(list(self.host.workspaces.root.glob(wid+'.registration-backup-*.json'))))

    def test_untracked_hash_batches_all_files_without_omitting_tail(self):
        root=Path(self.workspace['path'])
        for i in range(105):
            (root/f'new-{i:03}.txt').write_text(str(i))
        first=self.inspect()
        (root/'new-104.txt').write_text('changed')
        with self.assertRaisesRegex(DomainError,'RECOVERY_EVIDENCE_CHANGED'):
            self.resolve(first)

    def test_process_crash_before_commit_rolls_back_and_after_commit_replays(self):
        fingerprint=self.inspect()
        fixture=Path(__file__).parent/'fixtures/recovery_crash.py'
        for phase,code in [('before_commit',71),('after_commit',72)]:
            process=subprocess.run([sys.executable,str(fixture),str(self.root),phase,fingerprint],
                                   capture_output=True,timeout=30)
            self.assertEqual(code,process.returncode,process.stderr.decode(errors='replace'))
            with self.core.connection() as conn:
                state=conn.execute("SELECT state FROM tasks WHERE id='task_a'").fetchone()['state']
                self.assertEqual('blocked' if phase=='before_commit' else 'todo',state)
            if phase=='before_commit':
                self.assertEqual(fingerprint,self.inspect())
        replay=self.resolve(fingerprint,key='crash-recover')
        self.assertFalse(replay['execution_started'])
        self.host.recover()
        self.assertEqual('reconciled',self.host.workflow.list()[0]['state'])

    def test_concurrent_owners_allow_only_one_new_attempt(self):
        fingerprint=self.inspect()
        def attempt(key):
            try:
                return self.resolve(fingerprint,key=key)['state']
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(attempt,['owner-a','owner-b']))
        self.assertCountEqual(['todo','RECOVERY_EVIDENCE_CHANGED'],results)

    def test_missing_workspace_requires_explicit_loss_acknowledgement(self):
        old=Path(self.workspace['path']).resolve()
        moved=old.with_name(old.name+'-preserved')
        self.assertTrue(old.is_relative_to(self.root.resolve()))
        self.assertTrue(moved.is_relative_to(self.root.resolve()))
        git(self.root,'worktree','move',str(old),str(moved))
        evidence=self.recovery.inspect('run_a')
        self.assertTrue(evidence['missing_workspace'])
        with self.assertRaisesRegex(DomainError,'RECOVERY_MISSING_WORKSPACE_ACK_REQUIRED'):
            self.resolve(evidence['fingerprint'])
        result=self.recovery.resolve('run_a',evidence['fingerprint'],'Stopped and checked external effects',True,
                                     key='loss',accept_missing_workspace=True)
        self.assertFalse(result['old_workspace_preserved'])
        self.assertTrue((moved/'code.txt').exists())

    def test_orphan_task_recovery_does_not_retire_unrelated_session(self):
        with self.core.connection() as conn:
            conn.execute("DELETE FROM metadata WHERE key='run:run_a'")
        evidence=self.recovery.inspect('orphan:task_a')
        self.assertTrue(evidence['orphan'])
        result=self.recovery.resolve('orphan:task_a',evidence['fingerprint'],'Checked all related executions',True,key='orphan')
        self.assertEqual('todo',result['state'])
        with self.core.connection() as conn:
            self.assertEqual('ready',conn.execute("SELECT state FROM sessions WHERE id='s'").fetchone()['state'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        git(self.root, 'init')
        git(self.root, 'config', 'user.name', 'Fixture')
        git(self.root, 'config', 'user.email', 'fixture@example.invalid')
        (self.root/'code.txt').write_text('baseline')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'baseline')
        self.host = Host(self.root, self.root/'.local/workbench')
        self.addCleanup(lambda: asyncio.run(self.host.close()))
        self.core = self.host.core
        self.core.create_task('task_a', 'fixture', ['unit'], key='create')
        self.lease = self.core.claim('task_a', 'agent:run_a', key='claim')
        self.core.create_session('s', 'claude', owner='pm', key='session')
        self.command = self.core.enqueue('s', 'pm', 0, 'uncertain command', key='command')['command_id']
        self.workspace = self.host.workspaces.create(self.root, git(self.root, 'rev-parse', 'HEAD'))
        self.record = dict(id='run_a',task_id='task_a',agent_id='agent:run_a',session_id='s',workspace_id=self.workspace['id'])
        self.host.workflow.block(self.record, 'RUN_TIMEOUT_RECONCILIATION_REQUIRED')
        self.recovery = Recovery(self.host)

    def inspect(self):
        return self.recovery.inspect('run_a')['fingerprint']

    def resolve(self, fingerprint=None, key='recover'):
        return self.recovery.resolve('run_a', fingerprint or self.inspect(), 'Stopped and checked external effects', True, key=key)

    def test_atomic_idempotent_restart_preserves_old_command_and_workspace(self):
        fingerprint = self.inspect()
        old = self.core.command(self.command)['state']
        result = self.resolve(fingerprint)
        self.assertEqual(result, self.resolve(fingerprint))
        self.assertFalse(result['execution_started'])
        history = self.recovery.inspect('run_a')
        self.assertEqual('RECOVERY_NOT_ELIGIBLE', history['unavailable_reason'])
        self.assertEqual(fingerprint, history['resolution']['fingerprint'])
        self.assertTrue(Path(self.workspace['path']).exists())
        self.assertEqual(old, self.core.command(self.command)['state'])
        self.host.workflow.recover()
        self.assertEqual('reconciled', self.host.workflow.list()[0]['state'])
        lease = self.core.claim('task_a', 'agent:new', key='new')
        self.assertEqual(2, lease['attempt'])
        with self.assertRaises(DomainError):
            self.core.heartbeat('task_a', 'agent:run_a', self.lease['lease_token'], 1, key='late')
        with self.assertRaises(DomainError):
            self.core.enqueue('s', 'pm', 0, 'replay', key='replay')

    def test_stale_session_and_task_evidence_rejected(self):
        for table, update in [('sessions','epoch=epoch+1'), ('tasks','revision=revision+1')]:
            fingerprint = self.inspect()
            with self.core.connection() as conn:
                conn.execute('UPDATE '+table+' SET '+update)
            with self.assertRaisesRegex(DomainError, 'RECOVERY_EVIDENCE_CHANGED'):
                self.resolve(fingerprint)

    def test_untracked_content_change_invalidates_evidence(self):
        path = Path(self.workspace['path'])/'new.txt'
        path.write_text('one')
        fingerprint = self.inspect()
        path.write_text('two')
        with self.assertRaisesRegex(DomainError, 'RECOVERY_EVIDENCE_CHANGED'):
            self.resolve(fingerprint)

    def test_active_execution_and_missing_attestation_rejected(self):
        fingerprint = self.inspect()
        self.host.workflow.active['task_a'] = None
        try:
            with self.assertRaisesRegex(DomainError, 'RECOVERY_EXECUTION_ACTIVE'):
                self.resolve(fingerprint)
        finally:
            self.host.workflow.active.clear()
        with self.assertRaisesRegex(DomainError, 'RECOVERY_OWNER_ATTESTATION_REQUIRED'):
            self.recovery.resolve('run_a',fingerprint,'checked',False,key='no')

    def test_late_evidence_archived_and_rollback_is_atomic(self):
        with self.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('late_interaction:s',encoded({'operation_hash':'fixture'})))
            conn.execute("CREATE TRIGGER fail_recovery BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'fixture'); END")
        fingerprint = self.inspect()
        with self.assertRaises(Exception):
            self.resolve(fingerprint)
        with self.core.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM metadata WHERE key='recovery:run_a'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM metadata WHERE key='late_interaction:s'").fetchone())
            conn.execute('DROP TRIGGER fail_recovery')
        self.resolve(fingerprint)
        with self.core.connection() as conn:
            receipt=json.loads(conn.execute("SELECT value FROM metadata WHERE key='recovery:run_a'").fetchone()['value'])
            self.assertEqual('fixture',receipt['evidence']['late']['operation_hash'])

    def test_api_requires_auth_and_returns_evidence(self):
        from fastapi.testclient import TestClient
        from autocoding.api import create_app
        client = TestClient(create_app(self.host, 'fixture-owner'), base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        path = '/api/v1/projects/'+self.host.project_id+'/runs/run_a/recovery'
        headers = {'Authorization':'Bearer fixture-owner','Idempotency-Key':'api-recover'}
        self.assertEqual(401, client.get(path).status_code)
        response = client.get(path, headers=headers)
        self.assertEqual(200, response.status_code, response.text)
        body = dict(fingerprint=response.json()['fingerprint'], note='Stopped and reviewed', stopped=True)
        first = client.post(path, headers=headers, json=body)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(),client.post(path, headers=headers, json=body).json())
        self.assertNotEqual(200,client.post(path, headers=headers, json={**body,'note':'changed'}).status_code)

    def test_pending_question_and_approval_retired_without_delivery(self):
        q = dict(id='q',session_id='s',state='delivery_unknown',expires_at=9999999999,
                 questions=[dict(question='Proceed?',options=[],secret=False)])
        with self.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('question:q',encoded(q)))
            conn.execute("INSERT INTO approvals VALUES ('a','s',1,'p','hash','pending',0,9999999999,NULL)")
            conn.execute("INSERT INTO approval_delivery VALUES ('a','delivery_unknown',NULL)")
        self.resolve()
        with self.core.connection() as conn:
            self.assertEqual('reconciled',conn.execute("SELECT state FROM approval_delivery").fetchone()['state'])
            receipt=json.loads(conn.execute("SELECT value FROM metadata WHERE key='recovery:run_a'").fetchone()['value'])
            self.assertEqual('delivery_unknown',receipt['evidence']['questions'][0]['state'])
        self.assertFalse(self.host.workflow.unresolved_interactions('s'))

    def test_restart_and_late_unknown_receipt_never_reopen_retired_session(self):
        with self.core.connection() as conn:
            conn.execute("UPDATE commands SET state='acknowledged' WHERE id=?",(self.command,))
        self.resolve()
        self.core.record_command(self.command,'delivery_unknown',key='late-unknown')
        self.host.recover()
        with self.core.connection() as conn:
            self.assertEqual('closed',conn.execute("SELECT state FROM sessions WHERE id='s'").fetchone()['state'])
            self.assertEqual('todo',conn.execute("SELECT state FROM tasks WHERE id='task_a'").fetchone()['state'])
