"""Real Codex local transport crash; explicitly NOT a public-network outage."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from resume_probe import ROOT,Host,turn
from autocoding.core import DomainError


async def main():
    output=Path(tempfile.mkdtemp(prefix='codex-disconnect-',dir=ROOT/'.local'))
    project=output/'project';project.mkdir()
    host=Host(project,output/'storage')
    async def disabled(*args,**kwargs):
        raise DomainError('ZHIPU_DEFERRED')
    host.pm_text=disabled
    report={'passed':False,'fault':'local_app_server_process_exit',
        'public_network_outage_verified':False,'zhipu_calls':0}
    started=time.monotonic()
    stage='startup'
    try:
        first=await host.start('codex');sid=first['session_id']
        stage='initial_turn'
        marker='CONTEXT_'+uuid.uuid4().hex
        if await turn(host,sid,'Without tools or files remember '+marker+'. Reply OK.','first')!='completed':
            raise DomainError('FIRST_TURN_FAILED')
        command=await host.send(sid,0,'Without tools or files, list the integers 1 through 1000, one per line.',key='fault-turn')
        async with asyncio.timeout(60):
            while host.core.command(command['command_id'])['state']=='queued':await asyncio.sleep(.02)
            while host.core.command(command['command_id'])['state']=='dispatching':await asyncio.sleep(.02)
        if host.core.command(command['command_id'])['state']!='acknowledged':
            raise DomainError('FAULT_WINDOW_MISSED')
        stage='kill_process_tree'
        process=host.runtimes[sid].adapter.rpc.transport.process
        killed=await asyncio.to_thread(subprocess.run,
            ['taskkill.exe','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
        report['process_tree_kill_exit_code']=killed.returncode
        stage='await_safe_resume'
        async with asyncio.timeout(90):
            while not any(e['type']=='resumed' and e.get('previous_session_id')==sid for e in host.output):
                await asyncio.sleep(.1)
        resumed=next(e['session_id'] for e in host.output if e['type']=='resumed' and e.get('previous_session_id')==sid)
        report['unknown_preserved']=host.core.command(command['command_id'])['state']=='delivery_unknown'
        report['no_replay']=not any(c['session_id']==resumed for c in host.core.snapshot()['commands'])
        report['same_provider_session']=host.metadata[resumed]['provider_session_id']==first['provider_session_id']
        # A new explicit read-only question; never resend the interrupted input.
        report['resume_state']=await turn(host,resumed,'Without tools or files reply only with the CONTEXT_ marker I gave you.','verify-resume')
        text=''.join(e.get('text','') for e in host.output if e['session_id']==resumed)
        report['context_retained']=marker in text
        report['passed']=all(report[k] for k in ['unknown_preserved','no_replay','same_provider_session','context_retained']) and report['resume_state']=='completed'
    except Exception as exc:
        report['error']=getattr(exc,'code',type(exc).__name__)
        report['failure_stage']=stage
        report['command_states']=[item['state'] for item in host.core.snapshot()['commands']]
        report['session_states']=[item['state'] for item in host.core.snapshot()['sessions']]
        report['restart_counts']=sorted({int(value.get('restart_count',0)) for value in host.metadata.values()})
        report['recent_output_shapes']=[{'type':item.get('type'),'code':item.get('code')}
            for item in host.output[-12:]]
    finally:
        await host.close()
        report['seconds']=round(time.monotonic()-started,2)
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'report':str(output/'report.json')}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
