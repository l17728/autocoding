import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.holdouts import Holdouts
from autocoding.core import DomainError, encoded
from autocoding.learning_eval import LearningEval
from autocoding.redaction import Redactor


class HoldoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host=Host(self.temp.name,Path(self.temp.name)/'state')
        self.samples=Holdouts(self.host)

    def test_capture_freezes_baseline_without_leaking_into_learning(self):
        record=self.samples.create('new owner scene','PRIVATE_SCENE_MARKER','outside',key='create')
        old_profile=self.host.pm_profile.snapshot()
        preferences={**old_profile['preferences'],'reporting_style':'changed'}
        self.host.pm_profile.save(old_profile['revision'],preferences,key='profile')
        self.samples.answer(record['id'],{'answer':'PRIVATE_ANSWER_MARKER'},key='answer')
        restored=Holdouts(Host(self.temp.name,self.host.storage)).snapshot()
        with self.host.core.connection() as conn:
            baseline=json.loads(conn.execute('SELECT value FROM metadata WHERE key=?',
                ('evaluation_holdout_baseline:'+record['baseline_hash'],)).fetchone()['value'])
        self.assertEqual(old_profile,baseline['owner_profile'])
        self.assertEqual('pending_independence_review',restored['items'][0]['state'])
        self.assertEqual(0,restored['certified_holdouts'])
        self.assertEqual([],LearningEval(self.host).history_cases()['cases'])
        self.assertEqual([],self.host.interactions.relevant([{'question':'PRIVATE_SCENE_MARKER'}],''))
        self.assertEqual([],self.host.questions.list())
        evidence,_=self.host.reflection.evidence(0)
        for marker in ('PRIVATE_SCENE_MARKER','PRIVATE_ANSWER_MARKER'):
            self.assertNotIn(marker,encoded(evidence))
            self.assertNotIn(marker,encoded(self.host.reflection.references()))
            self.assertNotIn(marker,encoded(self.host.closure.list()))

    def test_idempotency_duplicate_rejection_and_immutable_answer(self):
        first=self.samples.create('source','question','inside',key='same')
        self.assertEqual(first,self.samples.create('source','question','inside',key='same'))
        with self.assertRaisesRegex(DomainError,'DUPLICATE_HOLDOUT'):
            self.samples.create('renamed source',' QUESTION! ','inside',key='duplicate')
        answer=self.samples.answer(first['id'],{'answer':'A'},key='answer')
        self.assertEqual(answer,self.samples.answer(first['id'],{'answer':'A'},key='answer'))
        with self.assertRaisesRegex(DomainError,'HOLDOUT_ALREADY_ANSWERED'):
            self.samples.answer(first['id'],{'answer':'B'},key='change')
        with self.assertRaisesRegex(DomainError,'NOT_FOUND'):
            self.samples.answer('missing',{'answer':'A'},key='missing')

    def test_reject_invalid_and_configured_secret_without_storage(self):
        self.host.redactor=Redactor(('fixture-private-key-12345',))
        with self.assertRaisesRegex(DomainError,'HOLDOUT_CONTAINS_SECRET'):
            self.samples.create('source','fixture-private-key-12345','inside',key='secret')
        with self.assertRaisesRegex(DomainError,'INVALID_HOLDOUT_SCENE'):
            self.samples.create('source',' ','inside',key='empty')
        self.assertEqual([],self.samples.snapshot()['items'])

    def reviewed(self, scope):
        r=self.samples.create('new source',scope+' scenario','inside' if scope=='inside' else 'outside',key='create-'+scope)
        r=self.samples.answer(r['id'],{'decision':scope},key='answer-'+scope)
        return self.samples.review(r['id'],r['revision'],'accepted','Verified source and semantic overlap independently',key='review-'+scope)

    def test_review_export_install_and_permanent_contamination(self):
        records=[self.reviewed(scope) for scope in ('inside','outside')]
        exported=self.samples.export([r['id'] for r in records],key='export')
        self.assertEqual(exported,self.samples.export([r['id'] for r in records],key='export'))
        evaluation=LearningEval(self.host)
        installed=evaluation.install_holdouts(exported['id'],evaluation.snapshot()['fingerprint'],key='install')
        self.assertEqual(exported['cases'],installed['cases'])
        evaluation.validate_holdouts(installed)
        r=self.samples.review(records[0]['id'],records[0]['revision'],'contaminated','Answer appeared in operational learning context',key='contaminate')
        for work in [lambda:evaluation.validate_holdouts(installed),
                     lambda:self.samples.export([x['id'] for x in records],key='bad-export'),
                     lambda:evaluation.install_holdouts(exported['id'],evaluation.snapshot()['fingerprint'],key='bad-install')]:
            with self.assertRaisesRegex(DomainError,'HOLDOUT_REVIEW_INVALIDATED'):work()
        with self.assertRaisesRegex(DomainError,'HOLDOUT_CONTAMINATION_PERMANENT'):
            self.samples.review(r['id'],r['revision'],'accepted','Attempt to restore contaminated sample',key='restore')
        self.assertNotIn('Answer appeared',encoded(self.host.reflection.evidence(0)))
        historical=self.samples.exported(exported['id'])
        self.assertFalse(historical['current'])
        self.assertEqual(exported['content_hash'],historical['content_hash'])

    def test_stale_review_and_baseline_change_require_new_review(self):
        records=[self.reviewed(scope) for scope in ('inside','outside')]
        r=records[0]
        with self.assertRaisesRegex(DomainError,'STALE_HOLDOUT_REVIEW'):
            self.samples.review(r['id'],0,'rejected','Stale browser review must not overwrite',key='stale')
        profile=self.host.pm_profile.snapshot()
        self.host.pm_profile.save(profile['revision'],{**profile['preferences'],'reporting_style':'new style'},key='changed')
        self.assertFalse(self.samples.snapshot()['items'][0]['review_current'])
        with self.assertRaisesRegex(DomainError,'HOLDOUT_REVIEW_INVALIDATED'):
            self.samples.export([x['id'] for x in records],key='stale-export')
        for r in records:
            self.samples.review(r['id'],r['revision'],'accepted','Reviewed again against the updated profile',key='again-'+r['id'])
        self.assertEqual(2,len(self.samples.export([x['id'] for x in records],key='new-export')['cases']))


if __name__=='__main__':unittest.main()
