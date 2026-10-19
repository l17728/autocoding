import concurrent.futures
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from autocoding.core import Core, DomainError, SCHEMA
from autocoding.delivery import Approvals, Outbox


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = pathlib.Path(self.temp.name) / 'core.db'
        self.now = 100.
        self.core = Core(self.database, clock=lambda: self.now)
        self.approvals = Approvals(self.core)
        self.core.create_session('s', 'claude', key='s')

    def approval(self, session='s', request='r', operation=None, key='a'):
        return self.approvals.register(session, 1, request, operation or {'tool': 'Write', 'path': 'a.txt'}, key=key)

    def test_upgrade_preserves_v1_data_and_backup(self):
        old = pathlib.Path(self.temp.name) / 'old.db'
        with closing(sqlite3.connect(old)) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO sessions(id,agent,owner) VALUES ('old','fixture','human')")
            conn.commit()
        upgraded = Core(old)
        self.assertEqual('old', upgraded.snapshot()['sessions'][0]['id'])
        backups = list(old.parent.glob('old.db.v1-*.bak'))
        self.assertEqual(1, len(backups))
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual('1', conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0])
        Core(old)
        self.assertEqual(1, len(list(old.parent.glob('old.db.v1-*.bak'))))

    def test_event_outbox_is_atomic_and_idempotent(self):
        self.core.create_session('s', 'claude', key='s')
        with self.core.connection() as conn:
            self.assertEqual(1, conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])
            conn.execute('BEGIN')
            self.core._event(conn, 'RolledBack', 'test')
            conn.rollback()
            self.assertEqual(1, conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0])

    def test_expired_event_lease_redelivers_same_identity(self):
        outbox = Outbox(self.core)
        first = outbox.claim(lease_seconds=1)
        self.assertIsNone(outbox.claim())
        self.now += 2
        second = outbox.claim()
        self.assertEqual(first['event']['event_id'], second['event']['event_id'])
        with self.assertRaisesRegex(DomainError, 'STALE_DELIVERY'):
            outbox.acknowledge(first)
        outbox.acknowledge(second)
        self.assertIsNone(outbox.claim())

    def test_approval_identity_and_changed_operation(self):
        a = self.approval()
        with self.assertRaisesRegex(DomainError, 'APPROVAL_OPERATION_CHANGED'):
            self.approval(operation={'tool': 'Delete'}, key='changed')
        self.core.create_session('other', 'claude', key='other')
        b = self.approval(session='other', key='b')
        self.assertNotEqual(a['id'], b['id'])
        self.assertNotIn('a.txt', str(self.core.events()))

    def test_decision_replay_and_competing_decisions(self):
        a = self.approval()
        def decide(decision):
            try:
                return self.approvals.decide(a['id'], decision, a['operation_hash'], 0, key=decision)['state']
            except DomainError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result = list(pool.map(decide, ['approved', 'denied']))
        self.assertEqual(1, result.count('STALE_APPROVAL'))
        winner = next(x for x in result if x != 'STALE_APPROVAL')
        self.assertEqual(winner, decide(winner))
        self.assertEqual(1, sum(e['type'] == 'ApprovalDecided' for e in self.core.events()))

    def test_expired_or_changed_generation_rejected(self):
        a = self.approval()
        self.now += 301
        with self.assertRaisesRegex(DomainError, 'STALE_APPROVAL'):
            self.approvals.decide(a['id'], 'approved', a['operation_hash'], 0, key='late')
        b = self.approval(request='b', key='b')
        with self.core.connection() as conn:
            conn.execute("UPDATE sessions SET generation=2 WHERE id='s'")
        with self.assertRaisesRegex(DomainError, 'STALE_APPROVAL'):
            self.approvals.decide(b['id'], 'approved', b['operation_hash'], 0, key='generation')

    def test_crash_after_decision_preserves_pending_delivery(self):
        a = self.approval()
        self.approvals.decide(a['id'], 'denied', a['operation_hash'], 0, key='deny')
        restarted = Approvals(Core(self.database, clock=lambda: self.now))
        self.assertEqual(0, restarted.recover())
        delivery = restarted.begin_delivery(a['id'])
        self.assertEqual('denied', delivery['state'])
        restarted.finish_delivery(a['id'], delivery['token'], confirmed=True)
        with self.assertRaisesRegex(DomainError, 'DELIVERY_NOT_ALLOWED'):
            restarted.begin_delivery(a['id'])

    def test_crash_during_send_never_replays_approval(self):
        a = self.approval()
        self.approvals.decide(a['id'], 'approved', a['operation_hash'], 0, key='allow')
        self.approvals.begin_delivery(a['id'])
        self.assertEqual(1, self.approvals.recover())
        self.assertEqual('reconciling', self.core.snapshot()['sessions'][0]['state'])
        with self.assertRaisesRegex(DomainError, 'DELIVERY_NOT_ALLOWED'):
            self.approvals.begin_delivery(a['id'])


if __name__ == '__main__':
    unittest.main()
