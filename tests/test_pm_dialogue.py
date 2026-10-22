import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from autocoding.core import DomainError
from autocoding.host import Host


class PMDialogueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.host = Host(root, root / 'state')

    async def asyncTearDown(self):
        await self.host.close()
        self.temp.cleanup()

    async def test_status_query_is_saved_and_returns_project_evidence(self):
        self.host.core.create_task('task_waiting', 'Waiting task', ['unit'], key='task')
        result = await self.host.pm_dialogue.submit('message_0001', '现在项目进度如何？')
        self.assertEqual('answered', result['action'])
        self.assertEqual('owner', result['owner_message']['actor'])
        self.assertEqual('pm', result['reply']['actor'])
        self.assertIn('总体进度', result['reply']['content'])
        persisted = self.host.pm_dialogue.list()['items']
        self.assertEqual(['owner', 'pm'], [m['actor'] for m in persisted])
        self.assertEqual(result['reply']['content'], persisted[-1]['content'])

    async def test_pause_is_audited_and_unknown_instructions_do_not_claim_execution(self):
        paused = await self.host.pm_dialogue.submit('message_0002', '暂停派发')
        self.assertEqual('paused', paused['action'])
        self.assertTrue(self.host.paused)
        recorded = await self.host.pm_dialogue.submit('message_0003', '请关注这项约束')
        self.assertEqual('recorded', recorded['action'])
        self.assertIn('尚未触发', recorded['reply']['content'])
        self.assertTrue(any(e['type'] == 'ProjectPauseChanged' for e in self.host.core.events()))

    async def test_invalid_page_and_empty_message_are_rejected(self):
        with self.assertRaises(DomainError):
            self.host.pm_dialogue.list(-1)
        with self.assertRaises(DomainError):
            await self.host.pm_dialogue.submit('message_0004', '   ')

    async def test_intent_classifier_does_not_execute_negated_or_question_commands(self):
        dialogue = self.host.pm_dialogue
        self.assertEqual('discussion', dialogue._intent('先讨论，不要开发或实现'))
        self.assertEqual('status', dialogue._intent('为什么暂停了？'))
        self.assertEqual('discussion', dialogue._intent('能否修复这个问题？'))
        self.assertEqual('resume', dialogue._intent('继续推进'))
        self.assertEqual('pause', dialogue._intent('暂停派发'))
        self.assertEqual('work', dialogue._intent('请修复这个问题'))

    async def test_latest_page_and_older_pages_cover_conversation_once(self):
        for index in range(205):
            self.host.pm_dialogue._append(f'message_{index:04d}', 'owner', 'input', f'message {index}')
        latest = self.host.pm_dialogue.list(limit=100)
        older = self.host.pm_dialogue.list(offset=100, limit=100)
        oldest = self.host.pm_dialogue.list(offset=200, limit=100)
        self.assertEqual(100, len(latest['items']))
        self.assertEqual(100, len(older['items']))
        self.assertEqual(5, len(oldest['items']))
        self.assertTrue(latest['has_older'])
        self.assertTrue(older['has_older'])
        self.assertFalse(oldest['has_older'])
        self.assertEqual('message 105', latest['items'][0]['content'])
        self.assertEqual('message 204', latest['items'][-1]['content'])
        all_ids = [m['id'] for page in (oldest, older, latest) for m in page['items']]
        self.assertEqual(205, len(set(all_ids)))
