import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError
from unittest.mock import patch


class LearningTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_background_build_cannot_retain_cache_after_close(self):
        import asyncio
        import threading
        from autocoding.learning import canonical_topic
        started,proceed=threading.Event(),threading.Event()
        self.review('late','Preserve evidence',topic='late fixture')
        def delayed(item):
            started.set()
            if not proceed.wait(10):raise AssertionError('test synchronization timeout')
            return canonical_topic(item)
        with patch('autocoding.learning.canonical_topic',side_effect=delayed):
            work=asyncio.create_task(asyncio.to_thread(self.r.learning.page))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait,10))
                await self.host.close()
            finally:
                proceed.set()
            self.assertEqual(1,(await work)['total'])
        self.assertIsNone(self.r.learning._cache)
        self.assertIsNone(self.r.learning._persisted)

    async def test_closed_host_releases_derived_graph_and_reopens_without_loss(self):
        from autocoding.learning import Learning
        self.review('release','Keep persisted evidence',topic='release fixture')
        expected=self.r.learning.page()
        self.r.learning.references()
        await self.host.close()
        self.assertIsNone(self.r.learning._cache)
        self.assertIsNone(self.r.learning._references_cache)
        self.assertIsNone(self.r.learning._persisted)
        self.assertEqual(expected,Learning(self.r).page())

    async def test_renamed_source_invalidates_persistent_cache(self):
        from autocoding.learning import Learning
        self.review('rename','Archive old advice',topic='rename fixture')
        self.assertEqual(1,self.r.learning.page()['total'])
        with self.host.core.connection() as conn:
            conn.execute("UPDATE metadata SET key='retired_review:rename' WHERE key='pm_review:rename'")
        self.assertEqual(0,Learning(self.r).page()['total'])

    async def test_cache_write_failure_keeps_authoritative_knowledge_available(self):
        self.review('disk','Keep source available',topic='disk fixture')
        with self.host.core.connection() as conn:
            conn.execute("CREATE TRIGGER cache_fail BEFORE INSERT ON learning_catalogue_cache "
                         "BEGIN SELECT RAISE(ABORT,'cache unavailable'); END")
        self.assertEqual(1,self.r.learning.page()['total'])
        self.assertEqual(1,len(self.r.learning.references()))

    async def test_persistent_cache_rebuilds_on_damage_revision_and_expiry(self):
        from autocoding.learning import Learning
        self.review('cache','Keep a stable rule',topic='cache fixture')
        expected=self.r.learning.page()
        replacement=Learning(self.r)
        with patch('autocoding.learning.canonical_topic',side_effect=AssertionError('recomputed')):
            self.assertEqual(expected,replacement.page())
        with self.host.core.connection() as conn:
            conn.execute("UPDATE learning_catalogue_cache SET payload='broken'")
        self.assertEqual(expected,Learning(self.r).page())
        self.review('cache-new','Another rule',topic='new scope')
        self.assertEqual(expected['total']+1,Learning(self.r).page()['total'])
        now=self.host.core.clock()
        with patch.object(self.host.core,'clock',return_value=now+91*86400):
            expired=Learning(self.r).page()
            self.assertTrue(all(v['state']=='expired' for g in expired['items'] for v in g['variants']))

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.host=Host(Path(self.temp.name),Path(self.temp.name)/'state')
        self.r=self.host.reflection

    async def asyncTearDown(self):
        await self.host.close()
        self.temp.cleanup()

    def review(self,rid,content,topic='test strategy',age=0):
        self.r._save('pm_review:'+rid,{'id':rid,'created_at':self.r.core.clock()-age,'state':'completed',
            'archived':False,'items':[{'category':'lesson','topic':topic,'content':content,
            'confidence':'observed','evidence_ids':['event_fixture']}]})

    async def test_duplicates_merge_and_conflicts_require_current_resolution(self):
        self.review('a','Run tests first')
        self.review('b','Ｒun   tests first')
        self.assertEqual(1,len(self.r.references()))
        group=self.r.learning.catalogue()[0]
        self.assertEqual(2,len(group['variants'][0]['sources']))
        old=group['revision']
        self.review('c','Skip tests first')
        self.assertEqual([],self.r.references())
        group=self.r.learning.catalogue()[0]
        selected=next(v['id'] for v in group['variants'] if v['content']=='Run tests first')
        with self.assertRaisesRegex(DomainError,'STALE_KNOWLEDGE'):
            self.r.learning.resolve(group['id'],selected,old)
        self.r.learning.resolve(group['id'],selected,group['revision'])
        self.assertEqual('Run tests first',self.r.references()[0]['content'])
        self.r.used_references()
        selected_event=self.r.core.events()[-1]
        self.assertEqual('PMReviewKnowledgeUsed',selected_event['type'])
        self.assertEqual('selected',selected_event['payload']['stage'])
        self.review('d','Run only quick tests')
        self.assertEqual([],self.r.references())

    async def test_expiry_archive_and_knowledge_survives_review_page_limit(self):
        self.review('old','Old rule',age=91*86400)
        self.assertEqual([],self.r.references())
        self.review('fresh','Fresh rule',topic='fresh')
        for i in range(55):
            self.r._save('pm_review:failed'+str(i),{'id':str(i),'created_at':self.r.core.clock(),
                'state':'failed','archived':False,'items':[]})
        self.assertEqual('Fresh rule',self.r.references()[0]['content'])
        self.r.archive('fresh',True)
        self.assertEqual([],self.r.references())
        self.r.archive('fresh',False)
        self.assertEqual(1,len(self.r.references()))

    async def test_outcomes_deduplicate_runs_and_refuse_small_sample_claims(self):
        def outcome(rid,state):
            with self.r.core.connection() as conn:
                self.r.core._event(conn,'TaskRunChanged','runner',run_id=rid,state=state)
        self.assertIsNone(self.r.learning.outcomes()['current']['score'])
        for i in range(40):
            outcome(str(i),'done' if i>=20 else 'rework_required')
        for _ in range(5):
            outcome('39','done')
        result=self.r.learning.outcomes()
        self.assertEqual(20,result['current']['samples'])
        self.assertEqual(100,result['delta'])
        self.assertEqual('comparable',result['status'])
        self.review('only','Recommendation')
        self.assertEqual(100,self.r.learning.outcomes()['delta'])

    async def test_resolution_persists_and_is_audited(self):
        self.review('a','A');self.review('b','B')
        group=self.r.learning.catalogue()[0]
        self.r.learning.resolve(group['id'],group['variants'][0]['id'],group['revision'])
        other=Host(self.host.root,self.host.storage)
        try:
            self.assertEqual(self.r.references(),other.reflection.references())
            self.assertEqual('PMReviewKnowledgeResolved',self.r.core.events()[-1]['type'])
        finally:
            await other.close()

    async def test_cross_topic_bilingual_candidates_do_not_auto_delete(self):
        self.review('a','Always run tests before delivery','quality gate')
        self.review('b','交付前跳过测试','交付速度')
        groups=self.r.learning.catalogue()
        self.assertEqual(1,len(groups))
        self.assertTrue(all(v['state']=='conflict' for v in groups[0]['variants']))
        self.assertEqual([],self.r.references())
        self.review('c','Use short paragraphs','communication')
        self.assertEqual('Use short paragraphs',self.r.references()[0]['content'])
        self.assertEqual(3,len(self.r.records()))

    async def test_cache_invalidates_for_other_host_and_time_and_page_is_detached(self):
        self.review('a','Fresh rule','fresh')
        page=self.r.learning.page()
        page['items'][0]['variants'][0]['content']='corrupted'
        self.assertEqual('Fresh rule',self.r.references()[0]['content'])
        other=Host(self.host.root,self.host.storage)
        try:
            other.reflection.archive('a',True)
            self.assertEqual([],self.r.references())
            other.reflection.archive('a',False)
            self.assertEqual(1,len(self.r.references()))
        finally:
            await other.close()
        now=self.r.core.clock()
        with patch.object(self.r.core,'clock',return_value=now+91*86400):
            self.assertEqual([],self.r.references())
        self.assertEqual(1,len(self.r.references()))
        with self.assertRaisesRegex(DomainError,'INVALID_KNOWLEDGE_PAGE'):
            self.r.learning.page(-1)

    async def test_bilingual_conflict_domains_and_unrelated_advice(self):
        pairs=[('Run tests before delivery','交付前不要测试'),
               ('Require human approval before deletion','删除文件无需审批'),
               ('Never replay unknown delivery','未确认输入需要重放'),
               ('Never print API keys','记录密钥便于排查')]
        for i,(left,right) in enumerate(pairs):
            with self.subTest(domain=i):
                self.review('left'+str(i),left,'English scope '+str(i))
                self.review('right'+str(i),right,'中文主题 '+str(i))
                self.assertEqual([],self.r.references())
        self.review('unrelated','Keep paragraphs short','沟通长度')
        self.assertEqual(['Keep paragraphs short'],[r['content'] for r in self.r.references()])
        self.assertEqual(9,len(self.r.records()))
