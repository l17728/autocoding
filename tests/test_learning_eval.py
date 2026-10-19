import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.learning_eval import LearningEval, score_pairs


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_holdout_revocation_during_model_call_stops_scoring(self):
        from autocoding.holdouts import Holdouts
        samples=Holdouts(self.host)
        records=[]
        for scope in ('inside','outside'):
            r=samples.create('new source',scope+' prompt',scope,key='create-'+scope)
            r=samples.answer(r['id'],{'decision':'verify'},key='answer-'+scope)
            records.append(samples.review(r['id'],r['revision'],'accepted','Source and overlap checked for this scene',key='review-'+scope))
        exported=samples.export([r['id'] for r in records],key='export')
        self.evaluation.install_holdouts(exported['id'],self.evaluation.snapshot()['fingerprint'],key='install')
        async def revoke(*args,**kwargs):
            r=records[0]
            samples.review(r['id'],r['revision'],'contaminated','Answer leaked into subsequent development context',key='revoke')
            return '{"decision":"verify"}'
        with patch.object(self.host.reflection,'references',return_value=[{'id':'rule','content':'verify'}]), \
             patch.object(self.host,'pm_text',new=AsyncMock(side_effect=revoke)) as model:
            result=await self.evaluation.run()
            self.assertEqual('failed',result['state'])
            self.assertEqual('HOLDOUT_REVIEW_INVALIDATED',result['reason'])
            self.assertEqual([],result['pairs'])
            model.assert_awaited_once()
            self.assertFalse(self.evaluation.snapshot()['reports'][0]['current'])

    async def test_knowledge_change_during_model_call_stops_scoring(self):
        self.evaluation.save_suite(self.evaluation.snapshot()['fingerprint'],self.cases,key='save')
        rules=[{'id':'rule','content':'verify'}]
        async def change(*args,**kwargs):
            rules.append({'id':'new','content':'new rule'})
            return '{"decision":"verify"}'
        with patch.object(self.host.reflection,'references',side_effect=lambda:list(rules)), \
             patch.object(self.host,'pm_text',new=AsyncMock(side_effect=change)) as model:
            result=await self.evaluation.run()
            self.assertEqual('EVALUATION_KNOWLEDGE_CHANGED',result['reason'])
            self.assertEqual([],result['pairs'])
            model.assert_awaited_once()

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.host=Host(root,root/'state')
        self.addAsyncCleanup(self.host.close)
        self.evaluation=LearningEval(self.host)
        self.model_patch=patch.object(self.evaluation,'model_identity',return_value={'provider':'fixture','model':'test'})
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.cases=[{'id':'inside','scope':'inside','prompt':'Return JSON decision','expected':{'decision':'verify'}},
                    {'id':'outside','scope':'outside','prompt':'Return JSON decision','expected':{'decision':'ask'}}]

    async def test_regression_and_insufficient_sample_cannot_promote(self):
        pairs=[{'id':str(i),'scope':'outside' if i<2 else 'inside','control':False,'treatment':True} for i in range(10)]
        self.assertTrue(score_pairs(pairs)['candidate_for_owner_review'])
        pairs[0].update(control=True,treatment=False)
        self.assertFalse(score_pairs(pairs)['candidate_for_owner_review'])
        self.assertFalse(score_pairs(pairs[:2])['candidate_for_owner_review'])
        self.assertEqual(['0'],score_pairs(pairs)['regressions'])

    async def test_counterexamples_required_and_suite_conflicts(self):
        fingerprint=self.evaluation.snapshot()['fingerprint']
        with self.assertRaisesRegex(DomainError,'EVALUATION_COUNTEREXAMPLES_REQUIRED'):
            self.evaluation.save_suite(fingerprint,[{**c,'scope':'inside'} for c in self.cases],key='invalid')
        self.evaluation.save_suite(fingerprint,self.cases,key='save')
        with self.assertRaisesRegex(DomainError,'EVALUATION_VERSION_CONFLICT'):
            self.evaluation.save_suite(fingerprint,self.cases,key='stale')

    async def test_fresh_pair_calls_and_knowledge_change_invalidates_result(self):
        self.evaluation.save_suite(self.evaluation.snapshot()['fingerprint'],self.cases,key='save')
        with patch.object(self.host.reflection,'references',return_value=[{'id':'rule','content':'verify'}]),patch.object(self.host,'pm_text',new=AsyncMock(return_value=json.dumps({'decision':'verify'}))) as model:
            report=await self.evaluation.run()
            self.assertEqual(4,model.await_count)
            self.assertEqual('completed',report['state'])
            self.assertEqual(1,report['summary']['control_passed'])
            self.assertFalse(report['summary']['production_improvement_proven'])
            self.assertTrue(self.evaluation.snapshot()['reports'][0]['current'])
        self.assertFalse(self.evaluation.snapshot()['reports'][0]['current'])

    async def test_owner_profile_change_invalidates_report_and_interrupts_pairs(self):
        self.evaluation.save_suite(self.evaluation.snapshot()['fingerprint'],self.cases,key='save')
        def update():
            current=self.host.pm_profile.snapshot()
            self.host.pm_profile.save(current['revision'],{**current['preferences'],'reporting_style':'只报告证据'},key=str(current['revision']))
        with patch.object(self.host.reflection,'references',return_value=[{'id':'rule','content':'verify'}]), \
             patch.object(self.host,'pm_text',new=AsyncMock(return_value='{"decision":"verify"}')):
            report=await self.evaluation.run()
            self.assertTrue(self.evaluation.snapshot()['reports'][0]['current'])
            update()
            self.assertFalse(self.evaluation.snapshot()['reports'][0]['current'])
            async def change_during_call(*args,**kwargs):
                update()
                return '{"decision":"verify"}'
            self.host.pm_text=AsyncMock(side_effect=change_during_call)
            interrupted=await self.evaluation.run()
            self.assertEqual('failed',interrupted['state'])
            self.assertEqual('EVALUATION_OWNER_PROFILE_CHANGED',interrupted['reason'])
            self.host.pm_text.assert_awaited_once()

    async def test_history_drafts_deduplicate_and_exclude_unsafe_or_ambiguous_answers(self):
        def record(sid,context='same task',answer='JSON',state='delivered',actor='owner',secret=False):
            with self.host.core.connection() as conn:
                self.host.interactions.record(conn,sid,kind='question',content=[{'question':'Which format?','options':['JSON','YAML']}],
                    state=state,answers=[[answer]],actor=actor,context=context,secret=secret)
        record('a');record('b')
        record('c',context='conflict',answer='JSON');record('d',context='conflict',answer='YAML')
        record('secret',secret=True);record('pending',context='pending',state='sent_unconfirmed')
        record('pm',context='pm',actor='pm');record('truncated',context='x'*4100)
        with patch.object(self.host,'pm_text',new=AsyncMock()) as model:
            drafts=self.evaluation.history_cases()
            model.assert_not_awaited()
        self.assertEqual(1,len(drafts['cases']))
        self.assertEqual({'answers':[['JSON']]},drafts['cases'][0]['expected'])
        self.assertEqual(0,drafts['model_calls'])
        self.assertIn('不能作为未见过',drafts['notice'])
        self.assertEqual([],self.evaluation.snapshot()['suite']['cases'])
