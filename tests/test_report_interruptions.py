import json
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

from tools.report_interruptions import build_report


class InterruptionReportTests(unittest.TestCase):
    def test_counts_distinct_runs_recoveries_and_batch_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'platform.db'
            conn = sqlite3.connect(database)
            conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('CREATE TABLE events (timestamp REAL, type TEXT, payload TEXT)')
            now = datetime(2026, 10, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('run:one', json.dumps({
                'id': 'run-one', 'created_at': now, 'task_id': 'task_a', 'provider': 'claude',
                'state': 'reconciled', 'reason': 'RUN_DELIVERY_RECONCILIATION_REQUIRED',
                'reconciliation_reason': 'CONNECTION_LOST', 'error_type': 'ConnectionError',
                'error_message': 'provider CONNECTION_LOST', 'recovery_action': 'new_attempt'})))
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('run:two', json.dumps({
                'id': 'run-two', 'created_at': now, 'task_id': 'task_b', 'provider': 'codex',
                'state': 'rework_required', 'failed_checks': ['pm_auto']})))
            for payload in (
                {'run_id': 'run-one'}, {'run_id': 'run-one'},
            ):
                conn.execute('INSERT INTO events VALUES (?,?,?)', (now, 'PMAutoRecoveredAttempt', json.dumps(payload)))
            conn.execute('INSERT INTO events VALUES (?,?,?)',
                         (now, 'CommandDeliveryUnknown', json.dumps({'reason': 'CONNECTION_LOST'})))
            conn.execute('INSERT INTO events VALUES (?,?,?)', (now, 'TaskInterruptionRecorded', json.dumps({
                'incident_id': 'run-two', 'recovery_policy': 'platform_auto_repair'})))
            for state in ('reconciling', 'rework_required', 'rework_required'):
                conn.execute('INSERT INTO events VALUES (?,?,?)', (now, 'TaskBatchChanged', json.dumps({
                    'batch_id': 'batch-a', 'state': state, 'reason': 'BATCH_ATTEMPT_LIMIT'})))
            conn.commit()
            conn.close()

            result = build_report(database, '2026-10-23', '2026-10-24')

        self.assertEqual(2, result['interrupted_runs'])
        self.assertEqual(1, result['automatic_recovery_events'])
        self.assertEqual(1, result['delivery_unknown_events'])
        self.assertEqual(1, result['recorded_interruption_events'])
        self.assertEqual({'platform_auto_repair': 1}, result['recorded_recovery_policies'])
        self.assertEqual(2, result['stopped_batches'])
        self.assertEqual({'external_dependency': 1, 'platform_or_workflow': 1}, result['runs_by_category'])


if __name__ == '__main__':
    unittest.main()
