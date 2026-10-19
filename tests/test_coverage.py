import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from autocoding.acceptance import Acceptance
from autocoding.core import DomainError, digest, encoded
from autocoding.coverage import Coverage
from autocoding.host import Host


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = Host(self.root, self.root / '.local/workbench')
        self.addCleanup(lambda: asyncio.run(self.host.close()))
        self.core = self.host.core
        self.core.create_task('task_a', 'example', ['unit'], key='task')
        self.coverage = Coverage(self.host)
        self.requirements = [{'id': 'R1', 'description': 'example requirement', 'cases': [{'task_id': 'task_a', 'criterion': 'unit'}]}]
        self.coverage.save(self.coverage.snapshot()['fingerprint'], self.requirements, key='map')
        self.policy = Acceptance(self.host)
        self.config = {'checks': {'unit': [sys.executable, 'test.py']}, 'protected_paths': []}
        self.policy.save(self.policy.snapshot()['fingerprint'], self.config, key='policy')

    def verified(self):
        lease = self.core.claim('task_a', 'agent:a', key='claim')
        job = self.core.report('task_a', 'agent:a', lease['lease_token'], lease['attempt'], 'a'*40, ['artifact:source'], key='report')
        raw = encoded({'job': job, 'checks': {'unit': {'passed': True, 'argv_hash': digest(self.config['checks']['unit'])}}}).encode()
        sha = hashlib.sha256(raw).hexdigest()
        self.artifact = self.root / 'evidence.json'
        self.artifact.write_bytes(raw)
        self.core.verify(job, 'verifier:test', {'unit': {'passed': True, 'artifact_ref': 'sha256:'+sha}}, key='verify')
        with self.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('run:fixture', encoded({'id':'fixture','state':'done','created_at':1,'task_id': 'task_a', 'head_sha': 'a'*40, 'artifact': str(self.artifact), 'artifact_sha256': sha, 'protected_paths': []})))

    def test_current_audited_evidence_and_policy_change(self):
        self.assertFalse(self.coverage.snapshot()['registered_scope_passed'])
        self.verified()
        self.assertTrue(self.coverage.snapshot()['registered_scope_passed'])
        from closure_support import persist_batch
        batch={'id':'batch_fixture','task_ids':['task_a'],'state':'verified_candidate','candidate_sha':'a'*40}
        persist_batch(self.host,batch)
        self.assertIsNotNone(self.host.closure.create(batch))
        changed = {'checks': {'unit': [sys.executable, 'other.py']}, 'protected_paths': []}
        self.policy.save(self.policy.snapshot()['fingerprint'], changed, key='changed')
        self.assertFalse(self.coverage.snapshot()['registered_scope_passed'])
        self.assertIsNone(self.host.closure.create(batch))

    def test_corruption_and_task_revision_invalidate_evidence(self):
        self.verified()
        raw = self.artifact.read_bytes()
        self.artifact.write_text('{}')
        self.assertFalse(self.coverage.snapshot()['registered_scope_passed'])
        self.artifact.write_bytes(raw)
        with self.core.connection() as conn:
            conn.execute('UPDATE tasks SET revision=revision+1')
        self.assertFalse(self.coverage.snapshot()['registered_scope_passed'])

    def test_mapping_validation_conflict_and_uncovered_requirement(self):
        fingerprint = self.coverage.snapshot()['fingerprint']
        record = self.coverage.save(fingerprint, self.requirements + [{'id': 'R2', 'description': 'uncovered', 'cases': []}], key='second')
        self.assertFalse(self.coverage.snapshot()['registered_scope_passed'])
        self.assertEqual(record, self.coverage.save(fingerprint, self.requirements + [{'id': 'R2', 'description': 'uncovered', 'cases': []}], key='second'))
        with self.assertRaisesRegex(DomainError, 'COVERAGE_VERSION_CONFLICT'):
            self.coverage.save(fingerprint, [], key='stale')
        with self.assertRaisesRegex(DomainError, 'INVALID_COVERAGE_CASE'):
            self.coverage.save(record['fingerprint'], [{'id': 'bad', 'description': 'bad', 'cases': [{'task_id': 'task_a', 'criterion': 'nonexistent'}]}], key='invalid')
