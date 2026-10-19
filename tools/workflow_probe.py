"""Exercise the selected real Code Agent in a new managed Git fixture.

Uses the application's authorized permission profile in an isolated fixture.
Any exceptional permission request is restricted to the fixture operation.
"""
import asyncio
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.workspaces import git

CODE='def add(a, b):\n    return a + b\n'


async def main(agent='claude',timeout=600,glob_probe=False,output=None):
    output=Path(output) if output is not None else Path(tempfile.mkdtemp(prefix='workflow-probe-',dir=ROOT/'.local'))
    output.mkdir(parents=True,exist_ok=True)
    repo=output/'project'
    repo.mkdir()
    git(repo,'init')
    git(repo,'config','user.name','Fixture')
    git(repo,'config','user.email','fixture@example.invalid')
    (repo/'calculator.py').write_text('def add(a, b):\n    raise NotImplementedError\n',encoding='utf-8')
    (repo/'test_calculator.py').write_text('from calculator import add\nassert add(2,3)==5\nassert add(-3,2)==-1\n',encoding='utf-8')
    git(repo,'add','.')
    git(repo,'commit','-m','fixture baseline')
    baseline=git(repo,'rev-parse','HEAD')
    host=Host(repo,repo/'.local/workbench',config=ROOT/'.local/credentials/opencode.json')
    if glob_probe:
        with host.core.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('permission_mode',json.dumps('review')))
    (repo/'.local/acceptance.json').write_text(json.dumps({'checks':{'unit':[sys.executable,'test_calculator.py']},
                                                       'protected_paths':['test_calculator.py']}),encoding='utf-8')
    discovery = ('First call the glob tool with exactly {"pattern":"calculator.py"}; omit the optional path argument. '
                 'This is an authorized compatibility check. Then read the matched file. ' if glob_probe else '')
    tool_policy=('You may use local shell commands to read calculator.py and edit it, or use apply_patch. '
                 'Do not run git or modify tests; the platform runs independent checks. ' if agent=='codex' else
                 'Use only read and write/edit tools. Do not run shell commands, tests, git status or diff: the platform runs independent checks. ')
    host.core.create_task('task_add',discovery+'Read calculator.py and replace it with exactly:\n'+CODE+
                         'Do not modify other files. '+tool_policy+
                         'After writing the file, immediately give a brief final reply.',
                         ['unit'],key='task')
    report={'agent':agent,'key_printed':False,'passed':False,'approvals':0}
    try:
        run=await host.workflow.start('task_add',max_seconds=timeout,agent=agent)
        work=host.workflow.active['task_add']
        async with asyncio.timeout(timeout+60):
            while not work.done():
                for approval in host.approvals.list():
                    if agent != 'claude':
                        continue  # Exercise the application's PM permission reviewer.
                    if approval['state']!='pending':
                        continue
                    operation=host.operations.get(approval['id'],{})
                    sid=approval['session_id']
                    workspace=Path(host.metadata[sid]['cwd'])
                    raw=operation.get('input',{}).get('file_path','')
                    target=Path(raw)
                    target=(target if target.is_absolute() else workspace/target).resolve()
                    allowed=(operation.get('tool_name')=='Read' and target in {workspace/'calculator.py',workspace/'test_calculator.py'}) or (
                        operation.get('tool_name')=='Write' and target==workspace/'calculator.py' and
                        operation.get('input',{}).get('content','').replace('\r\n','\n')==CODE)
                    await host.decide(approval['id'],'approved' if allowed else 'denied',approval['operation_hash'],
                                      approval['revision'],key='probe:'+approval['id'])
                    report['approvals']+=1
                await asyncio.sleep(.1)
            await work
        final=next(r for r in host.workflow.list() if r['id']==run['id'])
        report.update(run=final,passed=final['state']=='done',source_unchanged=git(repo,'rev-parse','HEAD')==baseline)
        report['mcp_events']=[e for e in host.core.events() if e.get('type')=='MCPToolAuthorized']
        report['mcp_verified']=bool(report['mcp_events'])
        report['passed']=report['passed'] and report['mcp_verified'] and report['source_unchanged']
        report['pm_approvals']=sum(a.get('decided_by')=='pm' and a['state']=='approved' for a in host.approvals.list())
        report['review_events']=[e for e in host.output if e['type']=='approval_owner_required']
        report['errors']=[e for e in host.output if e['type']=='error']
        report['unmapped_request_shapes']=[host.redactor.value(r.adapter.unmapped_request_shape)
            for r in host.runtimes.values() if hasattr(r.adapter,'unmapped_request_shape')]
        report['permission_kinds']=[{'permission':o.get('permission'), 'tool_name':o.get('tool_name'),
                                     'method':o.get('method')} for o in host.operations.values()]
        if glob_probe and final.get('session_id'):
            sid=final['session_id']
            private=Path(host.metadata[sid]['state_directory'])
            marker=private/'glob-path-defaulted.json'
            report['glob_defaulted']=marker.exists() and json.loads(marker.read_text())['defaulted'] is True
            approvals=[a for a in host.approvals.list() if a['session_id']==sid and a['state']=='approved'
                       and a.get('decided_by')=='pm' and host.operations.get(a['id'],{}).get('permission')=='glob']
            report['glob_pm_approved']=bool(approvals)
            report['glob_path_matches_workspace']=bool(approvals) and all(
                Path(host.operations[a['id']].get('metadata',{}).get('path','')).resolve()==Path(host.metadata[sid]['cwd']).resolve()
                for a in approvals)
            report['passed']=report['passed'] and all(report[k] for k in
                ['glob_defaulted','glob_pm_approved','glob_path_matches_workspace'])
        elif glob_probe:
            report.update(passed=False,glob_defaulted=False,glob_pm_approved=False,glob_path_matches_workspace=False)
    except Exception as exc:
        report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        await host.close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'approvals':report['approvals'],'error':report.get('error'),
                      'report':str(output/'report.json'),'key_printed':False}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent',choices=['claude','codex','opencode'],default='claude')
    parser.add_argument('--timeout',type=int,default=600)
    parser.add_argument('--glob',action='store_true',help='Exercise omitted-path glob compatibility and PM approval (OpenCode)')
    args=parser.parse_args()
    if args.glob and args.agent!='opencode':
        parser.error('--glob requires --agent opencode')
    raise SystemExit(asyncio.run(main(args.agent,args.timeout,args.glob)))
