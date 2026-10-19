import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import Core,DomainError
from autocoding.pm import PM,Knowledge
from autocoding.redaction import Redactor
from autocoding.mcp import Bridge,TOOL_DESCRIPTIONS


class PMTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core=Core(pathlib.Path(self.temp.name)/'core.db')
        self.pm=PM(self.core)

    def task(self,tid='task_a',deps=None):
        return {'id':tid,'description':'Implement behavior','acceptance':['unit'],'dependencies':deps or []}

    def test_out_of_order_plan_is_atomic_and_ready_depends_on_verification(self):
        self.pm.plan([self.task('task_b',['task_a']),self.task()],0,key='plan')
        self.assertEqual(['task_a'],[t['id'] for t in self.pm.ready()])
        self.assertEqual(2,len(self.core.snapshot()['tasks']))
        self.pm.plan([self.task('task_b',['task_a']),self.task()],0,key='plan')
        self.assertEqual(2,len(self.core.snapshot()['tasks']))

    def test_cycle_or_unknown_dependency_has_no_partial_tasks(self):
        for tasks in [[self.task('task_a',['task_b']),self.task('task_b',['task_a'])],
                      [self.task(),self.task('task_b',['task_missing'])]]:
            with self.assertRaises(DomainError): self.pm.plan(tasks,0,key='invalid')
            self.assertEqual([],self.core.snapshot()['tasks'])

    def test_stale_context_and_injected_shell_are_rejected(self):
        self.core.create_session('s','fixture',key='s')
        with self.assertRaisesRegex(DomainError,'STALE_PM_CONTEXT'):
            self.pm.plan([self.task()],0,key='stale')
        with self.assertRaisesRegex(DomainError,'PM_OUTPUT_INVALID'):
            self.pm.plan([{**self.task(),'shell':'delete everything'}],1,key='injected')

    def test_twelve_policy_scenarios_cannot_approve_or_mark_done(self):
        cases={'normal':'assign','repairable_failure':'rework','silent':'wait','credentials':'escalate',
            'scope_change':'escalate','cost_change':'escalate','duplicate_completion':'wait',
            'contradictory_logs':'verify','old_commit':'verify','conflict':'coordinate',
            'human_takeover':'wait','stale_context':'refresh'}
        for situation,expected in cases.items():
            with self.subTest(situation=situation):
                result=self.pm.evaluate(situation)
                self.assertEqual(expected,result['action'])
                self.assertFalse(result['may_approve'] or result['may_mark_done'] or result['may_run_shell'])

    def test_knowledge_has_commit_freshness_and_redaction(self):
        knowledge=Knowledge(self.core,Redactor(['PRIVATE_CANARY']))
        knowledge.update('architecture','Reference PRIVATE_CANARY','a'*40,['docs/design.md'],actor='pm',key='memory')
        self.assertFalse(knowledge.lookup('architecture','a'*40)['stale'])
        result=knowledge.lookup('architecture','b'*40)
        self.assertTrue(result['stale'])
        self.assertEqual('reference_only',result['trust'])
        self.assertNotIn('PRIVATE_CANARY',str(result)+str(self.core.events()))

    def test_mcp_eight_tools_and_task_identity(self):
        self.core.create_task('task_a','Implement',['unit'],key='task')
        lease=self.core.claim('task_a','agent:a',key='claim')
        a=Bridge(self.core,'agent:a',self.temp.name,Redactor())
        b=Bridge(self.core,'agent:b',self.temp.name,Redactor())
        self.assertEqual(8,len(a.handle({'id':1,'method':'tools/list'})['result']['tools']))
        self.assertEqual('task_a',a.call('get_instructions',{'task_id':'task_a'})['task_id'])
        for tool in ('get_instructions','get_context_for_task'):
            self.assertEqual(str(Path(self.temp.name).resolve()),
                             a.call(tool,{'task_id':'task_a'})['workspace_root'])
        with self.assertRaisesRegex(DomainError,'FORBIDDEN'):
            b.call('get_instructions',{'task_id':'task_a'})
        result=a.call('report_completion',{'task_id':'task_a','lease_token':lease['lease_token'],
            'attempt':lease['attempt'],'head_sha':'a'*40,'artifact_refs':['commit'],'idempotency_key':'report'})
        self.assertEqual('verifying',result['state'])
        with self.assertRaisesRegex(DomainError,'MCP_INVALID_TOOL'):
            a.call('run_shell',{'task_id':'task_a'})

    def test_mcp_contract_rejects_unknown_fields_and_wrong_types(self):
        bridge=Bridge(self.core,'agent:a',self.temp.name,Redactor())
        tools=bridge.handle({'id':1,'method':'tools/list'})['result']['tools']
        report=next(t for t in tools if t['name']=='report_completion')['inputSchema']
        self.assertIn('lease_token',report['required'])
        self.assertFalse(report['additionalProperties'])
        for args in [{'task_id':12}, {'task_id':'task_a','shell':'anything'}, {}]:
            with self.assertRaisesRegex(DomainError,'MCP_INVALID_ARGUMENTS'):
                bridge.call('get_instructions',args)

    def test_memory_write_rejects_expired_or_wrong_lease(self):
        self.core.create_task('task_a','Implement',['unit'],key='task')
        lease=self.core.claim('task_a','agent:a',key='claim')
        bridge=Bridge(self.core,'agent:a',self.temp.name,Redactor())
        args={'task_id':'task_a','lease_token':lease['lease_token'],'attempt':lease['attempt'],
              'name':'notes','content':'reference','sources':['file.py'],'idempotency_key':'memory'}
        with patch('autocoding.mcp.git',return_value='a'*40):
            with self.assertRaisesRegex(DomainError,'STALE_LEASE'):
                bridge.call('update_memory',{**args,'lease_token':'wrong'})
            self.core.clock=lambda:lease['lease_until']+1
            with self.assertRaisesRegex(DomainError,'STALE_LEASE'):
                bridge.call('update_memory',args)
        with self.core.connection() as conn:
            self.assertEqual(0,conn.execute("SELECT count(*) FROM metadata WHERE key LIKE 'knowledge:%'").fetchone()[0])

    def test_managed_mcp_scope_revokes_reads_after_lease_expiry(self):
        self.core.create_task('task_a','Implement',['unit'],key='task')
        lease=self.core.claim('task_a','agent:a',key='claim')
        bridge=Bridge(self.core,'agent:a',self.temp.name,Redactor(),task_id='task_a',attempt=lease['attempt'])
        self.assertEqual('task_a',bridge.call('get_instructions',{'task_id':'task_a'})['task_id'])
        with self.assertRaisesRegex(DomainError,'MCP_TASK_SCOPE_MISMATCH'):
            bridge.call('get_instructions',{'task_id':'task_other'})
        self.core.clock=lambda:lease['lease_until']+1
        for name,args in [('get_instructions',{'task_id':'task_a'}),('get_conventions',{})]:
            with self.assertRaisesRegex(DomainError,'STALE_LEASE'):
                bridge.call(name,args)

    def test_managed_mcp_memory_binds_lease_and_completion_stays_with_runner(self):
        self.core.create_task('task_a','Implement',['unit'],key='task')
        lease=self.core.claim('task_a','agent:a',key='claim')
        bridge=Bridge(self.core,'agent:a',self.temp.name,Redactor(),task_id='task_a',attempt=lease['attempt'])
        listed=bridge.handle({'id':1,'method':'tools/list'})['result']['tools']
        self.assertEqual(7,len(listed))
        memory=next(t for t in listed if t['name']=='update_memory')['inputSchema']
        self.assertNotIn('lease_token',memory['properties'])
        with self.assertRaisesRegex(DomainError,'MCP_INVALID_TOOL'):
            bridge.call('report_completion',{})
        args={'task_id':'task_a','name':'notes','content':'reference','sources':['file.py'],'idempotency_key':'memory'}
        with patch('autocoding.mcp.git',return_value='a'*40):
            bridge.call('update_memory',args)
            result=bridge.call('lookup',{'task_id':'task_a','name':'task_task_a_notes'})
        self.assertEqual('reference_only',result['trust'])
        self.assertNotIn(lease['lease_token'],str(result)+str(self.core.events()))
        stale=Bridge(self.core,'agent:a',self.temp.name,Redactor(),task_id='task_a',attempt=lease['attempt']+1)
        with self.assertRaisesRegex(DomainError,'STALE_LEASE'):
            stale.call('get_conventions',{})
