import json
import asyncio
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError
from autocoding.redaction import Redactor
from autocoding.transport import TransportError


class ReflectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.host=Host(self.root,self.root/'state')
        self.host.redactor=Redactor(['PRIVATE_CANARY'])
        with self.host.core.connection() as conn:
            self.host.core._event(conn,'PlanProposed','pm',task_count=2)
        self.event=self.host.core.events()[0]['event_id']

    async def asyncTearDown(self):
        await self.host.close();self.temp.cleanup()

    def response(self,evidence=None):
        return json.dumps({'summary':'Review my planning','items':[{'category':'communication',
            'content':'Give exact file paths PRIVATE_CANARY','evidence_ids':[evidence or self.event],'confidence':'hypothesis'}]})

    async def test_evidence_collection_does_not_block_other_async_work(self):
        original=self.host.reflection.evidence
        def slow_evidence(*args):
            time.sleep(0.15)
            return original(*args)
        progressed=[]
        async def heartbeat():
            await asyncio.sleep(0.01)
            progressed.append(True)
        self.host.pm_text=AsyncMock(return_value=self.response())
        ticker=asyncio.create_task(heartbeat())
        try:
            with patch.object(self.host.reflection,'evidence',slow_evidence):
                result=await self.host.reflection.run()
            self.assertEqual('completed',result['state'])
            self.assertTrue(progressed,'Evidence collection blocked the event loop')
        finally:
            await ticker

    async def test_oversized_event_keeps_identity_and_cursor_progresses(self):
        with self.host.core.connection() as conn:
            self.host.core._event(conn,'PlanProposed','pm',details='x'*60000)
        large=self.host.core.events()[-1]
        self.host.pm_text=AsyncMock(return_value=self.response(large['event_id']))
        result=await self.host.reflection.run()
        self.assertEqual('completed',result['state'])
        self.assertEqual(large['seq'],result['to_seq'])
        payload=result['evidence']['events'][-1]['payload']
        self.assertTrue(payload['evidence_truncated'])
        self.assertLess(len(json.dumps(result['evidence']).encode()),48000)
        self.assertEqual('no_new_evidence',(await self.host.reflection.run())['state'])

    async def test_recovery_includes_running_reviews_older_than_ui_page(self):
        self.host.reflection._save('pm_review:old',{'id':'old','created_at':0,'state':'running'})
        for i in range(51):
            self.host.reflection._save('pm_review:new'+str(i),{'id':'new'+str(i),'created_at':i+1,'state':'completed'})
        self.assertNotIn('old',[r['id'] for r in self.host.reflection.records()])
        self.host.reflection.recover()
        self.assertEqual('interrupted',self.host.reflection._read('pm_review:old',{})['state'])

    async def test_evidence_bound_learning_persists_archives_and_does_not_repeat(self):
        self.host.pm_text=AsyncMock(return_value=self.response())
        result=await self.host.reflection.run(automatic=True)
        self.assertEqual('completed',result['state'])
        self.assertNotIn('PRIVATE_CANARY',json.dumps(result))
        self.assertEqual('reference_only',self.host.reflection.references()[0]['trust'])
        self.assertFalse(self.host.pm_text.call_args.kwargs['include_learning'])
        self.assertEqual('not_due',(await self.host.reflection.run(automatic=True))['state'])
        self.assertEqual('no_new_evidence',(await self.host.reflection.run())['state'])
        self.assertEqual(1,self.host.pm_text.await_count)
        again=Host(self.root,self.root/'state')
        self.assertEqual(result['id'],again.reflection.references()[0]['review_id'])
        again.reflection.archive(result['id'],True)
        self.assertEqual([],self.host.reflection.references())
        await again.close()

    async def test_fabricated_evidence_is_not_knowledge_and_can_be_corrected(self):
        self.host.pm_text=AsyncMock(return_value=self.response('invented'))
        result=await self.host.reflection.run()
        self.assertEqual('failed',result['state']);self.assertEqual([],self.host.reflection.references())
        self.host.pm_text=AsyncMock(return_value=self.response())
        self.assertEqual('completed',(await self.host.reflection.run())['state'])

    async def test_cold_start_maintenance_joins_original_topic_and_requires_resolution(self):
        from autocoding.cold_start import install
        install(self.host.core)
        evidence,_=self.host.reflection.evidence(0)
        target=evidence['maintenance'][0]
        response={'summary':'Refine seed using project feedback','items':[{
            'category':'improvement','topic':'Model invented topic','content':'Apply only when current task constraints match; otherwise ask for missing context.',
            'evidence_ids':[self.event],'confidence':'hypothesis','revises':target['id']}]}
        self.host.pm_text=AsyncMock(return_value=json.dumps(response))
        result=await self.host.reflection.run()
        self.assertEqual('completed',result['state'])
        self.assertEqual(target['topic'],result['items'][0]['topic'])
        self.assertEqual('curated_cold_start',result['items'][0]['origin'])
        group=next(g for g in self.host.reflection.learning.catalogue() if g['id']==target['id'])
        self.assertEqual(2,len(group['variants']))
        self.assertTrue(all(v['state']=='conflict' for v in group['variants']))
        revised=next(v for v in group['variants'] if v.get('revises'))
        self.host.reflection.learning.resolve(group['id'],revised['id'],group['revision'])
        self.assertTrue(any(r['id']==revised['id'] for r in self.host.reflection.references()))
        self.assertEqual(8,self.host.reflection._read('pm_review_cursor',{})['maintenance_offset'])

    async def test_maintenance_rejects_unoffered_target(self):
        response={'summary':'Invalid target','items':[{'category':'principle','content':'Do something',
            'evidence_ids':[self.event],'confidence':'hypothesis','revises':'invented'}]}
        self.host.pm_text=AsyncMock(return_value=json.dumps(response))
        result=await self.host.reflection.run()
        self.assertEqual('failed',result['state'])
        self.assertEqual('STALE_KNOWLEDGE_MAINTENANCE',result['error'])

    async def test_schedule_and_pause_bound_model_calls(self):
        self.host.pm_text=AsyncMock(return_value=self.response())
        self.host.reflection.configure(False,30)
        self.assertEqual('not_due',(await self.host.reflection.run(automatic=True))['state'])
        self.host.paused=True
        with self.assertRaisesRegex(DomainError,'PROJECT_PAUSED'):
            await self.host.reflection.run()
        self.host.pm_text.assert_not_awaited()

    async def test_scheduler_survives_transient_failure_and_records_health(self):
        self.host.reflection.run=AsyncMock(side_effect=[OSError('private diagnostic'),{'state':'not_due'}])
        with patch('autocoding.reflection.asyncio.sleep',new=AsyncMock(side_effect=[None,None,asyncio.CancelledError()])):
            with self.assertRaises(asyncio.CancelledError):
                await self.host.reflection.loop()
        self.assertEqual(2,self.host.reflection.run.await_count)
        health=self.host.reflection.snapshot()['health']
        self.assertEqual('running',health['state'])
        self.assertNotIn('private diagnostic',json.dumps(health))

    async def test_failed_reviews_back_off_across_restart_without_losing_evidence(self):
        now=self.host.core.clock()
        self.host.pm_text=AsyncMock(side_effect=TimeoutError())
        with patch.object(self.host.core,'clock',return_value=now):
            self.assertEqual('failed',(await self.host.reflection.run(automatic=True))['state'])
        restarted=Host(self.root,self.root/'state')
        self.assertEqual(1,restarted.reflection._read('pm_review_cursor',{})['failures'])
        await restarted.close()
        with patch.object(self.host.core,'clock',return_value=now+3601):
            self.assertEqual('not_due',(await self.host.reflection.run(automatic=True))['state'])
        self.host.pm_text=AsyncMock(return_value=self.response())
        with patch.object(self.host.core,'clock',return_value=now+7201):
            self.assertEqual('completed',(await self.host.reflection.run(automatic=True))['state'])
        self.assertEqual(0,self.host.reflection._read('pm_review_cursor',{})['failures'])

    async def test_provider_ack_failure_keeps_only_redacted_protocol_shape(self):
        error=TransportError('CLAUDE_ACK_TIMEOUT_DELIVERY_UNKNOWN')
        error.diagnostic={'failure_phase':'ack_wait','frames_seen':1,
                          'last_frame':{'event_type':'system','frame_keys':['type','session_id']},
                          'private':'PRIVATE_CANARY'}
        self.host.pm_text=AsyncMock(side_effect=error)
        result=await self.host.reflection.run(automatic=True)
        self.assertEqual('failed',result['state'])
        self.assertEqual('ack_wait',result['error_diagnostic']['failure_phase'])
        self.assertEqual(1,result['error_diagnostic']['frames_seen'])
        self.assertNotIn('PRIVATE_CANARY',json.dumps(result))
