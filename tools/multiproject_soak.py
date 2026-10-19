"""Bounded concurrent project knowledge/restart load; no model calls."""
import argparse
import asyncio
from collections import deque
import json
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from autocoding.host import Host
from autocoding.core import encoded


def percentile(values,p):
    ordered=sorted(values)
    return round(ordered[min(len(ordered)-1,int(len(ordered)*p))],2) if ordered else None


async def main(seconds,projects,records):
    output=Path(tempfile.mkdtemp(prefix='multiproject-soak-',dir=ROOT/'.local'))
    tracemalloc.start()
    states=[]
    try:
        for index in range(projects):
            root=output/('project-'+str(index))
            host=Host(root,root/'state',cold_start=False)
            with host.core.connection() as conn:
                conn.execute('BEGIN IMMEDIATE')
                for i in range(records):
                    record={'id':f'p{index}-{i}','state':'completed','created_at':host.core.clock(),
                        'items':[{'category':'lesson','topic':f'subject {i}','content':f'project {index} rule {i}',
                                  'confidence':'hypothesis','evidence_ids':[f'p{index}']}]}
                    conn.execute('INSERT INTO metadata VALUES (?,?)',('pm_review:'+record['id'],encoded(record)))
                conn.commit()
            tick=time.perf_counter()
            await asyncio.to_thread(host.reflection.learning.page)
            cold=(time.perf_counter()-tick)*1000
            await host.close()
            host=Host(root,root/'state',cold_start=False)
            tick=time.perf_counter()
            await asyncio.to_thread(host.reflection.learning.page)
            warm=(time.perf_counter()-tick)*1000
            states.append(dict(host=host,root=root,index=index,cold_ms=round(cold,2),cached_restart_ms=round(warm,2),
                               timings=deque(maxlen=10000),restarts=0,iterations=0))
        baseline=tracemalloc.get_traced_memory()[0]
        lag=deque(maxlen=10000)
        started=time.monotonic()
        def query(state):
            host=state['host']
            page=host.reflection.learning.page((state['iterations']*25)%records)
            assert page['total']==records and len(page['items'])<=25
            refs=host.reflection.references()
            assert len(refs)<=12 and all(r['evidence_ids']==[f"p{state['index']}"] for r in refs)
        async def worker(state):
            restart=time.monotonic()
            while time.monotonic()-started<seconds:
                tick=time.perf_counter()
                await asyncio.to_thread(query,state)
                state['timings'].append((time.perf_counter()-tick)*1000)
                state['iterations']+=1
                if time.monotonic()-restart>=60:
                    await state['host'].close()
                    state['host']=await asyncio.to_thread(Host,state['root'],state['root']/'state',cold_start=False)
                    await asyncio.to_thread(query,state)
                    state['restarts']+=1
                    restart=time.monotonic()
                await asyncio.sleep(.02)
        async def heartbeat():
            last_report=started
            while time.monotonic()-started<seconds:
                tick=time.monotonic()
                await asyncio.sleep(.05)
                lag.append(max(0,(time.monotonic()-tick-.05)*1000))
                if time.monotonic()-last_report>=30:
                    print(json.dumps({'elapsed_seconds':round(time.monotonic()-started),
                        'iterations':sum(s['iterations'] for s in states)}),flush=True)
                    last_report=time.monotonic()
        await asyncio.gather(*(worker(s) for s in states),heartbeat())
        growth=tracemalloc.get_traced_memory()[0]-baseline
        details=[{k:s[k] for k in ['index','cold_ms','cached_restart_ms','restarts','iterations']}|
                 {'p95_ms':percentile(s['timings'],.95),'p99_ms':percentile(s['timings'],.99)} for s in states]
        report=dict(model_calls=0,synthetic=True,records_per_project=records,elapsed_seconds=round(time.monotonic()-started,2),
                    projects=details,loop_lag_p95_ms=percentile(lag,.95),tracked_growth_bytes=growth,
                    passed=all(d['p95_ms']<500 and d['iterations']>0 for d in details) and percentile(lag,.95)<250 and growth<32*1024*1024)
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps({**report,'report':str(output/'report.json')}),flush=True)
        return 0 if report['passed'] else 1
    finally:
        for state in states:
            await state['host'].close()
        tracemalloc.stop()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--projects',type=int,default=3)
    parser.add_argument('--records',type=int,default=5000)
    args=parser.parse_args()
    if not 1<=args.seconds<=86400 or not 1<=args.projects<=10 or not 1<=args.records<=100000:
        parser.error('invalid bounded load parameters')
    raise SystemExit(asyncio.run(main(args.seconds,args.projects,args.records)))
