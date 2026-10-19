"""Run three real providers on independent files and verify their integration."""
import asyncio
import argparse
import json
from pathlib import Path
import sys
import tempfile
import secrets
import socket

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.workspaces import git
from autocoding.opencode import OpenCodeSession


async def main(browser=False):
    output=Path(tempfile.mkdtemp(prefix='concurrency-probe-',dir=ROOT/'.local'))
    repo=output/'project'
    repo.mkdir()
    agents=['codex','opencode','claude']
    checks={}
    for agent in agents:
        (repo/(agent+'.py')).write_text('value = 0\n',encoding='utf-8')
        test='test_'+agent+'.py'
        (repo/test).write_text('import '+agent+'\nassert '+agent+'.value == 42\n',encoding='utf-8')
        checks[agent]=[sys.executable,test]
    git(repo,'init')
    git(repo,'config','user.name','Fixture')
    git(repo,'config','user.email','fixture@example.invalid')
    git(repo,'add','.')
    git(repo,'commit','-m','baseline')
    baseline=git(repo,'rev-parse','HEAD')
    host=Host(repo,repo/'.local/workbench',config=ROOT/'.local/credentials/opencode.json')
    # Hold the tiny fixture turns until all three native sessions are ready,
    # so a fast provider cannot finish before the slowest provider boots.
    original_send=host.send
    ready=set()
    gate=asyncio.Event()
    async def send_together(sid,*args,**kwargs):
        ready.add(sid)
        if len(ready)>=3:
            gate.set()
        async with asyncio.timeout(110):
            await gate.wait()
        return await original_send(sid,*args,**kwargs)
    host.send=send_together
    (repo/'.local/acceptance.json').write_text(json.dumps({'checks':checks,'protected_paths':['test_*.py']}),encoding='utf-8')
    for agent in agents:
        host.core.create_task('task_'+agent,'Change only '+agent+'.py to exactly: value = 42\n'
            'The file already exists directly in the registered worktree root from get_instructions. '
            'Read that exact file directly; no file discovery is needed. '
            'Use only file read/write/edit tools. Do not use glob, grep or search tools. '
            'Do not run shell commands, tests, git status or diff; '
            'the platform performs independent verification. Immediately give a final reply after the edit.',[agent],key=agent)
    report={'key_printed':False,'passed':False,'peak_running':0,'synchronized_fixture_start':True}
    # Record only HTTP method/status and a fixed route category, never bodies,
    # headers, query strings or model/Owner credentials.
    http_failures=[]
    original_request=OpenCodeSession._request
    async def observe_response(response):
        if response.status_code not in {200,204}:
            path=response.request.url.path
            category=next((name for name in ['permission','question','session','global']
                           if path.startswith('/'+name)), 'other')
            http_failures.append({'method':response.request.method,'status':response.status_code,
                                  'route':category,'reply':path.endswith('/reply')})
            await response.aread()
            # Keep redacted diagnostics in a local-only artifact, not stdout.
            (output/'http-error-redacted.txt').write_text(host.redactor.text(response.text),encoding='utf-8')
    async def observed_request(session,*args,**kwargs):
        hooks=session.client.event_hooks['response']
        if observe_response not in hooks:
            hooks.append(observe_response)
        return await original_request(session,*args,**kwargs)
    OpenCodeSession._request=observed_request
    server=None
    server_task=None
    browser_process=None
    try:
        if browser:
            import uvicorn
            from autocoding.api import create_app
            token=secrets.token_urlsafe(32)
            token_file=output/'owner-token.txt'
            token_file.write_text(token,encoding='utf-8')
            listener=socket.socket()
            listener.bind(('127.0.0.1',0))
            origin='http://127.0.0.1:'+str(listener.getsockname()[1])
            server=uvicorn.Server(uvicorn.Config(create_app(host,token,origin=origin,ui=ROOT/'ui/dist'),
                access_log=False,log_level='error'))
            server_task=asyncio.create_task(server.serve(sockets=[listener]))
            async with asyncio.timeout(20):
                while not server.started:
                    if server_task.done():
                        await server_task
                        raise RuntimeError('SERVER_NOT_STARTED')
                    await asyncio.sleep(.1)
            browser_process=await asyncio.create_subprocess_exec('node',str(ROOT/'ui/tools/concurrency-browser.mjs'),
                origin,str(token_file),str(output),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(60):
                while not host.coordinator.list():
                    if browser_process.returncode is not None:
                        raise RuntimeError('BROWSER_START_FAILED')
                    await asyncio.sleep(.1)
            batch=host.coordinator.list()[-1]
        else:
            batch=await host.coordinator.start(['task_'+a for a in agents],agents=agents,max_concurrency=3)
        async with asyncio.timeout(780):
            while host.coordinator.current:
                runs=host.workflow.list()
                report['peak_running']=max(report['peak_running'],sum(r['state']=='running' for r in runs))
                await asyncio.sleep(.1)
        final=next(b for b in host.coordinator.list() if b['id']==batch['id'])
        report.update(state=final['state'],reason=final.get('reason'),candidate_sha=final.get('candidate_sha'),
            providers=sorted({r['provider'] for r in host.workflow.list()}),
            source_unchanged=git(repo,'rev-parse','HEAD')==baseline,
            runs=[{k:r.get(k) for k in ['task_id','provider','state','reason','error_type']} for r in host.workflow.list()])
        report['errors']=[{'agent':host.metadata.get(e['session_id'],{}).get('agent'),'code':e.get('code')}
                          for e in host.output if e['type']=='error']
        report['passed']=final['state']=='verified_candidate' and report['peak_running']==3 and report['source_unchanged']
        if browser:
            async with asyncio.timeout(40):
                await browser_process.wait()
            report['browser']=json.loads((output/'browser.json').read_text(encoding='utf-8'))
            report['passed']=report['passed'] and browser_process.returncode==0 and report['browser']['passed']
    except Exception as exc:
        report['passed']=False
        report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        if browser_process and browser_process.returncode is None:
            browser_process.terminate()
            await browser_process.wait()
        if server:
            server.should_exit=True
            await server_task
        else:
            await host.close()
        if browser and (output/'browser.json').exists():
            report['browser']=json.loads((output/'browser.json').read_text(encoding='utf-8'))
        report['http_failures']=http_failures
        OpenCodeSession._request=original_request
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({**report,'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser',action='store_true',help='Start the batch in Edge and capture real UI evidence')
    raise SystemExit(asyncio.run(main(parser.parse_args().browser)))
