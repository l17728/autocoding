"""Real provider pre-dispatch failure -> Owner recovery -> verified batch/closure.

Injects failure before any first-attempt input reaches the provider. This proves
pre-dispatch recovery, not interruption or stopping a running remote model.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.recovery import Recovery
from autocoding.workspaces import git


async def main(agent, timeout):
    output = Path(tempfile.mkdtemp(prefix='recovery-probe-', dir=ROOT/'.local'))
    repo = output/'project'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo/'calculator.py').write_text('def add(a,b):\n    return 0\n', encoding='utf-8')
    (repo/'test_calculator.py').write_text('from calculator import add\nassert add(2,3)==5\nassert add(-3,2)==-1\n', encoding='utf-8')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'fixture')
    base = git(repo, 'rev-parse', 'HEAD')
    host = Host(repo, repo/'.local/workbench', config=ROOT/'.local/credentials/opencode.json')
    (repo/'.local/acceptance.json').write_text(json.dumps({'checks':{'unit':[sys.executable,'test_calculator.py']},
        'protected_paths':['test_calculator.py']}), encoding='utf-8')
    host.core.create_task('task_add', 'Implement add(a,b) in calculator.py as return a+b. Only edit calculator.py. '
        'The platform runs tests and commits. Use autocoding MCP get_instructions first; finish briefly.', ['unit'], key='task')
    report = dict(agent=agent, passed=False, fault='before_first_input', remote_stop_proven=False, key_printed=False)
    send = host.send
    async def fail_before_send(*args, **kwargs):
        raise DomainError('PROBE_BEFORE_DISPATCH')
    try:
        host.send = fail_before_send
        first = await host.workflow.start('task_add', agent=agent)
        await asyncio.wait_for(asyncio.shield(host.workflow.active['task_add']), timeout)
        host.send = send
        old = next(r for r in host.workflow.list() if r['id']==first['id'])
        if old['state']!='reconciling' or old.get('reason')!='PROBE_BEFORE_DISPATCH' or not old.get('session_id'):
            raise DomainError('PROBE_FAULT_NOT_REACHED')
        consumer=host.consumers.get(old['session_id'])
        if consumer:
            await asyncio.wait_for(asyncio.shield(consumer), 15)
        evidence = await asyncio.to_thread(Recovery(host).inspect, first['id'])
        if evidence['commands']:
            raise DomainError('PROBE_UNEXPECTED_INPUT')
        report['recovery'] = await asyncio.to_thread(Recovery(host).resolve, first['id'], evidence['fingerprint'],
            'Isolated probe: failure injected before any input dispatch; adapter closed and no commands sent.', True, key='probe-recovery')
        batch = await host.coordinator.start(['task_add'], agent=agent, max_attempts=1)
        async with asyncio.timeout(timeout):
            while host.coordinator.current is not None:
                await asyncio.sleep(.2)
        final = next(b for b in host.coordinator.list() if b['id']==batch['id'])
        runs = [r for r in host.workflow.list() if r['id']!=first['id']]
        report.update(batch=final, runs=runs, closures=len(host.closure.list()),
            source_unchanged=git(repo,'rev-parse','HEAD')==base,
            independent_workspace=all(r.get('workspace_id')!=old['workspace_id'] for r in runs),
            old_session_sealed=next(s for s in host.core.snapshot()['sessions'] if s['id']==old['session_id'])['state']=='closed')
        report['passed'] = bool(runs) and final['state']=='verified_candidate' and report['closures']>0 and all(
            report[k] for k in ['source_unchanged','independent_workspace','old_session_sealed'])
        report['errors'] = [e for e in host.output if e['type']=='error']
    except Exception as exc:
        report['error'] = getattr(exc, 'code', type(exc).__name__)
    finally:
        await host.close()
        (output/'report.json').write_text(json.dumps(host.redactor.value(report),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'],agent=agent,error=report.get('error'),report=str(output/'report.json'))))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent',choices=['codex','claude','opencode'],required=True)
    parser.add_argument('--timeout',type=int,default=600)
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.agent,args.timeout)))
