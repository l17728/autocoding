import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autocoding.claude import ClaudeSession
from autocoding.codex import CodexSession
from autocoding.core import digest


class AgentPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_mcp_approval_is_confirmed_by_matching_tool_completion(self):
        params={'threadId':'thread_fixture','turnId':'turn_fixture','serverName':'autocoding','mode':'form',
                'requestedSchema':{'type':'object','properties':{}},
                '_meta':{'codex_approval_kind':'mcp_tool_call','tool_name':'get_instructions'}}
        rpc=type('RPC',(),{'next_event':AsyncMock(return_value={'id':17,'method':'mcpServer/elicitation/request','params':params}),
                          'respond':AsyncMock()})()
        adapter=CodexSession(rpc)
        adapter.thread_id='thread_fixture'
        event=await adapter.next_event()
        self.assertEqual('tool/requestApproval',event['method'])
        receipt=await adapter.respond_approval('17',digest(event['params']['operation']),allow=True)
        self.assertEqual('sent_unconfirmed',receipt['state'])
        rpc.respond.assert_awaited_once_with(17,{'action':'accept','content':{}})
        rpc.next_event.return_value={'method':'item/completed','params':{'threadId':'thread_fixture','turnId':'turn_fixture',
            'item':{'id':'call_1','type':'mcpToolCall','server':'autocoding','tool':'get_instructions','status':'completed'}}}
        completed=await adapter.next_event()
        self.assertEqual(receipt['tool_use_id'],completed['params']['toolUseId'])
        # Installed CLI omits tool_name in approval metadata. Correlate the
        # preceding item/started instead of guessing from the prompt text.
        params['_meta'].pop('tool_name')
        rpc.next_event.return_value={'method':'item/started','params':{'threadId':'thread_fixture','turnId':'turn_fixture',
            'item':{'id':'call_2','type':'mcpToolCall','server':'autocoding','tool':'get_instructions'}}}
        await adapter.next_event()
        rpc.next_event.return_value={'id':18,'method':'mcpServer/elicitation/request','params':params}
        event=await adapter.next_event()
        self.assertEqual('get_instructions',event['params']['operation']['mcp_tool'])
        receipt=await adapter.respond_approval('18',digest(event['params']['operation']),allow=True)
        rpc.next_event.return_value={'method':'item/completed','params':{'threadId':'thread_fixture','turnId':'turn_fixture',
            'item':{'id':'call_2','type':'mcpToolCall','server':'autocoding','tool':'get_instructions','status':'completed'}}}
        self.assertEqual(receipt['tool_use_id'],(await adapter.next_event())['params']['toolUseId'])

    async def test_codex_mcp_forms_and_other_servers_are_not_permission_shortcuts(self):
        for extra in [{'serverName':'other'},{'requestedSchema':{'type':'object','properties':{'secret':{'type':'string'}}}},
                      {'threadId':'other'}, {'_meta':{'codex_approval_kind':'browser_auth'}}]:
            params={'threadId':'t','serverName':'autocoding','mode':'form','requestedSchema':{'type':'object'},
                    '_meta':{'codex_approval_kind':'mcp_tool_call','tool_name':'lookup'},**extra}
            rpc=type('RPC',(),{'next_event':AsyncMock(return_value={'id':1,'method':'mcpServer/elicitation/request','params':params})})()
            adapter=CodexSession(rpc)
            adapter.thread_id='t'
            self.assertEqual('mcpServer/elicitation/request',(await adapter.next_event())['method'])

    async def test_claude_skip_permissions_persistence_and_exact_resume(self):
        class Transport:
            async def receive(self):
                await asyncio.Future()
            async def close(self):
                pass
            def poison(self,code):
                pass
        with patch('autocoding.claude.JsonLineProcess.start',new=AsyncMock(return_value=Transport())) as start:
            with patch.object(ClaudeSession,'control',new=AsyncMock(return_value={})):
                sid='11111111-1111-4111-8111-111111111111'
                adapter=await ClaudeSession.connect('claude.exe',cwd='.',env={},model='fixture',
                    tools=('AskUserQuestion',),skip_permissions=True,persist=True,resume=sid)
                argv=start.call_args.args[0]
                self.assertIn('--dangerously-skip-permissions',argv)
                self.assertIn('--resume='+sid,argv)
                self.assertNotIn('--safe-mode',argv)
                self.assertNotIn('--no-session-persistence',argv)
                self.assertNotIn('--permission-mode',argv)
                await adapter.close()
                adapter=await ClaudeSession.connect('claude.exe',cwd='.',env={},model='fixture',
                    tools=('Read',),skip_permissions=False,mcp_config='managed.json')
                argv=start.call_args.args[0]
                self.assertNotIn('--dangerously-skip-permissions',argv)
                self.assertNotIn('--safe-mode',argv)
                self.assertEqual('manual',argv[argv.index('--permission-mode')+1])
                self.assertEqual('managed.json',argv[argv.index('--mcp-config')+1])
                self.assertIn('--strict-mcp-config',argv)
                await adapter.close()

    async def test_codex_full_access_resume_uses_exact_thread(self):
        rpc=type('RPC',(),{'request':AsyncMock(return_value={'thread':{'id':'thread_fixture'}})})()
        adapter=CodexSession(rpc)
        await adapter.start_thread('.',sandbox='danger-full-access',resume='thread_fixture')
        method,params=rpc.request.call_args.args
        self.assertEqual('thread/resume',method)
        self.assertEqual('danger-full-access',params['sandbox'])
        self.assertEqual('thread_fixture',params['threadId'])

    async def test_codex_question_choice_and_free_text_wire_contract(self):
        operation={'threadId':'thread_fixture','turnId':'t','questions':[{'id':'format','question':'Format?',
                    'options':[{'label':'JSON','description':'structured'}]},{'id':'prefix','question':'Prefix?','isOther':True}]}
        rpc=type('RPC',(),{'next_event':AsyncMock(return_value={'id':123,'method':'item/tool/requestUserInput','params':operation}),
                          'respond':AsyncMock()})()
        adapter=CodexSession(rpc)
        adapter.thread_id='thread_fixture'
        event=await adapter.next_event()
        self.assertEqual('agent/question',event['method'])
        await adapter.respond_question(123,digest(operation),[['JSON'],['team_']])
        rpc.respond.assert_awaited_once_with(123,{'answers':{'format':{'answers':['JSON']},'prefix':{'answers':['team_']}}})

    async def test_codex_approval_preserves_integer_wire_request_id(self):
        params={'threadId':'thread_fixture','itemId':'tool_1','command':'echo fixture'}
        rpc=type('RPC',(),{'next_event':AsyncMock(return_value={'id':12,'method':'item/commandExecution/requestApproval','params':params}),
                          'respond':AsyncMock()})()
        adapter=CodexSession(rpc)
        adapter.thread_id='thread_fixture'
        event=await adapter.next_event()
        self.assertEqual('12',event['id'])
        await adapter.respond_approval('12',digest(event['params']['operation']),allow=True)
        rpc.respond.assert_awaited_once_with(12,{'decision':'accept'})
