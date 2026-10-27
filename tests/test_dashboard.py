import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import DomainError, encoded
from autocoding.dashboard import Dashboard
from autocoding.host import Host


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.host=Host(root,root/'state')
        self.addCleanup(lambda:asyncio.run(self.host.close()))
        self.dashboard=Dashboard(self.host)

    def test_task_pages_do_not_change_internal_scheduler_view(self):
        for i in range(105):
            self.host.core.create_task('task_'+str(i).zfill(3),'Example',['unit'],key=str(i))
        first=self.dashboard.snapshot()
        second=self.dashboard.snapshot(100)
        self.assertEqual(100,len(first['tasks']))
        self.assertEqual(5,len(second['tasks']))
        self.assertEqual(105,first['totals']['tasks'])
        self.assertEqual(105,len(self.host.core.snapshot()['tasks']))
        self.assertNotIn('lease_token',first['tasks'][0])
        self.assertFalse(set(t['id'] for t in first['tasks'])&set(t['id'] for t in second['tasks']))

    def test_pending_priority_budget_and_history_boundaries(self):
        with self.host.core.connection() as conn:
            for i in range(110):
                conn.execute('INSERT INTO metadata VALUES (?,?)',('question:'+str(i),encoded({'id':str(i),'created_at':i,'state':'awaiting_owner' if i==0 else 'answered','questions':[]})))
            conn.execute('INSERT INTO metadata VALUES (?,?)',('run:large',encoded({'id':'large','state':'done','created_at':1,'content':'x'*100000})))
        snapshot=self.dashboard.snapshot()
        self.assertEqual('0',snapshot['questions'][0]['id'])
        self.assertEqual(110,snapshot['totals']['questions'])
        self.assertTrue(snapshot['runs'][0]['payload_omitted'])
        page=self.dashboard.history('questions',100,20)
        self.assertEqual(10,len(page['items']))
        self.assertIsNone(page['next_offset'])
        with self.assertRaises(DomainError):
            self.dashboard.history('unknown')
        with self.assertRaises(DomainError):
            self.dashboard.history('runs',-1)

    def test_pm_progress_is_weighted_and_only_counts_verified_tasks_as_complete(self):
        self.host.core.create_task('task_done','Verified',['unit'],key='done')
        self.host.core.create_task('task_running','Active',['unit'],key='running')
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET state='done' WHERE id='task_done'")
            conn.execute("UPDATE tasks SET state='leased' WHERE id='task_running'")
        progress=self.dashboard.snapshot()['pm_progress']
        self.assertEqual(75,progress['percent'])
        self.assertEqual(1.5,progress['completed_weight'])
        self.assertEqual(0.5,progress['remaining_equivalent_tasks'])

    def test_liveness_projection_excludes_conversation_history(self):
        self.host.core.create_task('task_live','Active task',['unit'],key='live')
        with self.host.core.connection() as conn:
            conn.execute("INSERT INTO metadata VALUES (?,?)",('batch:live',encoded({
                'id':'batch_live','state':'running','created_at':1,'active_tasks':[],
                'attempts':{'task_live':1},'conversation':'x'*100000})))
            conn.execute("UPDATE tasks SET state='leased' WHERE id='task_live'")
        result=self.dashboard.liveness()
        self.assertEqual({'leased':1},result['task_counts'])
        self.assertEqual('batch_live',result['batch']['id'])
        self.assertNotIn('conversation',encoded(result))

    def test_liveness_reports_expired_active_lease(self):
        self.host.core.create_task('task_stale','Stalled task',['unit'],key='stale')
        with self.host.core.connection() as conn:
            conn.execute("UPDATE tasks SET state='leased',lease_until=? WHERE id='task_stale'",
                         (self.host.core.clock()-1,))
            conn.execute("INSERT INTO metadata VALUES (?,?)",('batch:stale',encoded({
                'id':'batch_stale','state':'running','created_at':9999999999,
                'active_tasks':['task_stale'],'attempts':{'task_stale':1}})))
        result=self.dashboard.liveness()
        self.assertEqual(['task_stale'],result['stale_leases'])
        self.assertEqual(['task_stale'],result['batch']['active_tasks'])

    def test_delivered_command_session_counts_as_online_until_completion(self):
        session=self.host.core.create_session('session_waiting','opencode',key='session-waiting')
        command=self.host.core.enqueue(session['session_id'],'pm',session['epoch'],
                                       'continue implementation',key='waiting-input')
        self.host.core.begin_dispatch(command['command_id'],key='waiting-dispatch')
        self.host.core.record_command(command['command_id'],'acknowledged',provider_turn_id='turn_1',
                                      key='waiting-ack')

        snapshot=self.dashboard.snapshot()
        self.assertNotIn(session['session_id'],snapshot['live_ids'])
        self.assertIn(session['session_id'],snapshot['online_session_ids'])
        self.assertEqual(1,self.dashboard.liveness()['active_sessions'])

        self.host.core.record_command(command['command_id'],'completed',key='waiting-complete')
        self.assertNotIn(session['session_id'],self.dashboard.snapshot()['online_session_ids'])
        self.assertEqual(0,self.dashboard.liveness()['active_sessions'])

    def test_snapshot_limits_conversations_to_latest_run_and_bounds_payload(self):
        self.host.core.create_task('task_history','Task with retries',['unit'],key='history')
        with self.host.core.connection() as conn:
            for run_id,sid,created in (('run:old','session_old',1),('run:new','session_new',2)):
                conn.execute('INSERT INTO metadata VALUES (?,?)',(run_id,encoded({
                    'id':run_id[4:],'task_id':'task_history','session_id':sid,
                    'state':'done','created_at':created})))
                conn.execute('INSERT INTO metadata VALUES (?,?)',('session_context:'+sid,encoded({'initial':'input'})))
                conn.execute('INSERT INTO metadata VALUES (?,?)',('session_output:'+sid,encoded([
                    *[{'seq':1,'type':'tool_result','detail':'x'*20000} for _ in range(200)],
                    {'seq':2,'type':'tool_result','detail':None,'text':None}])))
        snapshot=self.dashboard.snapshot()
        runs={run['id']:run for run in snapshot['runs']}
        self.assertNotIn('conversation',runs['old'])
        self.assertLessEqual(len(runs['new']['conversation']['events']),20)
        self.assertLessEqual(len(runs['new']['conversation']['events'][-1]['detail']),2048)
        self.assertEqual('',runs['new']['conversation']['events'][-1]['detail'])
        self.assertEqual('',runs['new']['conversation']['events'][-1]['text'])
        self.assertLess(len(encoded(runs['new']).encode('utf-8')),65536)
