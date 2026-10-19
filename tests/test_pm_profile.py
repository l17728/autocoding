import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError
from autocoding.redaction import Redactor


class PMProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.host=Host(self.temp.name,Path(self.temp.name)/'state')
    async def asyncTearDown(self):
        await self.host.close();self.temp.cleanup()

    async def test_owner_preferences_are_versioned_redacted_and_persistent(self):
        self.host.redactor=Redactor(['PRIVATE_CANARY'])
        value=self.host.pm_profile.snapshot()['preferences']
        value['reporting_style']='先结果后证据 PRIVATE_CANARY'
        saved=self.host.pm_profile.save(0,value,key='owner')
        self.assertNotIn('PRIVATE_CANARY',json.dumps(saved))
        self.assertEqual(saved,self.host.pm_profile.save(0,value,key='owner'))
        with self.assertRaisesRegex(DomainError,'STALE_PM_PROFILE'):
            self.host.pm_profile.save(0,value,key='stale')
        other=Host(self.temp.name,self.host.storage)
        try:self.assertEqual(saved,other.pm_profile.snapshot())
        finally:await other.close()

    async def test_original_goal_and_owner_correction_survive_checkpoints_and_resume(self):
        self.host.core.create_session('s','codex',owner='human',key='s')
        def remember(text,key):
            command=self.host.core.enqueue('s','human',0,text,key=key)
            self.host.remember_context('s',command['command_id'],text)
        remember('实现中文界面；保留旧数据','initial')
        remember('增加键盘可访问性','correction')
        remember('颜色使用深色','correction-2')
        self.host.core.create_session('p','codex',owner='pm',key='p')
        self.host.metadata['p']={'resumed_from':'s'}
        command=self.host.core.enqueue('p','pm',0,'盘点剩余工作',key='check')
        self.host.remember_context('p',command['command_id'],'盘点剩余工作')
        other=Host(self.temp.name,self.host.storage)
        try:
            context=json.loads(other.task_context('p'))
            self.assertEqual('实现中文界面；保留旧数据',context['initial'])
            self.assertEqual('颜色使用深色',context['latest_owner_instruction'])
            self.assertIn('增加键盘可访问性',[item['text'] for item in context['owner_instructions']])
            self.assertEqual('盘点剩余工作',context['latest_instruction'])
        finally:await other.close()

    async def test_context_bounds_short_corrections_and_marks_missing_history(self):
        self.host.core.create_session('s','codex',owner='human',key='s')
        for index in range(20):
            text=f'修正 {index}'
            command=self.host.core.enqueue('s','human',0,text,key=str(index))
            self.host.remember_context('s',command['command_id'],text)
        context=json.loads(self.host.task_context('s'))
        self.assertEqual(16,len(context['owner_instructions']))
        self.assertEqual('修正 0',context['initial'])
        self.assertEqual('修正 19',context['latest_owner_instruction'])
        self.assertTrue(context['context_truncated'])

    async def test_pm_calls_include_explicit_style_even_without_learning(self):
        prefs=self.host.pm_profile.snapshot()['preferences'];prefs['reporting_style']='先验收结果，再列未完成'
        self.host.pm_profile.save(0,prefs,key='style')
        adapter=SimpleNamespace(send_text=AsyncMock(),close=AsyncMock(),next_event=AsyncMock(side_effect=[
            {'method':'item/agentMessage/delta','params':{'delta':'OK'}},
            {'method':'turn/completed','params':{'turn':{'status':'completed'}}}]))
        with patch('autocoding.host.native_executable',return_value='fixture.exe'), \
             patch('autocoding.host.resolve_zhipu',return_value=SimpleNamespace(api_key='PRIVATE',model='fixture')), \
             patch('autocoding.host.claude_environment',return_value={}), \
             patch('autocoding.host.ClaudeSession.connect',new=AsyncMock(return_value=adapter)):
            self.assertEqual('OK',await self.host.pm_text('复盘自己的表现',include_learning=False))
        prompt=adapter.send_text.call_args.args[0]
        self.assertIn('先验收结果，再列未完成',prompt)
        self.assertIn('复盘自己的表现',prompt)
        self.assertIn('不授权未知输入重放',prompt)
        adapter.close.assert_awaited_once()

    async def test_self_check_exposes_unfinished_work_without_claiming_improvement(self):
        self.host.core.create_task('task_gap','待完成的目标',['unit'],key='gap')
        check=self.host.pm_profile.self_check()
        self.assertEqual(1,check['task_states']['todo'])
        self.assertEqual(0,check['pending_owner'])
        self.assertIn('不证明',check['notice'])
