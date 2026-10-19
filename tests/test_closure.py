import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError
from closure_support import persist_batch


class ClosureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'state')
        self.addAsyncCleanup(self.host.close)
        self.host.core.create_task('task_a','Example feature',['unit'],key='task')
        self.batch={'id':'batch_fixture','task_ids':['task_a'],'state':'verified_candidate',
                    'candidate_sha':'a'*40,'artifact':'test-report.json','artifact_sha256':'b'*64}

    def done(self):
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET state='done',head_sha=? WHERE id='task_a'",('a'*40,))
        persist_batch(self.host,self.batch)

    async def test_registered_requirement_without_evidence_defers_closure(self):
        from autocoding.coverage import Coverage
        self.done()
        coverage=Coverage(self.host)
        coverage.save(coverage.snapshot()['fingerprint'],[{'id':'R1','description':'No test mapping yet','cases':[]}],key='requirement')
        self.assertIsNone(self.host.closure.create(self.batch))

    async def test_forged_missing_or_tampered_integration_is_rejected(self):
        self.done()
        with self.assertRaises(DomainError):
            self.host.closure.create({**self.batch,'candidate_sha':'f'*40})
        artifact=Path(self.batch['artifact'])
        artifact.write_text('{}',encoding='utf-8')
        with self.assertRaises(DomainError):
            self.host.closure.create(self.batch)
        persist_batch(self.host,self.batch)
        with self.host.core.connection() as conn:
            conn.execute("UPDATE events SET type='InvalidatedFixture' WHERE type='IntegrationVerified'")
        with self.assertRaises(DomainError):
            self.host.closure.create(self.batch)
        self.assertEqual([],self.host.closure.list())

    async def test_new_done_task_is_not_covered_by_old_candidate(self):
        self.done()
        self.host.core.create_task('task_b','Later feature',['unit'],key='later')
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET state='done',head_sha=? WHERE id='task_b'",('b'*40,))
        self.assertIsNone(self.host.closure.create(self.batch))
        self.assertEqual([],self.host.closure.list())

    async def test_task_revision_change_invalidates_candidate(self):
        self.done()
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET revision=revision+1 WHERE id='task_a'")
        self.assertIsNone(self.host.closure.create(self.batch))

    async def test_historical_report_does_not_claim_current_completion(self):
        self.done()
        report=self.host.closure.create(self.batch)
        self.host.core.create_task('task_b','Later feature',['unit'],key='later')
        self.assertIsNone(self.host.closure.create(self.batch))
        history=self.host.closure.list()
        self.assertEqual(report['id'],history[0]['id'])
        self.assertFalse(history[0]['task_snapshot_current'])

    async def test_new_unresolved_interaction_defers_existing_report(self):
        self.done()
        self.host.closure.create(self.batch)
        with self.host.core.connection() as conn:
            self.host.interactions.record(conn,'q_new',kind='question',content='Example?',state='delivery_unknown',requested=True)
        self.assertIsNone(self.host.closure.create(self.batch))

    async def test_incomplete_project_or_unverified_batch_cannot_close(self):
        self.assertIsNone(self.host.closure.create(self.batch))
        with self.assertRaises(DomainError):
            self.host.closure.create({**self.batch,'state':'rework_required'})
        self.assertEqual([],self.host.closure.list())

    async def test_closure_persists_failure_patterns_knowledge_and_is_idempotent(self):
        self.done()
        self.host.workflow.save({'id':'r1','task_id':'task_a','created_at':1,'failed_checks':['unit']},'rework_required')
        self.host.workflow.save({'id':'r2','task_id':'task_a','created_at':2},'done')
        report=self.host.closure.create(self.batch)
        self.assertEqual({'unit':1},report['patterns'])
        self.assertTrue(report['bugfix_review'][0]['later_verified'])
        self.assertFalse(report['published'])
        self.assertEqual(report,self.host.closure.create(self.batch))
        self.assertEqual(1,len(self.host.closure.list()))
        self.assertEqual(1,sum(e['type']=='ProjectClosureCreated' for e in self.host.core.events()))
        review=self.host.reflection.records()[0]
        self.assertEqual(report['knowledge_review_id'],review['id'])
        self.assertEqual(2,len(review['items']))
        again=Host(self.root,self.root/'state')
        try:self.assertEqual(report,again.closure.list()[0])
        finally:await again.close()
        evidence,useful=self.host.reflection.evidence(0)
        self.assertTrue(useful)
        self.assertEqual(report['id'],evidence['closure_reports'][0]['id'])

    async def test_unresolved_question_prevents_project_closure(self):
        self.done()
        with self.host.core.connection() as conn:
            self.host.interactions.record(conn,'q1',kind='question',content='Example?',state='delivery_unknown',requested=True)
        self.assertIsNone(self.host.closure.create(self.batch))

    async def test_report_is_redacted_and_keeps_original_task_ledger(self):
        self.done()
        from autocoding.redaction import Redactor
        self.host.redactor=Redactor(['PRIVATE_CANARY'])
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET description='PRIVATE_CANARY' WHERE id='task_a'")
        report=self.host.closure.create(self.batch)
        self.assertNotIn('PRIVATE_CANARY',json.dumps(report))
        self.assertNotIn('PRIVATE_CANARY',json.dumps(self.host.closure.list()))
        self.assertEqual('PRIVATE_CANARY',self.host.core.snapshot()['tasks'][0]['description'])
