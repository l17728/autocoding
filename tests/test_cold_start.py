import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.cold_start import install


def seed_count():
    return len(json.loads((ROOT/'src'/'autocoding'/'data'/'cold-start.json').read_text(encoding='utf-8'))['items'])


class ColdStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_is_idempotent_source_bound_and_preserves_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);host=Host(root,root/'state')
            try:
                first=install(host.core)
                self.assertEqual(seed_count(),first['imported'])
                self.assertEqual(seed_count(),host.reflection.learning.page()['total'])
                self.assertEqual(12,len(host.reflection.references()))
                records=host.reflection.records()
                ids={e['event_id'] for e in host.core.events()}
                for record in records:
                    self.assertFalse(record['model_generated'])
                    self.assertTrue(record['items'][0]['sources'])
                    self.assertTrue(set(record['items'][0]['evidence_ids'])<=ids)
                host.reflection.archive(records[0]['id'],True)
                self.assertEqual(0,install(host.core)['imported'])
                self.assertEqual(seed_count()-1,host.reflection.learning.page()['total'])
                self.assertEqual(seed_count(),len(host.core.events()))
                self.assertEqual('no_new_evidence',(await host.reflection.run())['state'])
            finally:
                await host.close()

    async def test_only_opted_in_hosts_get_seed_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);host=Host(root,root/'state',cold_start=True)
            try:self.assertEqual(seed_count(),len(host.reflection.records()))
            finally:await host.close()

    async def test_seed_refresh_preserves_archive_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);host=Host(root,root/'state',cold_start=True)
            try:
                record=next(r for r in host.reflection.records() if r['items'][0]['topic']=='根因与同类排查')
                record.pop('entry_hash')
                record['archived']=True
                record['items'][0]['content']='Previous seed text'
                with host.core.connection() as conn:
                    conn.execute('UPDATE metadata SET value=? WHERE key=?',
                                 (json.dumps(record),'pm_review:'+record['id']))
                result=install(host.core)
                self.assertEqual(0,result['imported'])
                self.assertEqual(1,result['updated'])
                refreshed=next(r for r in host.reflection.records() if r['id']==record['id'])
                self.assertTrue(refreshed['archived'])
                self.assertIn('递归迭代到问题收敛',refreshed['items'][0]['content'])
                self.assertEqual(0,install(host.core)['updated'])
                self.assertEqual(seed_count()+1,len(host.core.events()))
            finally:await host.close()


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('collector',ROOT/'tools/collect_user_instructions.py')
        cls.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.module)

    def test_only_user_text_not_assistant_tools_or_replays(self):
        extract=self.module.user_text
        self.assertEqual('Keep changes scoped',extract({'type':'response_item','payload':{'role':'user','content':[{'type':'input_text','text':'Keep changes scoped'}]}}))
        self.assertEqual('',extract({'type':'response_item','payload':{'role':'assistant','content':'bad'}}))
        self.assertEqual('',extract({'type':'user','isReplay':True,'message':{'role':'user','content':'duplicate'}}))
        self.assertEqual('',extract({'type':'user','message':{'role':'user','content':[{'type':'tool_result','content':'bad'}]}}))

    def test_sensitive_values_and_locations_are_redacted(self):
        value=self.module.sanitize('api_key=sk-secret-value password=hunter2 密码：abcdef C:\\private\\project user@example.com https://host/private')
        for secret in ('sk-secret-value','hunter2','abcdef','private','user@example.com','host/private'):
            self.assertNotIn(secret,value)
