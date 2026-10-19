"""Bounded real Codex development across independent disposable projects.

Never calls Zhipu. This is a short real workload sample, not 24-hour acceptance.
"""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from workflow_probe import main as workflow, ROOT, Host
from autocoding.core import DomainError


async def main(projects,rounds,timeout):
    output=Path(tempfile.mkdtemp(prefix='codex-workload-',dir=ROOT/'.local'))
    original=Host.pm_text
    deferred=[]
    async def no_zhipu(self,*args,**kwargs):
        deferred.append(self.project_id)
        raise DomainError('ZHIPU_VALIDATION_DEFERRED_UNTIL_2026_10_17')
    Host.pm_text=no_zhipu
    started=time.monotonic()
    rows=[]
    async def worker(index):
        for iteration in range(rounds):
            folder=output/f'project-{index}-round-{iteration}'
            tick=time.monotonic()
            try:
                result=await workflow('codex',timeout,output=folder)
                detail=json.loads((folder/'report.json').read_text(encoding='utf-8'))
                rows.append({'project':index,'round':iteration,'passed':result==0,
                    'source_unchanged':detail.get('source_unchanged',False),
                    'mcp_verified':detail.get('mcp_verified',False),
                    'error':detail.get('error'),'seconds':round(time.monotonic()-tick,2),
                    'report':str(folder/'report.json')})
            except Exception as exc:
                rows.append({'project':index,'round':iteration,'passed':False,
                             'error':getattr(exc,'code',type(exc).__name__)})
            print(json.dumps({'completed':len(rows),'expected':projects*rounds}),flush=True)
            if not rows[-1]['passed']:
                break
    try:
        await asyncio.gather(*(worker(i) for i in range(projects)))
    finally:
        Host.pm_text=original
    report={'provider':'codex','synthetic_model':False,'disposable_fixture':True,
        'zhipu_calls':0,'zhipu_calls_prevented':len(deferred),
        'long_duration_acceptance':False,'seconds':round(time.monotonic()-started,2),
        'projects':projects,'rounds':rounds,'runs':rows,
        'passed':len(rows)==projects*rounds and all(r['passed'] for r in rows)}
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'report':str(output/'report.json')}),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projects',type=int,choices=range(1,4),default=2)
    parser.add_argument('--rounds',type=int,choices=range(1,11),default=2)
    parser.add_argument('--timeout',type=int,choices=range(30,1801),default=300)
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.projects,args.rounds,args.timeout)))
