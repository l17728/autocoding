import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.acceptance import Acceptance
from autocoding.core import DomainError,digest
from autocoding.host import Host


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'.local/workbench')
        self.addCleanup(lambda:asyncio.run(self.host.close()))
        self.policy=Acceptance(self.host)
        self.config={'checks':{'unit':[sys.executable,'test.py']},'protected_paths':['test.py']}
        self.host.core.create_task('task_a','fixture',['unit'],key='task')

    def test_first_save_atomic_import_and_execution_reads_database(self):
        path=self.root/'.local/acceptance.json'
        path.write_text(json.dumps(self.config))
        current=self.policy.snapshot()
        self.assertEqual('file',current['source'])
        saved=self.policy.save(current['fingerprint'],self.config,key='save')
        self.assertEqual(saved,self.policy.save(current['fingerprint'],self.config,key='save'))
        with patch('autocoding.acceptance.Path.is_file',return_value=False):
            self.assertEqual(saved,self.policy.save(current['fingerprint'],self.config,key='save'))
        path.write_text('broken')
        self.assertEqual(self.config,self.policy.load())
        self.assertEqual(self.config['checks'],self.host.workflow.configuration('task_a')[1])
        self.assertEqual(1,saved['revision'])

    def test_file_edit_and_concurrent_saves_reject_stale_versions(self):
        old=self.policy.snapshot()['fingerprint']
        (self.root/'.local/acceptance.json').write_text(json.dumps(self.config))
        with self.assertRaisesRegex(DomainError,'ACCEPTANCE_VERSION_CONFLICT'):
            self.policy.save(old,self.config,key='stale')
        current=self.policy.snapshot()['fingerprint']
        def save(key):
            try:return self.policy.save(current,self.config,key=key)['revision']
            except DomainError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual([1,'ACCEPTANCE_VERSION_CONFLICT'],list(pool.map(save,['a','b'])))

    def test_write_failure_rolls_back_policy_and_idempotency_record(self):
        fingerprint=self.policy.snapshot()['fingerprint']
        with patch.object(self.host.core,'_event',side_effect=sqlite3.OperationalError('disk full')):
            with self.assertRaises(sqlite3.OperationalError):
                self.policy.save(fingerprint,self.config,key='retry')
        self.assertEqual(fingerprint,self.policy.snapshot()['fingerprint'])
        self.assertEqual(1,self.policy.save(fingerprint,self.config,key='retry')['revision'])

    def test_changed_policy_invalidates_active_acceptance(self):
        saved=self.policy.save(self.policy.snapshot()['fingerprint'],self.config,key='initial')
        record={'task_id':'task_a','checks_hash':digest(self.config['checks']),'protected_paths':['test.py']}
        self.host.workflow.check_policy(record)
        self.policy.save(saved['fingerprint'],{**self.config,'protected_paths':['tests/']},key='changed')
        with self.assertRaisesRegex(DomainError,'OWNER_CHECK_CONFIGURATION_CHANGED'):
            self.host.workflow.check_policy(record)

    def test_invalid_commands_and_paths_rejected_without_mutation(self):
        fingerprint=self.policy.snapshot()['fingerprint']
        for config in [{'checks':{'unit':['relative.exe']}}, {'checks':{'unit':[]}},
                       {**self.config,'protected_paths':['../outside']},
                       {**self.config,'protected_paths':['C:/outside']}]:
            with self.assertRaises(DomainError):self.policy.save(fingerprint,config,key='bad')
        self.assertEqual(fingerprint,self.policy.snapshot()['fingerprint'])

    def test_api_auth_and_missing_task_checks(self):
        from fastapi.testclient import TestClient
        from autocoding.api import create_app
        with TestClient(create_app(self.host,'test-token'),base_url='http://127.0.0.1:8765') as client:
            url='/api/v1/projects/'+self.host.project_id+'/acceptance'
            self.assertEqual(401,client.get(url).status_code)
            headers={'Authorization':'Bearer test-token','Idempotency-Key':'save'}
            current=client.get(url,headers=headers).json()
            self.assertEqual(['task_a'],current['uncovered_tasks'])
            response=client.post(url,headers=headers,json={'fingerprint':current['fingerprint'],'config':self.config})
            self.assertEqual(200,response.status_code,response.text)
            self.assertEqual([],client.get(url,headers=headers).json()['uncovered_tasks'])
