"""Bounded real-model Codex interaction acceptance in an isolated fixture."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.codex import CodexSession
from autocoding.codex_environment import child_environment
from autocoding.executables import native_executable
from autocoding.transport import JsonLineProcess,JsonRpcClient
from autocoding.core import digest


async def main():
    root=Path(tempfile.mkdtemp(prefix='codex-interactions-',dir=ROOT/'.local'))
    (root/'.codex/rules').mkdir(parents=True)
    (root/'.codex/config.toml').write_text('approvals_reviewer = "user"\n',encoding='utf-8')
    (root/'.codex/rules/fixture.rules').write_text('prefix_rule(pattern=["Set-Content"], decision="prompt", justification="Isolated interaction acceptance")\n',encoding='utf-8')
    subprocess.run(['git','-C',str(root),'init'],check=True,capture_output=True)
    report={'scope':'real_codex_model_isolated_fixture','checks':[],'passed':False,'zhipu_calls':0}
    session=None;model=None;plan=True
    async def connect(resume=None):
        nonlocal model
        transport=await JsonLineProcess.start([str(native_executable('codex')),'app-server','--listen','stdio://'],cwd=root,env=child_environment(os.environ))
        rpc=JsonRpcClient(transport,request_timeout=60)
        value=CodexSession(rpc)
        try:
            await rpc.request('initialize',{'clientInfo':{'name':'autocoding_acceptance','version':'0.1.0'},'capabilities':{'experimentalApi':True}})
            await rpc.notify('initialized');value.initialized=True
            original=rpc.request
            async def request(method,params):
                nonlocal model
                if method=='turn/start':
                    params={**params,'collaborationMode':{'mode':'plan' if plan else 'default','settings':{'model':model,'reasoning_effort':None,'developer_instructions':None}}}
                result=await original(method,params)
                if method in {'thread/start','thread/resume'}:model=result.get('model',model)
                return result
            rpc.request=request
            await value.start_thread(root,sandbox='danger-full-access',resume=resume,approval_policy='on-request')
            return value
        except BaseException:
            await value.close();raise
    async def turn(prompt,phase):
        await session.send_text(prompt,command_id='cmd_'+uuid.uuid4().hex)
        handled=False;text=''
        async with asyncio.timeout(150):
            while True:
                event=await session.next_event();method=event.get('method');params=event.get('params',{})
                if method=='agent/question':
                    assert phase=='question','Unexpected question'
                    questions=params['operation']['questions']
                    answers=[['本地自定义风格'] if i else ['Alpha'] for i in range(len(questions))]
                    assert len(questions)>=2,'Expected option and custom-answer questions'
                    await session.respond_question(event['id'],digest(params['operation']),answers);handled=True
                elif method=='tool/requestApproval':
                    assert phase in {'approve','deny'},'Unexpected approval'
                    await session.respond_approval(event['id'],digest(params['operation']),allow=phase=='approve');handled=True
                elif method=='item/agentMessage/delta':text+=params.get('delta','')
                elif method=='turn/completed':
                    assert params['turn']['status']=='completed','Turn did not complete'
                    break
        if phase in {'question','approve','deny'}:assert handled,'Required native interaction missing: '+phase
        return text
    try:
        session=await connect()
        text=await turn('Use request_user_input once with exactly two questions: first choose Alpha or Beta; second choose Formal or Casual with a custom answer allowed. After receiving answers, repeat both answers verbatim and finish. Do not use files or other tools.','question')
        assert 'Alpha' in text and '本地自定义风格' in text,'Answer not reflected by model'
        report['checks'].append('native_option_and_custom_question');plan=False
        await turn('Execute exactly this PowerShell command: Set-Content approve.txt FIXTURE. Then reply DONE.','approve')
        assert (root/'approve.txt').read_text(encoding='utf-8-sig').strip()=='FIXTURE'
        report['checks'].append('approval_verified_file')
        await turn('Request exactly this PowerShell command: Set-Content deny.txt FORBIDDEN. If approval is denied, do not retry or use any alternative, simply reply DENIED.','deny')
        assert not (root/'deny.txt').exists()
        report['checks'].append('denial_no_side_effect')
        previous=session.thread_id;await session.close();session=await connect(previous)
        text=await turn('Without using tools, repeat the custom style answer I gave to your second question earlier.','resume')
        assert '本地自定义风格' in text,'Resumed context missing'
        report['checks'].append('persistent_context_resume')
        await session.send_text('Think through a comprehensive test plan for this project, but do not use tools or write files.',command_id='cmd_'+uuid.uuid4().hex)
        async with asyncio.timeout(60):
            while True:
                event=await session.next_event()
                if event.get('method')=='turn/started' and event.get('params',{}).get('turn',{}).get('id')==session.turn_id:break
        await session.interrupt()
        async with asyncio.timeout(60):
            while (await session.next_event()).get('method')!='turn/completed':pass
        assert session.turn_id is None
        report['checks'].append('interrupt_completed')
        text=await turn('Do not use tools. Reply exactly AFTER_CANCEL.','after_cancel')
        assert 'AFTER_CANCEL' in text
        report['checks'].append('new_turn_after_interrupt');report['passed']=True
    except Exception as error:
        report['error']=getattr(error,'code',type(error).__name__)
        if isinstance(error,AssertionError):report['detail']=str(error)
    finally:
        if session is not None:await session.close()
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(root/'report.json'),**report},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
