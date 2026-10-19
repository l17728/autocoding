"""Real Codex repair on an isolated copy of platform source, without credentials.

Seed two reproduced regressions, protect the tests, and independently verify the
Agent's repair. This is an application-code sample, not long-duration acceptance.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.core import DomainError
from autocoding.host import Host
from autocoding.workspaces import git


def replace_once(text,old,new):
    if text.count(old)!=1:
        raise RuntimeError('PROBE_SOURCE_VERSION_CHANGED')
    return text.replace(old,new,1)


async def main(timeout):
    output=Path(tempfile.mkdtemp(prefix='platform-workload-',dir=ROOT/'.local'))
    repo=output/'project';repo.mkdir()
    for folder in ('src','tests'):
        shutil.copytree(ROOT/folder,repo/folder,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (repo/'.gitignore').write_text('.local/\n__pycache__/\n*.pyc\n',encoding='utf-8')
    report={'passed':False,'provider':'codex','application_source':True,'seeded_regressions':True,
            'long_duration_acceptance':False,'zhipu_calls':0,'source_hashes':{}}
    for relative in ('src/autocoding/redaction.py','src/autocoding/api.py'):
        source=(ROOT/relative).read_text(encoding='utf-8')
        report['source_hashes'][relative]=hashlib.sha256(source.encode()).hexdigest()
        if relative.endswith('redaction.py'):
            start=source.index('        # CSI final bytes')
            end=source.index('        prefixes =',start)
            source=source[:start]+source[end:]
            source=replace_once(source,"                if complete_csi and len(self.pending)-size >= complete_csi.start():\n                    continue\n",'')
        else:
            source=replace_once(source,'                cursor = sent_cursor','                cursor = entry[\'seq\']')
            source=replace_once(source,'                if closed:\n','                if host.terminals.sessions[sid]._closed:\n')
        (repo/relative).write_text(source,encoding='utf-8')
    check=[sys.executable,'-m','unittest','tests.test_redaction','tests.test_terminal_stream']
    baseline_check=await asyncio.to_thread(subprocess.run,check,cwd=repo,capture_output=True,timeout=45)
    baseline_log=(baseline_check.stdout+baseline_check.stderr).decode('utf-8',errors='replace')
    (output/'baseline-check.log').write_text(baseline_log,encoding='utf-8')
    if baseline_check.returncode!=1 or not all(name in baseline_log for name in
            ('FAIL: test_terminal_queries_are_complete','FAIL: test_output_produced_during_send')):
        raise RuntimeError('REGRESSIONS_NOT_REPRODUCED')
    git(repo,'init');git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.invalid')
    git(repo,'add','.');git(repo,'commit','-m','isolated platform regressions')
    baseline=git(repo,'rev-parse','HEAD')
    host=Host(repo,repo/'.local/workbench')
    async def no_zhipu(*args,**kwargs):
        raise DomainError('ZHIPU_VALIDATION_DEFERRED_UNTIL_2026_10_17')
    host.pm_text=no_zhipu
    (repo/'.local/acceptance.json').write_text(json.dumps({'checks':{'regression':check},
        'protected_paths':['tests/**']}),encoding='utf-8')
    host.core.create_task('task_terminal_repair',
        'Repair two reproduced terminal defects in this isolated platform repository: '
        'the terminal redactor holds CSI final bytes as credential prefixes, and the terminal WebSocket '
        'loses output when new frames and process exit occur during send. Read tests/test_redaction.py and '
        'tests/test_terminal_stream.py for protected behavioral expectations. Change only '
        'src/autocoding/redaction.py and src/autocoding/api.py. Preserve credential redaction, authentication, '
        'bounded buffering, replay protection and existing API behavior. Do not edit tests. '
        'Use available tools to inspect and edit the source, including local shell commands if needed. '
        'This bounded benchmark accepts only the two protected test suites named above. '
        'Run those suites explicitly; do not launch repository-wide pytest or unittest discovery. '
        'Any additional focused diagnostic must use an actual subprocess timeout of at most 45 seconds, '
        'not merely a tool output wait interval. Report other suites as not run. '
        'Do not commit or edit Git configuration; '
        'the platform independently executes the protected checks. Give a final reply after the repair.',
        ['regression'],key='task')
    started=time.monotonic()
    try:
        run=await host.workflow.start('task_terminal_repair',agent='codex',max_seconds=timeout)
        async with asyncio.timeout(timeout+60):
            await host.workflow.active['task_terminal_repair']
        final=next(item for item in host.workflow.list() if item['id']==run['id'])
        report.update(state=final['state'],run=final,source_unchanged=git(repo,'rev-parse','HEAD')==baseline,
                      mcp_verified=any(e['type']=='MCPToolAuthorized' for e in host.core.events()))
        report['passed']=report['state']=='done' and report['source_unchanged'] and report['mcp_verified']
    except Exception as exc:
        report['error']=getattr(exc,'code',type(exc).__name__)
    finally:
        # Retain bounded, already-redacted public diagnostics even when the
        # provider disconnects; task state alone cannot identify transport cause.
        (output/'public-output.json').write_text(json.dumps(list(host.output),ensure_ascii=False,indent=2),encoding='utf-8')
        await host.close()
    report['seconds']=round(time.monotonic()-started,2)
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'seconds':report['seconds'],'report':str(output/'report.json')}))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout',type=int,default=600,choices=range(30,1801))
    raise SystemExit(asyncio.run(main(parser.parse_args().timeout)))
