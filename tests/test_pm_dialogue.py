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

    async def test_search_filters_full_history_before_pagination(self):
        for index in range(125):
            self.host.pm_dialogue._append(f'message_{index:04d}', 'owner', 'input',
                                          'needle task_42' if index in (3, 123) else f'ordinary {index}')
        page = self.host.pm_dialogue.list(limit=1, query='NEEDLE')
        self.assertEqual(2, page['total'])
        self.assertTrue(page['has_older'])
        self.assertEqual('needle task_42', page['items'][0]['content'])
        older = self.host.pm_dialogue.list(offset=1, limit=1, query='needle')
        self.assertEqual('needle task_42', older['items'][0]['content'])
        self.assertFalse(older['has_older'])
        by_reference = self.host.pm_dialogue.list(query='task_42')
        self.assertEqual(2, by_reference['total'])

    async def test_search_rejects_oversized_query(self):
        with self.assertRaises(DomainError):
            self.host.pm_dialogue.list(query='x' * 201)

    async def test_discussion_uses_pm_context_without_claiming_action(self):
        from unittest.mock import AsyncMock
        self.host.pm_text = AsyncMock(return_value='建议先核对音频格式兼容性，再决定实现范围。')
        result = await self.host.pm_dialogue.submit('message_0101', '讨论一下导入格式的兼容性风险')
        self.assertEqual('discussion_recorded', result['action'])
        self.assertIn('建议先核对', result['reply']['content'])
        self.assertIn('尚未修改计划', result['reply']['content'])
        self.host.pm_text.assert_awaited_once()

    async def test_discussion_model_failure_is_visible_and_keeps_input(self):
        from unittest.mock import AsyncMock
        self.host.pm_text = AsyncMock(side_effect=DomainError('PM_MODEL_FAILED'))
        result = await self.host.pm_dialogue.submit('message_0102', '讨论一下风险')
        self.assertIn('PM_MODEL_FAILED', result['reply']['content'])
        self.assertEqual('discussion', self.host.pm_dialogue.list(limit=2)['items'][-1]['kind'])

    async def test_material_status_change_adds_one_proactive_report(self):
        self.host.pm_dialogue.status()  # Establish baseline without a noisy startup report.
        before = len(self.host.pm_dialogue.list()['items'])
        self.host.core.create_task('task_new', 'A newly added task', ['unit'], key='task_new')
        self.host.pm_dialogue.status()
        self.host.pm_dialogue.status()
        reports = [m for m in self.host.pm_dialogue.list()['items'][before:] if m['kind'] == 'report']
        self.assertEqual(1, len(reports))
        self.assertIn('进展更新', reports[0]['content'])

    async def test_background_reporter_runs_without_api_polling(self):
        from unittest.mock import patch
        self.host.pm_dialogue.status()
        with patch('autocoding.pm_dialogue.STATUS_REPORT_INTERVAL', .01):
            reporter = asyncio.create_task(self.host.pm_dialogue.run_reporter())
            try:
                await asyncio.sleep(.5)  # Reporter has established its durable event cursor.
                self.host.core.create_task('task_background', 'Background monitored task', ['unit'], key='task_bg')
                async with asyncio.timeout(3):
                    while not any(m['kind'] == 'report' for m in self.host.pm_dialogue.list()['items']):
                        await asyncio.sleep(.01)
                await self.host.pm_dialogue.submit('message_0199', '暂停派发')
                async with asyncio.timeout(3):
                    while not any(m['kind'] == 'report' and '已暂停新任务派发' in m['content']
                                  for m in self.host.pm_dialogue.list()['items']):
                        await asyncio.sleep(.01)
            finally:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
