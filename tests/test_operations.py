import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from autocoding.core import Core, DomainError, digest, encoded
from autocoding.operations import external_once


class ExternalOperationCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(Path(self.temp.name) / 'state.db')

    async def asyncTearDown(self):
        self.temp.cleanup()

    def record(self):
        with self.core.connection() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key GLOB 'operation:*'").fetchone()
        return json.loads(row['value'])

    async def test_success_has_durable_checkpoints_and_replays_result(self):
        calls = 0

        async def effect():
            nonlocal calls
            calls += 1
            return {'provider_id': 'opaque-result'}

        result = await external_once(self.core, 'provider_call', 'same-key', {'work': 'run'}, effect)
        self.assertEqual({'provider_id': 'opaque-result'}, result)
        record = self.record()
        self.assertEqual('completed', record['state'])
        self.assertEqual('completed', record['stage'])
        self.assertEqual(result, record['result'])
        self.assertIn('payload_hash', record)
        replay = await external_once(self.core, 'provider_call', 'same-key', {'work': 'run'}, effect)
        self.assertEqual(result, replay)
        self.assertEqual(1, calls)

    async def test_interrupted_effect_is_journaled_and_never_replayed(self):
        calls = 0

        async def effect():
            nonlocal calls
            calls += 1
            raise DomainError('PROVIDER_UNCERTAIN')

        with self.assertRaisesRegex(DomainError, 'PROVIDER_UNCERTAIN'):
            await external_once(self.core, 'provider_call', 'same-key', {'work': 'run'}, effect)
        record = self.record()
        self.assertEqual('reconciliation_required', record['state'])
        self.assertEqual('effect_interrupted', record['stage'])
        self.assertEqual('PROVIDER_UNCERTAIN', record['error_code'])
        with self.assertRaisesRegex(DomainError, 'OPERATION_RECONCILIATION_REQUIRED'):
            await external_once(self.core, 'provider_call', 'same-key', {'work': 'run'}, effect)
        self.assertEqual(1, calls)

    async def test_reserved_intent_resumes_once_without_replanning(self):
        payload = {'work': 'run'}
        intent = 'interrupted-before-effect'
        with self.core.connection() as conn:
            conn.execute('INSERT INTO metadata VALUES (?,?)', ('operation:' + intent, encoded({
                'state': 'reserved', 'stage': 'reserved', 'operation': 'provider_call',
                'payload_hash': digest(payload), 'created_at': 1, 'updated_at': 1,
            })))
            conn.execute('INSERT INTO requests VALUES (?,?,?,?,?)',
                         ('owner', 'provider_call', 'same-key', digest(payload), encoded({'intent': intent})))

        calls = 0
        async def effect():
            nonlocal calls
            calls += 1
            return {'resumed': True}

        self.assertEqual({'resumed': True}, await external_once(
            self.core, 'provider_call', 'same-key', payload, effect))
        self.assertEqual(1, calls)
        self.assertEqual('completed', self.record()['state'])
