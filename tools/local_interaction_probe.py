"""Real CLI / scripted loopback provider acceptance. Never calls Zhipu.

This proves CLI integration and tool interaction, not live-model quality.
All files/configuration are isolated; no actual account key is used.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.claude import ClaudeSession
from autocoding.opencode import OpenCodeSession
from autocoding.executables import native_executable
from autocoding.core import digest


class Provider(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,root):
        super().__init__(('127.0.0.1',0),Handler)
        self.root=root;self.requests=[];self.phase='question';self.sent=set();self.results=[];self.resume_history=False


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):self.reply({'data':[{'id':'fixture','object':'model'}]})
    def reply(self,data):
        body=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        try:self.handle_post()
        except (BrokenPipeError,ConnectionResetError):pass
    def handle_post(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        if self.path.endswith('count_tokens'):return self.reply({'input_tokens':100})
        phase=self.server.phase;anthropic='/messages' in self.path
        tools={t.get('name',t.get('function',{}).get('name')) for t in body.get('tools',[])}
        self.server.requests.append({'phase':phase,'path':self.path,'tools':sorted(t for t in tools if t)})
        if phase=='resume':
            history=json.dumps(body.get('messages',[]),ensure_ascii=False)
            self.server.resume_history=all(marker in history for marker in ('SCENARIO question','SCENARIO approve','SCENARIO deny','本地自定义风格'))
        if phase=='cancel':time.sleep(10)
        name=None;data=None
        if phase not in self.server.sent and phase in {'question','approve','deny'}:
            if phase=='question':
                name='AskUserQuestion' if anthropic else 'question'
                options=[{'label':'Alpha','description':'First'},{'label':'Beta','description':'Second'}]
                data={'questions':[{'header':'Features','question':'Choose features','options':options,('multiSelect' if anthropic else 'multiple'):True},
                                   {'header':'Style','question':'Describe style','options':options,('multiSelect' if anthropic else 'multiple'):False}]}
            else:
                name='Write' if anthropic else 'write'
                data={('file_path' if anthropic else 'filePath'):str(self.server.root/(phase+'.txt')),'content':'FIXTURE\n'}
            if name not in tools:raise RuntimeError('Expected native tool unavailable: '+name)
            self.server.sent.add(phase)
        else:
            # Only fixture tool results are retained, never configuration/system prompts.
            for message in body.get('messages',[]):
                if message.get('role')=='tool':self.server.results.append(str(message.get('content','')))
                for item in message.get('content',[]) if isinstance(message.get('content'),list) else []:
                    if item.get('type')=='tool_result':self.server.results.append(str(item.get('content','')))
        if not body.get('stream'):
            if anthropic:
                content=[{'type':'tool_use','id':'tool_'+phase,'name':name,'input':data}] if name else [{'type':'text','text':'DONE'}]
                return self.reply({'id':'msg_fixture','type':'message','role':'assistant','model':'fixture','content':content,'stop_reason':'tool_use' if name else 'end_turn','usage':{'input_tokens':100,'output_tokens':10}})
            message={'role':'assistant','content':None,'tool_calls':[{'id':'tool_'+phase,'type':'function','function':{'name':name,'arguments':json.dumps(data)}}]} if name else {'role':'assistant','content':'DONE'}
            return self.reply({'id':'chat_fixture','object':'chat.completion','created':int(time.time()),'model':'fixture','choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if name else 'stop'}],'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}})
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Connection','close');self.end_headers()
        def event(kind,payload):
            self.wfile.write((('event: '+kind+'\n' if kind else '')+'data: '+json.dumps(payload,ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
        if anthropic:
            event('message_start',{'type':'message_start','message':{'id':'msg_fixture_'+uuid.uuid4().hex,'type':'message','role':'assistant','model':body.get('model','fixture'),'content':[],'stop_reason':None,'usage':{'input_tokens':100,'output_tokens':0}}})
            block={'type':'tool_use','id':'tool_'+phase,'name':name,'input':{}} if name else {'type':'text','text':''}
            event('content_block_start',{'type':'content_block_start','index':0,'content_block':block})
            delta={'type':'input_json_delta','partial_json':json.dumps(data)} if name else {'type':'text_delta','text':'DONE'}
            event('content_block_delta',{'type':'content_block_delta','index':0,'delta':delta})
            event('content_block_stop',{'type':'content_block_stop','index':0})
            event('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use' if name else 'end_turn','stop_sequence':None},'usage':{'output_tokens':10}})
            event('message_stop',{'type':'message_stop'})
        else:
            def chunk(delta,finish=None):event('',{'id':'chat_fixture_'+phase,'object':'chat.completion.chunk','created':int(time.time()),'model':'fixture','choices':[{'index':0,'delta':delta,'finish_reason':finish}]})
            delta={'role':'assistant','tool_calls':[{'index':0,'id':'tool_'+phase,'type':'function','function':{'name':name,'arguments':json.dumps(data)}}]} if name else {'role':'assistant','content':'DONE'}
            chunk(delta);chunk({},'tool_calls' if name else 'stop');self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
        self.close_connection=True


async def main(agent,workspace=None,storage=None):
    root=Path(workspace).resolve() if workspace else Path(tempfile.mkdtemp(prefix='local-interactions-'+agent+'-',dir=ROOT/'.local')).resolve()
    if not root.is_relative_to((ROOT/'.local').resolve()) or not root.name.startswith('local-interactions-'+agent+'-'):
        raise ValueError('Probe workspace must be an isolated local interaction fixture')
    if any((root/(phase+'.txt')).exists() for phase in ('approve','deny')):
        raise ValueError('Probe workspace already has side effects; use a fresh fixture')
    storage=Path(storage).resolve() if storage else root/'opencode'
    if not storage.is_relative_to((ROOT/'.local').resolve()) or not storage.parent.name.startswith('local-interactions-opencode-'):
        if agent=='opencode':raise ValueError('Only isolated OpenCode dependency profiles may be reused')
    provider=Provider(root);thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(provider.server_port)
    env={k:v for k,v in os.environ.items() if not k.startswith(('ANTHROPIC_','CLAUDE','OPENAI_','ZHIPU','OPENCODE_')) and k.upper() not in {'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'}}
    env.update(ANTHROPIC_API_KEY='local-fixture-only',ANTHROPIC_BASE_URL=base,CLAUDE_CONFIG_DIR=str(root/'claude'),CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    env['OPENCODE_CONFIG_CONTENT']=json.dumps({'enabled_providers':['zhipuai-coding-plan'],'provider':{'zhipuai-coding-plan':{'npm':'@ai-sdk/openai-compatible','options':{'baseURL':base+'/v1','apiKey':'local-fixture-only'},'models':{'fixture':{'name':'Fixture','limit':{'context':32000,'output':2048}}}}}})
    env['OPENCODE_DISABLE_MODELS_FETCH']='true'
    env['OPENCODE_DISABLE_DEFAULT_PLUGINS']='true'
    report={'agent':agent,'scope':'real_cli_scripted_loopback_provider','live_supplier':False,'checks':[],'passed':False}
    adapter=None
    async def connect(resume=None):
        executable=native_executable(agent)
        if agent=='claude':return await ClaudeSession.connect(executable,cwd=root,env=env,model='fixture',timeout=60,tools=('Write','AskUserQuestion'),persist=True,resume=resume)
        return await OpenCodeSession.connect(executable,cwd=root,env=dict(env),storage=storage,model='fixture',timeout=120,resume=resume,permission_mode='review')
    try:
        adapter=await connect()
        for phase in ('question','approve','deny'):
            provider.phase=phase
            await adapter.send_text('SCENARIO '+phase,command_id='cmd_'+phase)
            handled=False
            async with asyncio.timeout(90):
                while True:
                    event=await adapter.next_event();method=event.get('method');params=event.get('params',{})
                    if method in {'agent/question','tool/requestApproval'}:
                        operation=params['operation'];rid=event.get('id',params.get('requestId'))
                        if method=='agent/question':await adapter.respond_question(rid,digest(operation),[['Alpha','Beta'],['本地自定义风格']])
                        else:await adapter.respond_approval(rid,digest(operation),allow=phase=='approve')
                        handled=True
                    if method=='turn/completed':
                        expected={'completed','interrupted'} if phase=='deny' else {'completed'}
                        assert params.get('turn',{}).get('status') in expected, 'Native turn did not succeed'
                        break
                    if method in {'turn/failed','error'}:raise RuntimeError('Native turn failed')
            assert handled,phase+' produced no native interaction'
            if phase=='approve':assert (root/'approve.txt').read_text()=='FIXTURE\n'
            if phase=='deny':assert not (root/'deny.txt').exists()
            if phase=='question':
                results=' '.join(provider.results)
                assert all(word in results for word in ('Alpha','Beta','本地自定义风格')), 'Native answer not delivered'
            report['checks'].append(phase)
        previous=adapter.thread_id
        await adapter.close();adapter=await connect(previous)
        assert adapter.thread_id==previous
        provider.phase='resume'
        await adapter.send_text('SCENARIO resume',command_id='cmd_resume')
        async with asyncio.timeout(60):
            while True:
                event=await adapter.next_event()
                if event.get('method')=='turn/completed':
                    assert event['params']['turn']['status']=='completed'
                    break
        assert provider.resume_history, 'Resumed native conversation lost earlier context'
        report['checks'].append('persistent_session_and_context_resume')
        provider.phase='cancel'
        await adapter.send_text('SCENARIO cancel',command_id='cmd_cancel')
        async with asyncio.timeout(30):
            while not any(r['phase']=='cancel' for r in provider.requests):await asyncio.sleep(.1)
        await adapter.interrupt();report['checks'].append('interrupt_acknowledged')
        async with asyncio.timeout(45):
            while (await adapter.next_event()).get('method')!='turn/completed':pass
        assert adapter.turn_id is None, 'Interrupted turn remained active'
        report['checks'].append('interrupt_completed')
        provider.phase='after_cancel'
        await adapter.send_text('SCENARIO after_cancel',command_id='cmd_after_cancel')
        async with asyncio.timeout(60):
            while True:
                event=await adapter.next_event()
                if event.get('method')=='turn/completed':
                    assert event['params']['turn']['status']=='completed'
                    break
        report['checks'].append('new_turn_after_interrupt')
        report['passed']=True
    except Exception as error:
        report['error']=getattr(error,'code',type(error).__name__)
        report['detail']=str(error) if isinstance(error,AssertionError) else None
    finally:
        if adapter is not None:await adapter.close()
        provider.shutdown();provider.server_close()
        report['requests']=provider.requests
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--agent',choices=['claude','opencode'],required=True)
    parser.add_argument('--workspace',type=Path,help='Resume isolated provider setup after a startup-only failure')
    parser.add_argument('--storage',type=Path,help='Reuse dependencies from an isolated OpenCode probe profile')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.agent,args.workspace,args.storage)))
