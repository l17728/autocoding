import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.host import Host
from autocoding.core import DomainError


class TerminalHubTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_launch_also_isolates_parent_lifecycle_without_mutating_credentials(self):
        from autocoding.codex import CodexSession
        env={'CODEX_DAEMON_SHUTDOWN_FILE':'parent','CODEX_THREAD_ID':'parent','CODEX_TOOL_CALL_ID':'parent',
             'OPENAI_API_KEY':'fixture-only-value','PATH':'fixture-path'}
        rpc=SimpleNamespace(request=AsyncMock(return_value={}),notify=AsyncMock(),close=AsyncMock())
        with patch('autocoding.codex.JsonLineProcess.start',new=AsyncMock()) as spawn, \
             patch('autocoding.codex.JsonRpcClient',return_value=rpc):
            session=await CodexSession.connect('fixture.exe',cwd='.',env=env)
        child=spawn.call_args.kwargs['env']
        self.assertEqual({'OPENAI_API_KEY':'fixture-only-value','PATH':'fixture-path'},child)
        self.assertEqual('parent',env['CODEX_DAEMON_SHUTDOWN_FILE'])
        self.assertTrue(session.initialized)

    async def test_resume_is_scoped_persistent_and_never_replays_input(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Host(folder,Path(folder)/'storage')
            async def spawn(*args,**kwargs):
                session=SimpleNamespace(_closed=False,_failure=None)
                async def read(**kwargs):await asyncio.Future()
                async def close():session._closed=True
                session.read=read;session.close=close
                return session
            with patch('autocoding.terminal_hub.native_executable',return_value='fixture.exe'), \
                 patch.dict('os.environ',{'FORCE_COLOR':'1','NO_COLOR':'1','CODEX_THREAD_ID':'parent',
                    'CODEX_SESSION_ID':'parent','CODEX_DAEMON_SHUTDOWN_SOCKET':'parent','CODEX_CI':'1',
                    'CODEX_DAEMON_SHUTDOWN_FILE':'parent','CODEX_TOOL_CALL_ID':'parent'}), \
                 patch('autocoding.terminal_hub.git',return_value='a'*40), \
                 patch.object(host.workspaces,'create',return_value={'id':'workspace_fixture','path':folder}), \
                 patch.object(host.workspaces,'get',return_value={'id':'workspace_fixture','path':folder}), \
                 patch('autocoding.terminal_hub.resolve_zhipu',return_value=SimpleNamespace(api_key='PRIVATE_CANARY',model='fixture')), \
                 patch('autocoding.terminal_hub.claude_environment',return_value={}), \
                 patch('autocoding.terminal_hub.TerminalSession.start',new=AsyncMock(side_effect=spawn)) as start:
                try:
                    for agent in ['codex','claude','opencode']:
                        original=await host.terminals.start(agent)
                        with self.assertRaisesRegex(DomainError,'TERMINAL_RESUME_UNAVAILABLE'):
                            await host.terminals.start(agent,resume_from=original['id'])
                        await host.terminals.stop(original['id'])
                        resumed=await host.terminals.start(agent,resume_from=original['id'])
                        argv=start.call_args.args[2]
                        self.assertNotIn('NO_COLOR',start.call_args.kwargs['env'])
                        self.assertNotIn('FORCE_COLOR',start.call_args.kwargs['env'])
                        self.assertEqual('xterm-256color',start.call_args.kwargs['env']['TERM'])
                        self.assertEqual(original['cwd'],resumed['cwd'])
                        self.assertEqual(original['state_directory'],resumed['state_directory'])
                        self.assertEqual(['resume','--last'],argv[1:3]) if agent=='codex' else self.assertIn('--continue',argv)
                        if agent=='codex':
                            for name in ('CODEX_THREAD_ID','CODEX_SESSION_ID','CODEX_TOOL_CALL_ID',
                                         'CODEX_DAEMON_SHUTDOWN_SOCKET','CODEX_DAEMON_SHUTDOWN_FILE','CODEX_CI'):
                                self.assertNotIn(name,start.call_args.kwargs['env'])
                            self.assertEqual('never',argv[argv.index('--ask-for-approval')+1])
                            self.assertEqual('danger-full-access',argv[argv.index('--sandbox')+1])
                            self.assertIn('check_for_update_on_startup=false',argv)
                        elif agent=='claude':
                            self.assertIn('--dangerously-skip-permissions',argv)
                        else:
                            child_config=json.loads(start.call_args.kwargs['env']['OPENCODE_CONFIG_CONTENT'])
                            self.assertEqual('allow',child_config['permission']['*'])
                            self.assertEqual('@ai-sdk/openai-compatible',child_config['provider']['zhipuai-coding-plan']['npm'])
                            self.assertEqual('https://open.bigmodel.cn/api/coding/paas/v4',
                                             child_config['provider']['zhipuai-coding-plan']['options']['baseURL'])
                            child=start.call_args.kwargs['env']
                            self.assertEqual('true',child['OPENCODE_DISABLE_MODELS_FETCH'])
                            self.assertEqual(str(Path(original['state_directory'])/'profile'),child['OPENCODE_TEST_HOME'])
                            self.assertEqual('true',child['OPENCODE_DISABLE_PROJECT_CONFIG'])
                            self.assertNotIn('OPENCODE_PURE',child)
                        self.assertNotIn('PRIVATE_CANARY',str(argv)+json.dumps(resumed))
                        with self.assertRaisesRegex(DomainError,'TERMINAL_RESUME_UNAVAILABLE'):
                            await host.terminals.start(agent,resume_from=original['id'])
                        await host.terminals.stop(resumed['id'])
                    self.assertEqual([],host.core.snapshot()['commands'])
                    other=Host(folder,Path(folder)/'storage')
                    other.recover()
                    self.assertEqual(6,len(other.terminals.snapshot()))
                    self.assertEqual(3,sum(s['can_resume'] for s in other.terminals.snapshot()))
                    self.assertTrue(all(not s['live'] for s in other.terminals.snapshot()))
                    await other.close()
                finally:
                    await host.close()
