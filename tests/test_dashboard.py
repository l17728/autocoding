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
